from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By


class PortalPageState(str, Enum):
    UNKNOWN = "unknown"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    LOGIN = "login"
    OTP = "otp"
    MY_SERVICES = "my_services"
    HPOS_LANDING = "hpos_landing"
    MBS_FORM = "mbs_form"
    MBS_RESULTS = "mbs_results"
    SESSION_EXPIRED = "session_expired"
    LOGGED_OUT = "logged_out"
    OFFSITE = "offsite"


@dataclass(frozen=True)
class PageSnapshot:
    state: PortalPageState
    url: str
    title: str
    ready_state: str
    body_excerpt: str
    has_reset_button: bool = False
    has_medicare_field: bool = False
    has_results_table: bool = False
    has_login_field: bool = False
    has_otp_field: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.state in {
            PortalPageState.MY_SERVICES,
            PortalPageState.HPOS_LANDING,
            PortalPageState.MBS_FORM,
            PortalPageState.MBS_RESULTS,
        }

    @property
    def needs_relogin(self) -> bool:
        return self.state in {
            PortalPageState.LOGIN,
            PortalPageState.OTP,
            PortalPageState.SESSION_EXPIRED,
            PortalPageState.LOGGED_OUT,
            PortalPageState.OFFSITE,
            PortalPageState.BROWSER_UNAVAILABLE,
        }

    @property
    def is_mbs_page(self) -> bool:
        return self.state in {
            PortalPageState.MBS_FORM,
            PortalPageState.MBS_RESULTS,
        }


class PageStateDetector:
    _SESSION_EXPIRED_MARKERS = (
        "session has expired",
        "session timeout",
        "your session has timed",
    )

    _LOGGED_OUT_MARKERS = (
        "you have been logged out",
        "you have successfully logged out",
        "signed out",
    )

    _ALLOWED_HOST_MARKERS = (
        "medicareaustralia.gov.au",
        "humanservices.gov.au",
        "servicesaustralia.gov.au",
    )

    def __init__(self, driver):
        self.driver = driver

    def snapshot(self) -> PageSnapshot:
        try:
            url = str(self.driver.current_url or "")
            title = str(self.driver.title or "")
        except Exception as exc:
            return PageSnapshot(
                state=PortalPageState.BROWSER_UNAVAILABLE,
                url="",
                title="",
                ready_state="",
                body_excerpt=str(exc)[:400],
            )

        try:
            has_login_field = self._has_id("loginFormAndStuff:username")
            has_otp_field = self._has_id("otppswd")
            has_medicare_field = self._has_id("guiForm:guiMedicareCardNumber")
            has_reset_button = self._has_id("guiForm:gui_searchAgain") or self._has_id("guiForm:gui_reset")
            has_results_table = self._has_id("guiForm:guiMbsItemNumberSearchResults")
            has_no_results = self._has_id("guiForm:noResultsLabel")
            has_hpos_link = self._any_present(
                [
                    (By.XPATH, "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'go to service')]"),
                    (By.XPATH, "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'hpos')]"),
                    (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'go to service')]"),
                ]
            )
        except WebDriverException as exc:
            return PageSnapshot(
                state=PortalPageState.BROWSER_UNAVAILABLE,
                url=url,
                title=title,
                ready_state="",
                body_excerpt=str(exc)[:400],
            )

        url_lower = url.lower()
        title_lower = title.lower()
        state = PortalPageState.UNKNOWN

        if has_login_field:
            state = PortalPageState.LOGIN
        elif has_otp_field:
            state = PortalPageState.OTP
        elif has_medicare_field:
            state = (
                PortalPageState.MBS_RESULTS
                if has_results_table or has_no_results or has_reset_button
                else PortalPageState.MBS_FORM
            )
        elif "my services" in title_lower:
            state = PortalPageState.MY_SERVICES
        elif "health professional online services" in title_lower or has_hpos_link:
            state = PortalPageState.HPOS_LANDING
        elif self._looks_offsite(url_lower):
            state = PortalPageState.OFFSITE
        elif self._contains_any_text(self._SESSION_EXPIRED_MARKERS):
            state = PortalPageState.SESSION_EXPIRED
        elif (
            "loggedout" in url_lower
            or "logged-out" in url_lower
            or self._contains_any_text(self._LOGGED_OUT_MARKERS)
        ):
            state = PortalPageState.LOGGED_OUT

        return PageSnapshot(
            state=state,
            url=url,
            title=title,
            ready_state="",
            body_excerpt="",
            has_reset_button=has_reset_button,
            has_medicare_field=has_medicare_field,
            has_results_table=has_results_table,
            has_login_field=has_login_field,
            has_otp_field=has_otp_field,
        )

    def _has_id(self, element_id: str) -> bool:
        return bool(self.driver.find_elements(By.ID, element_id))

    def _any_present(self, selectors: list[tuple[str, str]]) -> bool:
        return any(self.driver.find_elements(by, value) for by, value in selectors)

    def _contains_any_text(self, phrases: tuple[str, ...]) -> bool:
        for phrase in phrases:
            xpath = (
                "//*[contains("
                "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"'{phrase.lower()}')]"
            )
            if self.driver.find_elements(By.XPATH, xpath):
                return True
        return False

    def _looks_offsite(self, url_lower: str) -> bool:
        if not url_lower or url_lower.startswith("about:blank"):
            return False
        return not any(host in url_lower for host in self._ALLOWED_HOST_MARKERS)
