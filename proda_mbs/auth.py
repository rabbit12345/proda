from __future__ import annotations

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .config import AppConfig
from .gmail_otp import GmailOtpExtractor, GmailAuthError


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


class LoginError(Exception):
    pass


# CSS selectors for error detection on JSF pages
_ERROR_SELECTORS = [
    "div.ui-messages-error",
    "div.ui-messages",
    "div.ui-growl-message",
    "div.alert-danger",
    "div.error",
    "span.error",
    "span.ui-message-error-detail",
    "div#errorMessage",
    "p.error-message",
]

_ERROR_PHRASES = ["invalid", "incorrect", "failed", "locked",
                  "expired", "disabled", "try again"]


class ProdaAuthenticator:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self.page_timeout = config.session.page_load_timeout
        self.gmail_extractor = None

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

    def _diag(self) -> str:
        try:
            return f"title='{self.driver.title}' url='{self.driver.current_url}'"
        except Exception:
            return "could not read page state"

    def _find_error_on_page(self) -> str | None:
        """Scan the current page for visible error messages."""
        # Check URL for error codes
        try:
            url = self.driver.current_url
            if "ERROR_CODE" in url and "0x00000000" not in url:
                return f"PRODA returned error (URL: {url})"
        except Exception:
            pass

        # Check known error element selectors
        for selector in _ERROR_SELECTORS:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed() and el.text.strip():
                    return el.text.strip()
            except NoSuchElementException:
                continue

        # Scan body text for error phrases
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            for phrase in _ERROR_PHRASES:
                for line in body_text.split("\n"):
                    if phrase in line.lower() and len(line.strip()) < 200:
                        return line.strip()
        except Exception:
            pass

        return None

    # -- Login flow -----------------------------------------------------------

    def login(self, max_otp_attempts: int = 3):
        """Execute the full PRODA login flow including 2FA."""
        self._navigate_to_login()
        self._enter_credentials()
        self._submit_login()

        self._init_gmail_extractor()
        self.gmail_extractor.purge_old_otp_emails()
        self._request_otp_via_email()

        for attempt in range(1, max_otp_attempts + 1):
            log(f"OTP attempt {attempt}/{max_otp_attempts}")
            code = self._retrieve_otp_from_gmail()

            if not code:
                if attempt < max_otp_attempts:
                    log("No OTP found, requesting a new code...")
                    self._request_new_otp()
                    continue
                raise LoginError("Could not retrieve OTP code from Gmail")

            if self._submit_otp(code):
                log("Login complete - reached My Services page")
                return

            if attempt < max_otp_attempts:
                log("OTP rejected, purging and requesting a new code...")
                self.gmail_extractor.purge_old_otp_emails()
                self._request_new_otp()
            else:
                raise LoginError(
                    f"OTP rejected after {max_otp_attempts} attempts. {self._diag()}"
                )

    # -- Step implementations -------------------------------------------------

    def _navigate_to_login(self):
        log("Navigating to PRODA login page")
        self.driver.get(self.config.proda.url)
        try:
            self._wait(
                EC.presence_of_element_located(
                    (By.ID, "loginFormAndStuff:username")
                ),
                timeout=self.page_timeout
            )
            log(f"Login page loaded ({self._diag()})")
        except TimeoutException:
            raise LoginError(f"PRODA login page did not load. {self._diag()}")

    def _enter_credentials(self):
        username = self.config.proda.username
        password = self.config.proda.password
        log(f"Entering credentials (user: {username}, pass: {'*' * len(password)})")

        if not username or not password:
            raise LoginError("PRODA username or password is empty. Check config.yaml")

        try:
            username_field = self.driver.find_element(
                By.ID, "loginFormAndStuff:username"
            )
            username_field.click()
            username_field.clear()
            # Set via JS to bypass client-side keystroke validation (e.g. '@')
            self.driver.execute_script(
                "arguments[0].value = arguments[1]", username_field, username
            )
            self.driver.execute_script("""
                var el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            """, username_field)

            password_field = self.driver.find_element(
                By.ID, "loginFormAndStuff:inputPassword"
            )
            password_field.click()
            password_field.clear()
            password_field.send_keys(password)
        except NoSuchElementException as e:
            raise LoginError(f"Could not find login form fields: {e}")

    def _submit_login(self):
        log("Submitting login form")
        try:
            submit_btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "loginFormAndStuff:submitLoginWithProda")
            ))
            submit_btn.click()
        except (TimeoutException, NoSuchElementException):
            raise LoginError(
                f"Login submit button not found or not clickable. {self._diag()}"
            )

        try:
            self._wait(
                EC.title_contains("2-step verification"),
                timeout=self.page_timeout
            )
            log(f"Reached 2-step verification page ({self._diag()})")
        except TimeoutException:
            error_msg = self._find_error_on_page()
            if error_msg:
                raise LoginError(f"Login rejected: {error_msg}")
            raise LoginError(
                f"Did not reach 2-step verification page after login. {self._diag()}"
            )

    def _request_otp_via_email(self):
        log("Requesting OTP via email")
        try:
            reselect_link = self._wait(EC.element_to_be_clickable(
                (By.ID, "reselectLink")
            ))
            reselect_link.click()
            log("Clicked 'send code to backup channel'")
        except TimeoutException:
            raise LoginError(f"Could not find backup channel link. {self._diag()}")

        time.sleep(2)

        try:
            email_option = self._wait(EC.element_to_be_clickable(
                (By.ID, "span-email")
            ))
            email_option.click()
            log("Selected email channel")
        except TimeoutException:
            raise LoginError(f"Could not find email option. {self._diag()}")

        try:
            submit_btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "submit-btn")
            ))
            submit_btn.click()
            log("Submitted email OTP request")
        except TimeoutException:
            raise LoginError(
                f"Could not find submit button after email selection. {self._diag()}"
            )

        try:
            self._wait(
                EC.presence_of_element_located((By.ID, "otppswd")),
                timeout=self.page_timeout
            )
            log("OTP entry page ready - waiting for email delivery")
        except TimeoutException:
            raise LoginError(
                f"OTP entry page did not load after email request. {self._diag()}"
            )

    def _init_gmail_extractor(self):
        if self.gmail_extractor is not None:
            return
        try:
            self.gmail_extractor = GmailOtpExtractor(self.config.gmail)
        except GmailAuthError as e:
            raise LoginError(f"Gmail authentication failed: {e}")

    def _retrieve_otp_from_gmail(self) -> str | None:
        log("Retrieving OTP from Gmail")
        code = self.gmail_extractor.get_otp_code()
        if code:
            log(f"OTP code retrieved: {code}")
        else:
            log("No OTP code found in Gmail")
        return code

    def _request_new_otp(self):
        """Request a new OTP code when the previous one failed."""
        resend_selectors = [
            (By.XPATH, "//a[contains(text(), 'Resend')]"),
            (By.XPATH, "//a[contains(text(), 'resend')]"),
            (By.XPATH, "//a[contains(text(), 'Send again')]"),
            (By.XPATH, "//a[contains(text(), 'new code')]"),
            (By.XPATH, "//button[contains(text(), 'Resend')]"),
            (By.ID, "resendLink"),
        ]
        for by, selector in resend_selectors:
            try:
                el = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((by, selector))
                )
                el.click()
                log("Clicked resend OTP link")
                time.sleep(3)
                return
            except TimeoutException:
                continue

        log("No resend link found, re-requesting via backup channel")
        self._request_otp_via_email()

    def _submit_otp(self, code: str) -> bool:
        """Submit OTP code. Returns True if successful, False if rejected."""
        log(f"Entering OTP code: {code}")
        try:
            otp_field = self._wait(EC.presence_of_element_located(
                (By.ID, "otppswd")
            ))
            otp_field.click()
            otp_field.clear()
            for ch in code:
                otp_field.send_keys(ch)
                time.sleep(0.1)

            time.sleep(2)
            log("OTP entered, submitting...")

            submit_btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "submit-btn")
            ))
            submit_btn.click()

            self._wait(
                EC.title_contains("My Services"),
                timeout=self.page_timeout * 2
            )
            log("OTP accepted, reached My Services")
            return True
        except TimeoutException:
            title = self.driver.title
            log(f"Post-OTP state: title='{title}' url='{self.driver.current_url}'")

            error_msg = self._find_error_on_page()
            if error_msg:
                log(f"OTP error: {error_msg}")
                return False

            # If we're on a page that's not login/verification, assume success
            if "verification" not in title.lower() and "login" not in title.lower():
                log(f"Landed on '{title}' — continuing")
                return True

            return False
