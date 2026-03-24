from __future__ import annotations

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from .config import AppConfig
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

    def _wait(self, condition, timeout=None):
        return WebDriverWait(
            self.driver, timeout or self.wait_timeout
        ).until(condition)

    def _switch_to_new_window(self, original_handles, target_title_fragment: str = ""):
        """Switch to a newly opened window/tab if one appears.

        To avoid a long wait when the link navigates the *current* tab
        instead of opening a new one, we also check whether the current
        window's title already contains ``target_title_fragment``.
        """
        deadline = 10  # max seconds to wait for a new handle
        try:
            def _new_handle_or_current_nav(d):
                # A new tab appeared — switch to it
                if len(d.window_handles) > len(original_handles):
                    return "new_window"
                # No new tab, but the current page already navigated
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
            else:
                log("HPOS loaded in current tab (no new window)")
                return True
        except TimeoutException:
            pass
        return False

    def navigate_to_hpos(self):
        log("Navigating to HPOS from My Services")
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
            # Best-effort page-load wait.  On HPOS the government
            # portal often has slow background resources (tracking
            # pixels, certificate negotiation) that keep readyState at
            # "loading" long after the page is usable.  execute_script
            # can block at the transport level with no way for
            # WebDriverWait's timeout to interrupt it, causing an
            # indefinite hang that only Ctrl-C can break.  A short,
            # non-fatal wait is sufficient — the title check above
            # already confirms we are on the right page.
            try:
                wait_for_page_load(self.driver, timeout=5)
            except Exception as e:
                log(f"Page readyState wait skipped ({e}) — title confirmed, continuing")
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
            self._wait(
                EC.presence_of_element_located(
                    (By.ID, "guiForm:guiMedicareCardNumber")
                ),
                timeout=self.page_timeout * 2
            )
            log(f"Reached MBS Items Online Checker ({self.driver.current_url})")
        except TimeoutException:
            log(f"MBS form not found. title='{self.driver.title}' "
                f"url='{self.driver.current_url}'")
            self._dump_menu_links()
            raise NavigationError("Failed to load MBS Items Online Checker form")

    def navigate_to_mbs_checker_full(self):
        self.navigate_to_hpos()
        self.navigate_to_mbs_checker()

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
