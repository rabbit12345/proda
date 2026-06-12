from __future__ import annotations

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .config import AppConfig
from .gmail_otp import GmailOtpExtractor, GmailAuthError
from .page_state import PageStateDetector, PortalPageState
from .waits import wait_for_ajax, wait_for_page_load, log


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
        self.state_detector = PageStateDetector(driver)

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

    def _diag(self) -> str:
        try:
            return f"title='{self.driver.title}' url='{self.driver.current_url}'"
        except Exception:
            return "could not read page state"

    def _wait_for_login_page(self, timeout: int):
        def _login_snapshot_or_terminal(_driver):
            snapshot = self.state_detector.snapshot()
            if snapshot.state == PortalPageState.LOGIN:
                return snapshot
            if snapshot.state in {
                PortalPageState.OTP,
                PortalPageState.MY_SERVICES,
                PortalPageState.HPOS_LANDING,
                PortalPageState.MBS_FORM,
                PortalPageState.MBS_RESULTS,
            }:
                raise LoginError(
                    "Browser landed on unexpected page while opening login: "
                    f"state={snapshot.state.value} url='{snapshot.url}'"
                )
            if snapshot.state in {
                PortalPageState.SESSION_EXPIRED,
                PortalPageState.LOGGED_OUT,
                PortalPageState.OFFSITE,
                PortalPageState.BROWSER_UNAVAILABLE,
            }:
                raise LoginError(
                    "PRODA did not present the login form after navigation: "
                    f"state={snapshot.state.value} url='{snapshot.url}'"
                )
            return False

        return self._wait(_login_snapshot_or_terminal, timeout=timeout)

    def _find_error_on_page(self) -> str | None:
        """Scan the current page for visible error messages."""
        try:
            url = self.driver.current_url
            if "ERROR_CODE" in url and "0x00000000" not in url:
                return f"PRODA returned error (URL: {url})"
        except Exception:
            pass

        for selector in _ERROR_SELECTORS:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed() and el.text.strip():
                    return el.text.strip()
            except NoSuchElementException:
                continue

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

    def login(self, max_otp_attempts: int = 3, max_login_retries: int = 2):
        """Execute the full PRODA login flow including 2FA."""
        self._init_gmail_extractor()

        for login_try in range(1, max_login_retries + 1):
            if login_try > 1:
                log(f"Restarting full login (attempt {login_try}/{max_login_retries})")

            self._navigate_to_login()
            self._enter_credentials()

            # Purge old OTP emails and set the request timestamp BEFORE
            # submitting login.  PRODA sends the OTP during _submit_login(),
            # so the timestamp must already be recorded — otherwise the
            # freshly-arrived email has an internalDate earlier than the
            # timestamp and the stale-filter incorrectly skips it, causing
            # an unnecessary 90-second wait and a duplicate resend email.
            self.gmail_extractor.purge_old_otp_emails()
            self.gmail_extractor.mark_otp_requested()

            self._submit_login()

            if self._is_otp_field_present():
                log("OTP field already present - portal sent OTP to default channel, skipping backup request")
            else:
                log("OTP field not found - requesting via backup email channel")
                self._request_otp_via_email()

            if self._otp_loop(max_otp_attempts):
                snapshot = self.state_detector.snapshot()
                if snapshot.state not in {
                    PortalPageState.MY_SERVICES,
                    PortalPageState.HPOS_LANDING,
                    PortalPageState.MBS_FORM,
                    PortalPageState.MBS_RESULTS,
                }:
                    raise LoginError(
                        "Login completed but browser did not land on an "
                        f"authenticated page: state={snapshot.state.value} "
                        f"url='{snapshot.url}'"
                    )
                log("Login complete - reached authenticated portal")
                return

            if login_try < max_login_retries:
                log("All OTP attempts failed, will retry full login...")
            else:
                raise LoginError(
                    f"Login failed after {max_login_retries} full attempts. "
                    f"{self._diag()}"
                )

    def _otp_loop(self, max_attempts: int) -> bool:
        """Try up to max_attempts OTP codes. Returns True on success."""
        for attempt in range(1, max_attempts + 1):
            log(f"OTP attempt {attempt}/{max_attempts}")
            code = self._retrieve_otp_from_gmail()

            if not code:
                if attempt < max_attempts:
                    log("No OTP found, clicking 'Didn't get your code?'...")
                    self.gmail_extractor.purge_old_otp_emails()
                    self._click_didnt_get_code()
                    continue
                log("No OTP found after all attempts")
                return False

            if self._submit_otp(code):
                return True

            if attempt < max_attempts:
                log("OTP incorrect/expired, clicking 'Didn't get your code?'...")
                self.gmail_extractor.purge_old_otp_emails()
                self._click_didnt_get_code()

        return False

    # -- Step implementations -------------------------------------------------

    def _navigate_to_login(self):
        log("Navigating to PRODA login page")
        self.driver.get(self.config.proda.url)
        try:
            wait_for_page_load(self.driver, min(self.page_timeout, 5))
            snapshot = self._wait_for_login_page(timeout=self.page_timeout)
            log(f"Login page loaded ({self._diag()})")
            return snapshot
        except LoginError:
            raise
        except TimeoutException:
            raise LoginError(f"PRODA login page did not load. {self._diag()}")

    def _enter_credentials(self):
        username = self.config.proda.username
        password = self.config.proda.password
        log(f"Entering credentials (user: {username}, pass: {'*' * len(password)})")

        if not username or not password:
            raise LoginError("PRODA username or password is empty. Check config.yaml")

        try:
            username_field = self._wait(EC.element_to_be_clickable(
                (By.ID, "loginFormAndStuff:username")
            ))
            username_field.click()
            username_field.clear()
            self.driver.execute_script(
                "arguments[0].value = arguments[1]", username_field, username
            )
            self.driver.execute_script("""
                var el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            """, username_field)

            password_field = self._wait(EC.element_to_be_clickable(
                (By.ID, "loginFormAndStuff:inputPassword")
            ))
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
            wait_for_page_load(self.driver, self.page_timeout)
            log(f"Reached 2-step verification page ({self._diag()})")
        except TimeoutException:
            error_msg = self._find_error_on_page()
            if error_msg:
                raise LoginError(f"Login rejected: {error_msg}")
            raise LoginError(
                f"Did not reach 2-step verification page after login. {self._diag()}"
            )

    def _is_otp_field_present(self, timeout: int = 5) -> bool:
        """Check if the OTP entry field is already visible,
        meaning the portal sent OTP to the default channel."""
        try:
            self._wait(EC.presence_of_element_located((By.ID, "otppswd")), timeout=timeout)
            return True
        except TimeoutException:
            return False

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

        # Wait for email option to appear (no fixed sleep)
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

    def _click_didnt_get_code(self):
        """Click 'Didn't get your code?' link to request a new OTP."""
        resend_selectors = [
            (By.XPATH, "//a[contains(text(), \"Didn't get your code\")]"),
            (By.XPATH, "//a[contains(text(), \"didn't get your code\")]"),
            (By.CSS_SELECTOR, "a[href*='regenerateButtonDiv']"),
            (By.XPATH, "//a[contains(text(), 'Resend')]"),
            (By.XPATH, "//a[contains(text(), 'Send again')]"),
            (By.XPATH, "//a[contains(text(), 'new code')]"),
        ]
        for by, selector in resend_selectors:
            try:
                el = self._wait(EC.element_to_be_clickable((by, selector)), timeout=5)
                self.gmail_extractor.mark_otp_requested()
                el.click()
                log("Clicked 'Didn't get your code?' link")
                # Wait for OTP field to be ready again (no fixed sleep)
                try:
                    self._wait(
                        EC.presence_of_element_located((By.ID, "otppswd")),
                        timeout=self.page_timeout
                    )
                except TimeoutException:
                    log("OTP field not found after resend, page may have changed")
                return
            except TimeoutException:
                continue

        log("No resend link found on page, re-requesting via backup channel")
        self.gmail_extractor.mark_otp_requested()
        self._request_otp_via_email()

    def _submit_otp(self, code: str) -> bool:
        """Submit OTP code. Returns True if successful, False if rejected."""
        log(f"Entering OTP code: {code}")
        try:
            otp_field = self._wait(EC.element_to_be_clickable(
                (By.ID, "otppswd")
            ))
            otp_field.click()
            otp_field.clear()
            otp_field.send_keys(code)

            # Verify the field accepted all characters
            self._wait(
                lambda d: d.find_element(By.ID, "otppswd").get_attribute("value") == code,
                timeout=5
            )

            # Wait for any client-side validation AJAX
            wait_for_ajax(self.driver, timeout=5)

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
            url = self.driver.current_url
            log(f"Post-OTP state: title='{title}' url='{url}'")

            error_msg = self._find_error_on_page()
            if error_msg:
                log(f"OTP error: {error_msg}")
                return False

            if "verification" not in title.lower() and "login" not in title.lower():
                log(f"Landed on '{title}' — continuing")
                return True

            return False
