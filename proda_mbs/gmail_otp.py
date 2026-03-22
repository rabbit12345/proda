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


class GmailAuthError(Exception):
    """Raised when Gmail authentication fails."""
    pass


class GmailOtpExtractor:
    SENDER = "proda@servicesaustralia.gov.au"
    OTP_PATTERN = re.compile(r"^\d{6}$")

    def __init__(self, config: GmailConfig):
        self.config = config
        self.service = self._build_service()

    def _build_service(self):
        """Authenticate with Gmail API using JSON-based OAuth2 credentials.

        Auth flow:
        1. If token.json exists and is valid -> use it directly
        2. If token.json exists but expired -> refresh it (no client_secret needed)
        3. If token.json missing or unrecoverable -> requires client_secret.json
           to run the OAuth consent flow and create a new token.json
        """
        credentials = self._load_existing_token()

        if credentials and credentials.valid:
            # Case 1: token.json is valid, use as-is
            return build("gmail", "v1", credentials=credentials)

        if credentials and credentials.expired and credentials.refresh_token:
            # Case 2: token expired but refreshable (no client_secret.json needed)
            try:
                credentials.refresh(Request())
                self._save_token(credentials)
                return build("gmail", "v1", credentials=credentials)
            except Exception as e:
                # Refresh failed — fall through to full re-auth
                print(f"Token refresh failed ({e}), re-authenticating...")

        # Case 3: no valid token — need client_secret.json for consent flow
        credentials = self._run_consent_flow()
        self._save_token(credentials)
        return build("gmail", "v1", credentials=credentials)

    def _load_existing_token(self):
        """Load credentials from token.json if it exists. Returns None if absent."""
        if not os.path.exists(self.config.token_path):
            return None

        try:
            return Credentials.from_authorized_user_file(
                self.config.token_path, self.config.scopes
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: corrupted token file, will re-authenticate: {e}")
            return None

    def _run_consent_flow(self):
        """Run OAuth consent flow using client_secret.json.

        This opens a browser for the user to grant Gmail access.
        Only needed when token.json is missing or unrecoverable.
        """
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
        """Save credentials to token.json for future runs."""
        try:
            token_dir = os.path.dirname(self.config.token_path)
            if token_dir:
                os.makedirs(token_dir, exist_ok=True)
            with open(self.config.token_path, "w") as token_file:
                token_file.write(credentials.to_json())
        except OSError as e:
            print(f"Warning: could not save token file: {e}")

    def get_otp_code(self, max_wait: int = 30, poll_interval: int = 2) -> str | None:
        """Poll Gmail for OTP code with retry logic.

        Args:
            max_wait: Maximum seconds to wait for the email.
            poll_interval: Seconds between polling attempts.

        Returns:
            The 6-digit OTP code string, or None if not found.
        """
        elapsed = 0
        while elapsed < max_wait:
            code = self._try_extract_code()
            if code:
                return code
            time.sleep(poll_interval)
            elapsed += poll_interval

        return None

    def _try_extract_code(self) -> str | None:
        """Attempt to find and extract the OTP code from unread PRODA emails."""
        query = f"from:{self.SENDER} newer_than:5m"
        results = self.service.users().messages().list(
            userId="me", q=query
        ).execute()

        messages = results.get("messages")
        if not messages:
            return None

        for message in messages:
            msg = self.service.users().messages().get(
                userId="me", id=message["id"]
            ).execute()

            try:
                payload = msg["payload"]
                body = payload.get("body", {})
                data = body.get("data")
                if not data:
                    # Try multipart message structure
                    parts = payload.get("parts", [])
                    for part in parts:
                        part_data = part.get("body", {}).get("data")
                        if part_data:
                            data = part_data
                            break
                    if not data:
                        continue

                # Use proper URL-safe base64 decoding
                msg_raw = base64.urlsafe_b64decode(data)

                soup = BeautifulSoup(msg_raw, "html.parser")
                strong_tag = soup.body.find("strong") if soup.body else None
                if strong_tag:
                    code = strong_tag.get_text().strip()
                    # Validate OTP is a 6-digit number
                    if self.OTP_PATTERN.match(code):
                        # Trash the processed email
                        self.service.users().messages().trash(
                            userId="me", id=message["id"]
                        ).execute()
                        return code
            except (KeyError, AttributeError):
                continue

        return None
