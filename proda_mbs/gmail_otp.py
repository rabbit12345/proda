from __future__ import annotations

import os
import json
import base64
import time
import re

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

from .config import GmailConfig
from .waits import log


class GmailAuthError(Exception):
    pass


# Subject keywords that identify OTP emails (vs other PRODA notifications)
_OTP_SUBJECT_KEYWORDS = ["verification", "otp", "one-time", "security code"]


class GmailOtpExtractor:
    SENDER = "proda@servicesaustralia.gov.au"
    OTP_PATTERN = re.compile(r"^\d{6}$")
    # Standalone 6-digit run, used only as a fallback when no <strong> matches.
    OTP_LOOSE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")

    def __init__(self, config: GmailConfig):
        self.config = config
        self._otp_requested_at: int = 0
        self.service = self._build_service()

    def _build_service(self):
        """Authenticate with Gmail API using JSON-based OAuth2 credentials."""
        credentials = self._load_existing_token()

        if credentials and credentials.valid:
            return build("gmail", "v1", credentials=credentials)

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                self._save_token(credentials)
                return build("gmail", "v1", credentials=credentials)
            except Exception as e:
                log(f"Token refresh failed ({e}), re-authenticating...")

        credentials = self._run_consent_flow()
        self._save_token(credentials)
        return build("gmail", "v1", credentials=credentials)

    def _load_existing_token(self):
        if not os.path.exists(self.config.token_path):
            return None
        try:
            return Credentials.from_authorized_user_file(
                self.config.token_path, self.config.scopes
            )
        except (json.JSONDecodeError, ValueError) as e:
            log(f"Warning: corrupted token file, will re-authenticate: {e}")
            return None

    def _run_consent_flow(self):
        secret_path = self.config.client_secret_path
        if not os.path.exists(secret_path):
            raise GmailAuthError(
                f"Gmail OAuth requires initial setup.\n"
                f"  client_secret.json not found at: {secret_path}\n"
                f"  \n"
                f"  To set up:\n"
                f"  1. Go to https://console.cloud.google.com/apis/credentials\n"
                f"  2. Enable the Gmail API\n"
                f"  3. Create OAuth 2.0 Client ID (Desktop application)\n"
                f"  4. Download the JSON and save as: {secret_path}\n"
                f"  5. Run this program again — a browser will open for consent\n"
                f"  \n"
                f"  After first consent, only token.json is needed for future runs."
            )
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                secret_path, self.config.scopes
            )
            return flow.run_local_server(port=0)
        except Exception as e:
            raise GmailAuthError(f"OAuth consent flow failed: {e}")

    def _save_token(self, credentials):
        try:
            token_dir = os.path.dirname(self.config.token_path)
            if token_dir:
                os.makedirs(token_dir, exist_ok=True)
            with open(self.config.token_path, "w") as f:
                f.write(credentials.to_json())
        except OSError as e:
            log(f"Warning: could not save token file: {e}")

    # -- OTP retrieval --------------------------------------------------------

    def purge_old_otp_emails(self):
        """Trash only PRODA verification emails (not other PRODA notifications).
        Uses sender + subject query, then double-checks subject keywords."""
        log("Purging old PRODA OTP emails...")
        query = f"from:{self.SENDER}"
        try:
            results = self.service.users().messages().list(
                userId="me", q=query
            ).execute()

            count = 0
            for message in results.get("messages", []):
                msg_meta = self.service.users().messages().get(
                    userId="me", id=message["id"], format="metadata",
                    metadataHeaders=["Subject"]
                ).execute()

                if self._is_otp_subject(msg_meta.get("payload", {})):
                    self.service.users().messages().trash(
                        userId="me", id=message["id"]
                    ).execute()
                    count += 1

            if count:
                log(f"Trashed {count} old PRODA OTP email(s)")
        except Exception as e:
            log(f"Warning: could not purge old emails: {e}")

    def mark_otp_requested(self):
        """Record the timestamp when an OTP was requested.
        Only emails arriving after this time will be accepted.

        A 5-second buffer is subtracted to tolerate minor clock skew
        between this machine and Gmail's servers (internalDate is set
        by Google, not by the sending MTA).
        """
        self._otp_requested_at = int(time.time() * 1000) - 5000
        log(f"OTP request timestamp set (with 5 s buffer)")

    def get_otp_code(self, max_wait: int = 90, poll_interval: int = 3) -> str | None:
        """Poll Gmail for a fresh OTP code that arrived after the request.

        Waits for the email to arrive, verifies it's newer than the OTP
        request timestamp, extracts and validates the 6-digit code, then
        trashes the email before returning.
        """
        min_time = self._otp_requested_at

        # Try immediately — the email often arrives during login
        # submission + page-load time, so it may already be in the inbox.
        elapsed = 0
        while True:
            code = self._try_extract_code(min_time)
            if code:
                return code
            if elapsed >= max_wait:
                break
            log(f"Waiting for OTP email... ({elapsed}s/{max_wait}s)")
            time.sleep(poll_interval)
            elapsed += poll_interval

        return None

    def _try_extract_code(self, min_timestamp: int = 0) -> str | None:
        """Extract OTP from a fresh PRODA verification email.

        Args:
            min_timestamp: Only accept emails with internalDate >= this
                           value (milliseconds since epoch). Ensures we
                           don't grab a stale code from a previous request.
        """
        query = f"from:{self.SENDER} newer_than:2m"
        results = self.service.users().messages().list(
            userId="me", q=query
        ).execute()

        messages = results.get("messages")
        if not messages:
            return None

        for message in messages:
            # A failure on one message must not abort the whole poll — the
            # next attempt (3s later) may well succeed, and giving up here
            # would strand a login on an OTP email that is already present.
            try:
                # One full fetch per message: the payload already carries the
                # Subject header, so no separate metadata round-trip is needed.
                msg = self.service.users().messages().get(
                    userId="me", id=message["id"]
                ).execute()

                if not self._is_otp_subject(msg.get("payload", {})):
                    continue

                # Verify email arrived after we requested the OTP
                internal_date = int(msg.get("internalDate", 0))
                if min_timestamp and internal_date < min_timestamp:
                    log(f"Skipping stale email (arrived {internal_date} < requested {min_timestamp})")
                    continue

                data = self._get_body_data(msg["payload"])
                if not data:
                    continue

                msg_raw = base64.urlsafe_b64decode(data)
                code = self._extract_code_from_html(msg_raw)

                if code:
                    log(f"Found fresh OTP: {code}")
                    self.service.users().messages().trash(
                        userId="me", id=message["id"]
                    ).execute()
                    return code

                # A fresh OTP email was matched but yielded no code — surface
                # it instead of silently polling on, which looks like the email
                # never arrived.
                log(
                    f"OTP email {message['id']} matched but no 6-digit code "
                    "could be extracted from its body"
                )
            except Exception as e:
                log(f"Warning: could not inspect message {message['id']}: {e}")
                continue

        return None

    @classmethod
    def _extract_code_from_html(cls, msg_raw: bytes) -> str | None:
        """Pull the 6-digit OTP out of an email body.

        PRODA wraps the code in <strong>, but not always as the first such tag
        (headings and greetings are bolded too), so every <strong> is checked
        before falling back to a scan of the plain text.
        """
        soup = BeautifulSoup(msg_raw, "html.parser")
        root = soup.body or soup

        for tag in root.find_all("strong"):
            candidate = tag.get_text().strip()
            if cls.OTP_PATTERN.match(candidate):
                return candidate

        # Fallback: the code may not be bolded at all in some templates.
        matches = cls.OTP_LOOSE_PATTERN.findall(root.get_text(" ", strip=True))
        if len(set(matches)) == 1:
            return matches[0]
        if matches:
            log(f"Ambiguous 6-digit candidates in OTP email: {sorted(set(matches))}")
        return None

    @staticmethod
    def _is_otp_subject(payload: dict) -> bool:
        """True if the message payload's Subject marks it as an OTP email."""
        subject = ""
        for header in payload.get("headers", []):
            if header.get("name", "").lower() == "subject":
                subject = (header.get("value") or "").lower()
                break
        return any(kw in subject for kw in _OTP_SUBJECT_KEYWORDS)

    @classmethod
    def _get_body_data(cls, payload: dict) -> str | None:
        """Extract body data from a Gmail message payload, recursing into
        nested multipart parts (e.g. multipart/mixed > multipart/alternative)."""
        data = payload.get("body", {}).get("data")
        if data:
            return data
        for part in payload.get("parts", []):
            part_data = cls._get_body_data(part)
            if part_data:
                return part_data
        return None
