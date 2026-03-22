import os
import pickle
import base64
import time

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from bs4 import BeautifulSoup

from .config import GmailConfig


class GmailOtpExtractor:
    SENDER = "proda@servicesaustralia.gov.au"

    def __init__(self, config: GmailConfig):
        self.config = config
        self.service = self._build_service()

    def _build_service(self):
        """Authenticate with Gmail API and return the service object."""
        credentials = None

        if os.path.exists(self.config.token_path):
            with open(self.config.token_path, "rb") as token:
                credentials = pickle.load(token)

        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.config.client_secret_path, self.config.scopes
                )
                credentials = flow.run_local_server(port=0)

            with open(self.config.token_path, "wb") as token:
                pickle.dump(credentials, token)

        return build("gmail", "v1", credentials=credentials)

    def get_otp_code(self, max_wait: int = 30, poll_interval: int = 2) -> str | None:
        """Poll Gmail for OTP code with retry logic.

        Args:
            max_wait: Maximum seconds to wait for the email.
            poll_interval: Seconds between polling attempts.

        Returns:
            The OTP code string, or None if not found.
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
                    continue

                data = data.replace("-", "+").replace("_", "/")
                msg_raw = base64.b64decode(data)

                soup = BeautifulSoup(msg_raw, "html.parser")
                strong_tag = soup.body.find("strong") if soup.body else None
                if strong_tag:
                    code = strong_tag.get_text().strip()
                    # Trash the processed email
                    self.service.users().messages().trash(
                        userId="me", id=message["id"]
                    ).execute()
                    return code
            except Exception:
                continue

        return None
