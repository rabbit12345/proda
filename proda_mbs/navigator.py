import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from .config import AppConfig


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


class NavigationError(Exception):
    """Raised when page navigation fails."""
    pass


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

    def navigate_to_hpos(self):
        """Step 4: From My Services page, click 'Go to service' for HPOS."""
        log("Navigating to HPOS from My Services")
        try:
            # Wait for My Services page
            self._wait(EC.title_contains("My Services"))

            # Click HPOS "Go to service" link
            # The link uses JSF form submission via onclick
            hpos_link = self._wait(EC.element_to_be_clickable((
                By.XPATH,
                "//a[contains(@onclick, 'j_id_2_2_1n:0:j_id_2_2_1s')]"
            )))
            hpos_link.click()

            # Wait for HPOS landing page to load
            self._wait(
                EC.title_contains("Health Professional Online Services"),
                timeout=self.page_timeout
            )
            log("Reached HPOS landing page")
        except TimeoutException:
            raise NavigationError(
                "Failed to navigate from My Services to HPOS"
            )

    def navigate_to_mbs_checker(self):
        """Step 5: From HPOS landing page, navigate to MBS Items Online Checker."""
        log("Navigating to MBS Items Online Checker")
        try:
            # Wait for HPOS page
            self._wait(EC.title_contains("Health Professional Online Services"))

            # Click MBS items online checker link in the sidebar menu
            # The link href contains "MBSIOC"
            mbs_link = self._wait(EC.element_to_be_clickable((
                By.CSS_SELECTOR, 'a[href*="MBSIOC"]'
            )))
            mbs_link.click()

            # Wait for MBS checker form to load
            self._wait(
                EC.presence_of_element_located(
                    (By.ID, "guiForm:guiMedicareCardNumber")
                ),
                timeout=self.page_timeout
            )
            log("Reached MBS Items Online Checker page")
        except TimeoutException:
            # Fallback: try expanding the Items menu section first
            try:
                log("Attempting to expand Items menu section")
                items_menu = self._wait(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, 'a[href*="HPOS.NAVMENU.ITEM.ITEMS"]'
                )))
                items_menu.click()
                time.sleep(1)

                mbs_link = self._wait(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, 'a[href*="MBSIOC"]'
                )))
                mbs_link.click()

                self._wait(
                    EC.presence_of_element_located(
                        (By.ID, "guiForm:guiMedicareCardNumber")
                    ),
                    timeout=self.page_timeout
                )
                log("Reached MBS Items Online Checker page (via menu expand)")
            except TimeoutException:
                raise NavigationError(
                    "Failed to navigate to MBS Items Online Checker"
                )

    def navigate_to_mbs_checker_full(self):
        """Execute the full navigation: My Services -> HPOS -> MBS Checker."""
        self.navigate_to_hpos()
        self.navigate_to_mbs_checker()
