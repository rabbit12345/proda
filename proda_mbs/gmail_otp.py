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
        """Authenticate with Gmail API using JSON-based OAuth2 credentials."""
        credentials = None

        # Load existing credentials from JSON token file
        if os.path.exists(self.config.token_path):
            try:
                credentials = Credentials.from_authorized_user_file(
                    self.config.token_path, self.config.scopes
                )
            except (json.JSONDecodeError, ValueError) as e:
                raise GmailAuthError(
                    f"Corrupted token file '{self.config.token_path}': {e}"
                )

        # Refresh or obtain new credentials
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                except Exception as e:
                    raise GmailAuthError(f"Failed to refresh token: {e}")
            else:
                if not os.path.exists(self.config.client_secret_path):
                    raise GmailAuthError(
                        f"Client secret file not found: "
                        f"'{self.config.client_secret_path}'"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.config.client_secret_path, self.config.scopes
                )
                credentials = flow.run_local_server(port=0)

            # Save credentials as JSON (not pickle)
            with open(self.config.token_path, "w") as token_file:
                token_file.write(credentials.to_json())

        return build("gmail", "v1", credentials=credentials)

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
