import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from .config import AppConfig


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


class NavigationError(Exception):
    pass


# Known HPOS URLs
_HPOS_MBS_URL = (
    "https://www2.medicareaustralia.gov.au:5447"
    "/pcert/hpos/securityRedirect.do?target=HPOS.NAVMENU.ITEM.MBSIOC"
)

# Selectors tried in order for the HPOS link on My Services
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

    def _wait_for_page_load(self, timeout=None):
        """Wait for document.readyState to be 'complete'."""
        timeout = timeout or self.page_timeout
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def _switch_to_new_window(self, original_handles):
        """Switch to a newly opened window/tab if one appeared."""
        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: len(d.window_handles) > len(original_handles)
            )
            new_handles = set(self.driver.window_handles) - set(original_handles)
            if new_handles:
                self.driver.switch_to.window(new_handles.pop())
                log(f"Switched to new window: title='{self.driver.title}'")
                return True
        except TimeoutException:
            pass
        return False

    def navigate_to_hpos(self):
        """From My Services page, click 'Go to service' for HPOS."""
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

        self._switch_to_new_window(original_handles)

        try:
            self._wait(
                EC.title_contains("Health Professional Online Services"),
                timeout=self.page_timeout
            )
            self._wait_for_page_load()
            log(f"Reached HPOS landing page ({self.driver.current_url})")
        except TimeoutException:
            raise NavigationError(
                f"Failed to reach HPOS. title='{self.driver.title}' "
                f"url='{self.driver.current_url}'"
            )

    def navigate_to_mbs_checker(self):
        """Navigate directly to MBS Items Online Checker via redirect URL."""
        log("Navigating to MBS Items Online Checker")
        self.driver.get(_HPOS_MBS_URL)

        try:
            self._wait_for_page_load(timeout=self.page_timeout * 2)
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
        """Execute the full navigation: My Services -> HPOS -> MBS Checker."""
        self.navigate_to_hpos()
        self.navigate_to_mbs_checker()

    def _dump_menu_links(self):
        """Log page links for debugging navigation failures."""
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
