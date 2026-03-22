import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .config import AppConfig
from .gmail_otp import GmailOtpExtractor


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

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

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
            self._wait(EC.presence_of_element_located(
                (By.ID, "loginFormAndStuff:username")
            ))
        except TimeoutException:
            raise LoginError("PRODA login page did not load")

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
            self._wait(EC.title_contains("2-step verification"))
            log("Reached 2-step verification page")
        except TimeoutException:
            raise LoginError(
                "Did not reach 2-step verification page after login"
            )

    def _request_otp_via_email(self):
        log("Requesting OTP via email")
        try:
            # Click "Send code to backup channel"
            self._wait(EC.element_to_be_clickable(
                (By.ID, "reselectLink")
            )).click()
            self._wait(EC.title_contains("One-Time Password"))

            # Select email option
            self._wait(EC.element_to_be_clickable(
                (By.ID, "span-email")
            )).click()

            # Click Next
            self._wait(EC.element_to_be_clickable(
                (By.ID, "submit-btn")
            )).click()
            self._wait(EC.title_contains("2-step verification"))
            log("OTP requested via email - waiting for delivery")
        except TimeoutException:
            raise LoginError("Failed during OTP email request flow")

    def _retrieve_otp_from_gmail(self):
        log("Retrieving OTP from Gmail")
        extractor = GmailOtpExtractor(self.config.gmail)
        code = extractor.get_otp_code()
        if not code:
            raise LoginError("Could not retrieve OTP code from Gmail")
        log(f"OTP code retrieved: {code}")
        return code

    def _submit_otp(self, code: str):
        log("Submitting OTP code")
        try:
            otp_field = self._wait(EC.presence_of_element_located(
                (By.ID, "otppswd")
            ))
            otp_field.send_keys(code)

            self.driver.find_element(By.ID, "submit-btn").click()
            self._wait(
                EC.title_contains("My Services"),
                timeout=self.config.session.page_load_timeout
            )
        except TimeoutException:
            raise LoginError(
                "Did not reach My Services page after OTP submission"
            )
