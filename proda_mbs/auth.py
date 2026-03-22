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
    """Raised when a login step fails."""
    pass


class ProdaAuthenticator:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self.page_timeout = config.session.page_load_timeout

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

    def _diag(self) -> str:
        """Return current page title and URL for diagnostics."""
        try:
            return f"title='{self.driver.title}' url='{self.driver.current_url}'"
        except Exception:
            return "could not read page state"

    def login(self):
        """Execute the full PRODA login flow including 2FA."""
        self._navigate_to_login()
        self._enter_credentials()
        self._submit_login()
        self._request_otp_via_email()
        code = self._retrieve_otp_from_gmail()
        self._submit_otp(code)
        log("Login complete - reached My Services page")

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
            raise LoginError(
                f"PRODA login page did not load. {self._diag()}"
            )

    def _enter_credentials(self):
        log("Entering credentials")
        try:
            username_field = self.driver.find_element(
                By.ID, "loginFormAndStuff:username"
            )
            username_field.clear()
            username_field.send_keys(self.config.proda.username)

            password_field = self.driver.find_element(
                By.ID, "loginFormAndStuff:inputPassword"
            )
            password_field.clear()
            password_field.send_keys(self.config.proda.password)
        except NoSuchElementException as e:
            raise LoginError(f"Could not find login form fields: {e}")

    def _submit_login(self):
        log("Submitting login form")
        try:
            self.driver.find_element(
                By.ID, "loginFormAndStuff:submitLoginWithProda"
            ).click()
        except NoSuchElementException:
            raise LoginError(
                f"Login submit button not found. {self._diag()}"
            )

        # Wait for 2-step verification page to load.
        # Use page_load_timeout since this is a full page navigation.
        try:
            self._wait(
                EC.title_contains("2-step verification"),
                timeout=self.page_timeout
            )
            log(f"Reached 2-step verification page ({self._diag()})")
        except TimeoutException:
            # Check if there's a login error message on the page
            error_msg = self._check_login_error()
            if error_msg:
                raise LoginError(f"Login rejected: {error_msg}")
            raise LoginError(
                f"Did not reach 2-step verification page after login. "
                f"{self._diag()}"
            )

    def _check_login_error(self) -> str | None:
        """Check if the login page is showing an error message."""
        # PRODA shows error via ERROR_CODE in URL or error elements on page
        try:
            url = self.driver.current_url
            if "ERROR_CODE" in url and "0x00000000" not in url:
                return f"PRODA returned error (URL: {url})"
        except Exception:
            pass

        # Check for common error text patterns on the page
        for selector in [
            "div.error", "div.alert-danger", "span.error",
            "div#errorMessage", "p.error-message"
        ]:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed() and el.text.strip():
                    return el.text.strip()
            except NoSuchElementException:
                continue

        return None

    def _request_otp_via_email(self):
        """Request OTP code to be sent via email backup channel."""
        log("Requesting OTP via email")

        # The 2-step verification page may already have the OTP input
        # and a link to send code to backup channel (email).
        # Click "send a code to a backup channel" to switch delivery method.
        try:
            reselect_link = self._wait(EC.element_to_be_clickable(
                (By.ID, "reselectLink")
            ))
            reselect_link.click()
            log("Clicked 'send code to backup channel'")
        except TimeoutException:
            raise LoginError(
                f"Could not find backup channel link. {self._diag()}"
            )

        # After clicking reselectLink, the page may reload with channel
        # selection options. Wait for the email option to appear.
        # The page title may change to "One-Time Password" or stay as
        # "2-step verification" depending on PRODA version.
        time.sleep(2)  # Allow page transition

        try:
            email_option = self._wait(EC.element_to_be_clickable(
                (By.ID, "span-email")
            ))
            email_option.click()
            log("Selected email channel")
        except TimeoutException:
            raise LoginError(
                f"Could not find email option. {self._diag()}"
            )

        # Click Next/Submit to confirm email delivery
        try:
            submit_btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "submit-btn")
            ))
            submit_btn.click()
            log("Submitted email OTP request")
        except TimeoutException:
            raise LoginError(
                f"Could not find submit button after email selection. "
                f"{self._diag()}"
            )

        # Wait for the OTP entry page to load.
        # The page should show the OTP input field (otppswd).
        try:
            self._wait(
                EC.presence_of_element_located((By.ID, "otppswd")),
                timeout=self.page_timeout
            )
            log("OTP entry page ready - waiting for email delivery")
        except TimeoutException:
            raise LoginError(
                f"OTP entry page did not load after email request. "
                f"{self._diag()}"
            )

    def _retrieve_otp_from_gmail(self):
        log("Retrieving OTP from Gmail")
        try:
            extractor = GmailOtpExtractor(self.config.gmail)
        except GmailAuthError as e:
            raise LoginError(f"Gmail authentication failed: {e}")

        code = extractor.get_otp_code()
        if not code:
            raise LoginError("Could not retrieve OTP code from Gmail")
        log("OTP code retrieved successfully")
        return code

    def _submit_otp(self, code: str):
        log("Submitting OTP code")
        try:
            otp_field = self._wait(EC.presence_of_element_located(
                (By.ID, "otppswd")
            ))
            otp_field.clear()
            otp_field.send_keys(code)

            self.driver.find_element(By.ID, "submit-btn").click()
            self._wait(
                EC.title_contains("My Services"),
                timeout=self.page_timeout
            )
        except TimeoutException:
            raise LoginError(
                f"Did not reach My Services page after OTP submission. "
                f"{self._diag()}"
            )
