from __future__ import annotations

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from .config import AppConfig
from .page_state import PageSnapshot, PageStateDetector, PortalPageState
from .waits import wait_for_page_load, log


class NavigationError(Exception):
    pass


_HPOS_MBS_URL = (
    "https://www2.medicareaustralia.gov.au:5447"
    "/pcert/hpos/securityRedirect.do?target=HPOS.NAVMENU.ITEM.MBSIOC"
)

_HPOS_SELECTORS = [
    (By.XPATH, "//a[contains(@onclick, 'j_id_2_2_1n:0:j_id_2_2_1s')]"),
    (By.XPATH, "//a[contains(text(), 'Go to service')]"),
    (By.XPATH, "//a[contains(text(), 'HPOS')]"),
    (By.XPATH, "//button[contains(text(), 'Go to service')]"),
    (By.CSS_SELECTOR, "a.go-to-service"),
]


class HposNavigator:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self.page_timeout = config.session.page_load_timeout
        self.state_detector = PageStateDetector(driver)

    def _wait(self, condition, timeout=None):
        return WebDriverWait(
            self.driver, timeout or self.wait_timeout
        ).until(condition)

    def _switch_to_new_window(self, original_handles, target_title_fragment: str = ""):
        """Switch to a newly opened or reused HPOS window if one appears."""
        deadline = 10  # max seconds to wait for a usable HPOS window
        try:
            original_handle = self.driver.current_window_handle

            def _scan_existing_handles():
                current_handles = list(self.driver.window_handles)
                target_title_lower = target_title_fragment.lower()
                for handle in current_handles:
                    self.driver.switch_to.window(handle)
                    title_lower = (self.driver.title or "").lower()
                    url_lower = (self.driver.current_url or "").lower()
                    if (
                        target_title_lower and target_title_lower in title_lower
                    ) or "health professional online services" in title_lower or "/hpos/" in url_lower:
                        return handle
                self.driver.switch_to.window(original_handle)
                return ""

            def _new_handle_or_current_nav(d):
                if len(d.window_handles) > len(original_handles):
                    return "new_window"
                existing_handle = _scan_existing_handles()
                if existing_handle:
                    return existing_handle
                if target_title_fragment and target_title_fragment.lower() in d.title.lower():
                    return "same_window"
                return False

            result = WebDriverWait(self.driver, deadline).until(
                _new_handle_or_current_nav
            )

            if result == "new_window":
                new_handles = set(self.driver.window_handles) - set(original_handles)
                if new_handles:
                    self.driver.switch_to.window(new_handles.pop())
                    log(f"Switched to new window: title='{self.driver.title}'")
                    return True
            elif result not in {"same_window", "new_window"}:
                self.driver.switch_to.window(result)
                log(f"Attached to existing window: title='{self.driver.title}'")
                return True
            else:
                log("HPOS loaded in current tab (no new window)")
                return True
        except TimeoutException:
            pass
        return False

    def navigate_to_hpos(self):
        log("Navigating to HPOS from My Services")

        # The HPOS sub-session expires well before the PRODA one. When it does,
        # the browser is left on a stale MBS page (or anywhere else) while
        # PRODA is still logged in, so load My Services explicitly rather than
        # assuming we are already on it.
        snapshot = self.get_page_snapshot()
        if snapshot.state != PortalPageState.MY_SERVICES:
            log(f"Not on My Services (state={snapshot.state.value}); loading it")
            self.driver.get(self.config.proda.url)

        try:
            self._wait(EC.title_contains("My Services"),
                       timeout=self.page_timeout)
        except TimeoutException:
            raise NavigationError(
                f"Not on My Services page. title='{self.driver.title}'"
            )

        original_handles = self.driver.window_handles

        clicked = False
        for by, selector in _HPOS_SELECTORS:
            try:
                link = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((by, selector))
                )
                log(f"Found HPOS link with: {selector}")
                link.click()
                clicked = True
                break
            except TimeoutException:
                continue

        if not clicked:
            raise NavigationError("Could not find HPOS 'Go to service' link")

        hpos_title = "Health Professional Online Services"
        self._switch_to_new_window(original_handles,
                                   target_title_fragment=hpos_title)

        try:
            self._wait(
                EC.title_contains(hpos_title),
                timeout=self.page_timeout
            )
            # Best-effort page-load wait. On HPOS the portal often keeps
            # background resources alive after the page is already usable.
            try:
                wait_for_page_load(self.driver, timeout=5)
            except Exception as e:
                log(f"Page readyState wait skipped ({e}); title confirmed, continuing")
            log(f"Reached HPOS landing page ({self.driver.current_url})")
        except TimeoutException:
            raise NavigationError(
                f"Failed to reach HPOS. title='{self.driver.title}' "
                f"url='{self.driver.current_url}'"
            )

    def navigate_to_mbs_checker(self):
        log("Navigating to MBS Items Online Checker")
        self.driver.get(_HPOS_MBS_URL)

        try:
            wait_for_page_load(self.driver, self.page_timeout * 2)
            snapshot = self.wait_for_mbs_ready(timeout=self.page_timeout * 2)
            log(f"Reached MBS Items Online Checker ({snapshot.url})")
        except TimeoutException:
            log(f"MBS form not found. title='{self.driver.title}' "
                f"url='{self.driver.current_url}'")
            self._dump_menu_links()
            raise NavigationError("Failed to load MBS Items Online Checker form")

    def navigate_to_mbs_checker_full(self):
        self.navigate_to_hpos()
        self.navigate_to_mbs_checker()

    def get_page_snapshot(self) -> PageSnapshot:
        return self.state_detector.snapshot()

    def wait_for_mbs_ready(self, timeout: int | None = None) -> PageSnapshot:
        timeout = timeout or self.page_timeout
        return self._wait(
            lambda d: self._mbs_snapshot_if_ready(),
            timeout=timeout,
        )

    def _mbs_snapshot_if_ready(self) -> PageSnapshot | bool:
        snapshot = self.get_page_snapshot()
        if snapshot.state in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
            return snapshot
        if snapshot.state in {
            PortalPageState.LOGIN,
            PortalPageState.OTP,
            PortalPageState.SESSION_EXPIRED,
            PortalPageState.LOGGED_OUT,
            PortalPageState.OFFSITE,
            PortalPageState.BROWSER_UNAVAILABLE,
            PortalPageState.UNKNOWN,
        }:
            raise NavigationError(
                "MBS navigation reached a terminal page state: "
                f"{snapshot.state.value} title='{snapshot.title}' url='{snapshot.url}'"
            )
        return False

    def _dump_menu_links(self):
        try:
            links = self.driver.find_elements(By.TAG_NAME, "a")
            log(f"Found {len(links)} links on page:")
            for link in links:
                href = link.get_attribute("href") or ""
                text = link.text.strip()
                if text or "ITEM" in href.upper() or "MBS" in href.upper():
                    log(f"  text='{text}' href='{href[:120]}'")
        except Exception as e:
            log(f"  (could not dump links: {e})")
