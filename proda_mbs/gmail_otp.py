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

# Body keywords used when the subject wording is unrecognised, so an OTP is
# still accepted rather than stalling the login for the whole poll window.
_OTP_BODY_KEYWORDS = [
    "verification code", "one-time", "one time", "security code",
    "your code", "authentication code", "2-step", "two-step",
]


class GmailOtpExtractor:
    SENDER = "proda@servicesaustralia.gov.au"
    SENDER_DOMAIN = "servicesaustralia.gov.au"
    OTP_PATTERN = re.compile(r"^\d{6}$")
    # Standalone 6-digit run, used only as a fallback when no <strong> matches.
    OTP_LOOSE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")
    # A 6-digit run introduced by an OTP cue, e.g. "your code is 123456".
    OTP_CUED_PATTERN = re.compile(
        r"(?:code|password|otp)\D{0,20}?(?<!\d)(\d{6})(?!\d)", re.IGNORECASE
    )
    # Scanned when the search index has not caught up and no query matched.
    RECENT_SCAN_LIMIT = 25
    # Tolerance for host-vs-Gmail clock skew when judging an email as stale.
    CLOCK_SKEW_BUFFER_MS = 120_000

    def __init__(self, config: GmailConfig):
        self.config = config
        self._otp_requested_at: int = 0
        self._stale_logged: set[str] = set()
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
        # in:anywhere so an OTP that was filtered into Spam is purged too,
        # otherwise it lingers and competes with the fresh code.
        query = f"from:{self.SENDER_DOMAIN} in:anywhere -in:trash"
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

        A generous buffer is subtracted to tolerate clock skew between this
        machine and Gmail's servers (internalDate is set by Google, not by
        this host). The old 5-second buffer meant a workstation whose clock
        ran even a minute fast rejected every genuinely fresh OTP as stale.
        Purging runs immediately before each request, so the stale codes this
        guard protects against are already gone and a wide buffer is cheap.
        """
        self._otp_requested_at = int(time.time() * 1000) - self.CLOCK_SKEW_BUFFER_MS
        self._stale_logged.clear()
        log(f"OTP request timestamp set (with {self.CLOCK_SKEW_BUFFER_MS // 1000} s buffer)")

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

    def _candidate_message_ids(self) -> list[str]:
        """Message ids that could carry a fresh OTP.

        Two independent sources, because either can miss on its own:

        * A sender search scoped with ``in:anywhere`` so an OTP filtered into
          Spam is still seen (plain Gmail search silently excludes Spam).
        * A raw listing of the most recent messages, used when the search
          returns nothing. Gmail's search index lags delivery by seconds to
          minutes, so a just-arrived OTP is routinely invisible to ``q=``
          while already fetchable by id — the main reason a delivered code
          goes uncaught.
        """
        ids: list[str] = []
        seen = set()

        def add(entries):
            for entry in entries or []:
                if entry["id"] not in seen:
                    seen.add(entry["id"])
                    ids.append(entry["id"])

        try:
            results = self.service.users().messages().list(
                userId="me",
                q=f"from:{self.SENDER_DOMAIN} in:anywhere -in:trash newer_than:1h",
            ).execute()
            add(results.get("messages"))
        except Exception as e:
            log(f"Warning: OTP search query failed: {e}")

        if not ids:
            try:
                results = self.service.users().messages().list(
                    userId="me", maxResults=self.RECENT_SCAN_LIMIT
                ).execute()
                add(results.get("messages"))
            except Exception as e:
                log(f"Warning: recent-message listing failed: {e}")

        return ids

    def _try_extract_code(self, min_timestamp: int = 0) -> str | None:
        """Extract OTP from a fresh PRODA verification email.

        Args:
            min_timestamp: Only accept emails with internalDate >= this
                           value (milliseconds since epoch). Ensures we
                           don't grab a stale code from a previous request.
        """
        for message_id in self._candidate_message_ids():
            # A failure on one message must not abort the whole poll — the
            # next attempt (3s later) may well succeed, and giving up here
            # would strand a login on an OTP email that is already present.
            try:
                # One full fetch per message: the payload already carries the
                # headers, so no separate metadata round-trip is needed.
                msg = self.service.users().messages().get(
                    userId="me", id=message_id
                ).execute()

                payload = msg.get("payload", {})
                if not self._is_from_proda(payload):
                    continue

                # Verify email arrived after we requested the OTP
                internal_date = int(msg.get("internalDate", 0))
                if min_timestamp and internal_date < min_timestamp:
                    # Logged once per message, not once per 3-second poll, so
                    # a wrongly-rejected OTP is still visible in the log.
                    if message_id not in self._stale_logged:
                        self._stale_logged.add(message_id)
                        log(
                            f"Skipping stale email {message_id} "
                            f"(arrived {internal_date} < requested {min_timestamp})"
                        )
                    continue

                text = self._message_text(payload)
                if not text:
                    continue

                # The subject is a hint, not a gate: PRODA has shipped OTPs
                # under wording the keyword list did not anticipate, and
                # gating on it stalls the login for the full poll window.
                if not (self._is_otp_subject(payload) or self._looks_like_otp_body(text)):
                    continue

                code = self._extract_code_from_html(text)

                if code:
                    log(f"Found fresh OTP: {code}")
                    self.service.users().messages().trash(
                        userId="me", id=message_id
                    ).execute()
                    return code

                # A fresh OTP email was matched but yielded no code — surface
                # it instead of silently polling on, which looks like the email
                # never arrived.
                log(
                    f"OTP email {message_id} matched but no 6-digit code "
                    "could be extracted from its body"
                )
            except Exception as e:
                log(f"Warning: could not inspect message {message_id}: {e}")
                continue

        return None

    @classmethod
    def _is_from_proda(cls, payload: dict) -> bool:
        """True if the From header is within the PRODA sending domain.

        Matched on domain rather than the exact address so a change of local
        part (noreply@, donotreply@, proda-notifications@) does not silently
        stop OTP retrieval.
        """
        sender = cls._header(payload, "from").lower()
        return cls.SENDER_DOMAIN in sender

    @classmethod
    def _looks_like_otp_body(cls, text: str) -> bool:
        """True if the body reads like an OTP mail, used when the subject
        does not match the known keywords."""
        lowered = text.lower()
        return any(kw in lowered for kw in _OTP_BODY_KEYWORDS)

    @classmethod
    def _message_text(cls, payload: dict) -> str:
        """Decode and concatenate every body part of a message.

        The old single-part lookup returned only the first part carrying data
        — usually text/plain in a multipart/alternative pair — so a code
        present only in the HTML alternative was never seen.
        """
        chunks = []
        for data in cls._iter_body_data(payload):
            try:
                chunks.append(base64.urlsafe_b64decode(data).decode("utf-8", "replace"))
            except Exception:
                continue
        return "\n".join(chunks)

    @classmethod
    def _iter_body_data(cls, payload: dict):
        """Yield the base64 body data of every part, depth-first."""
        data = payload.get("body", {}).get("data")
        if data:
            yield data
        for part in payload.get("parts", []):
            yield from cls._iter_body_data(part)

    @staticmethod
    def _header(payload: dict, name: str) -> str:
        for header in payload.get("headers", []):
            if header.get("name", "").lower() == name:
                return header.get("value") or ""
        return ""

    @classmethod
    def _extract_code_from_html(cls, msg_raw: bytes | str) -> str | None:
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
        text = root.get_text(" ", strip=True)

        # Prefer a digit run introduced by an OTP cue ("your code is 123456"),
        # which stays unambiguous even when the mail also quotes a reference
        # number, phone number or date.
        cued = cls.OTP_CUED_PATTERN.search(text)
        if cued:
            return cued.group(1)

        matches = cls.OTP_LOOSE_PATTERN.findall(text)
        if len(set(matches)) == 1:
            return matches[0]
        if matches:
            log(f"Ambiguous 6-digit candidates in OTP email: {sorted(set(matches))}")
        return None

    @classmethod
    def _is_otp_subject(cls, payload: dict) -> bool:
        """True if the message payload's Subject marks it as an OTP email."""
        subject = cls._header(payload, "subject").lower()
        return any(kw in subject for kw in _OTP_SUBJECT_KEYWORDS)
