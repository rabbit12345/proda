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

    def _switch_to_content_frame(self):
        """Try switching into an iframe if the page uses one for content."""
        try:
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            if not iframes:
                iframes = self.driver.find_elements(By.TAG_NAME, "frame")
            if iframes:
                log(f"Found {len(iframes)} iframe(s), switching to first")
                for i, frame in enumerate(iframes):
                    src = frame.get_attribute("src") or ""
                    name = frame.get_attribute("name") or ""
                    log(f"  frame[{i}]: name='{name}' src='{src[:100]}'")
                self.driver.switch_to.frame(iframes[0])
                log(f"Switched to iframe. Content title check skipped.")
                return True
        except Exception as e:
            log(f"Frame switch failed: {e}")
        return False

    def navigate_to_hpos(self):
        """Step 4: From My Services page, click 'Go to service' for HPOS."""
        log("Navigating to HPOS from My Services")
        try:
            # Wait for My Services page
            self._wait(EC.title_contains("My Services"))

            original_handles = self.driver.window_handles

            # Click HPOS "Go to service" link
            # Try multiple selectors - the JSF ID can vary
            hpos_selectors = [
                (By.XPATH, "//a[contains(@onclick, 'j_id_2_2_1n:0:j_id_2_2_1s')]"),
                (By.XPATH, "//a[contains(text(), 'Go to service')]"),
                (By.XPATH, "//a[contains(text(), 'HPOS')]"),
                (By.XPATH, "//button[contains(text(), 'Go to service')]"),
                (By.CSS_SELECTOR, "a.go-to-service"),
            ]

            clicked = False
            for by, selector in hpos_selectors:
                try:
                    hpos_link = WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    log(f"Found HPOS link with: {selector}")
                    hpos_link.click()
                    clicked = True
                    break
                except TimeoutException:
                    continue

            if not clicked:
                raise NavigationError("Could not find HPOS 'Go to service' link")

            # HPOS may open in a new window/tab
            self._switch_to_new_window(original_handles)

            # Wait for HPOS landing page to load
            self._wait(
                EC.title_contains("Health Professional Online Services"),
                timeout=self.page_timeout
            )
            log(f"Reached HPOS landing page ({self.driver.current_url})")
        except TimeoutException:
            log(f"HPOS page load timeout. title='{self.driver.title}' url='{self.driver.current_url}'")
            raise NavigationError(
                "Failed to navigate from My Services to HPOS"
            )

    def _dump_menu_links(self):
        """Log all links on the page for debugging navigation."""
        try:
            # Also check for iframes we might not be inside
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            frames = self.driver.find_elements(By.TAG_NAME, "frame")
            log(f"Page has {len(iframes)} iframes, {len(frames)} frames")
            for i, f in enumerate(iframes + frames):
                log(f"  frame[{i}]: src='{f.get_attribute('src') or ''}' "
                    f"name='{f.get_attribute('name') or ''}'")

            links = self.driver.find_elements(By.TAG_NAME, "a")
            log(f"Found {len(links)} links on page (showing all with text):")
            for link in links:
                href = link.get_attribute("href") or ""
                text = link.text.strip()
                if text or "ITEM" in href.upper() or "MBS" in href.upper():
                    log(f"  text='{text}' href='{href[:120]}'")
        except Exception as e:
            log(f"  (could not dump links: {e})")

    def navigate_to_mbs_checker(self):
        """Step 5: From HPOS landing page, navigate to MBS Items Online Checker."""
        log("Navigating to MBS Items Online Checker")
        try:
            self._wait(EC.title_contains("Health Professional Online Services"))
        except TimeoutException:
            raise NavigationError(
                f"Not on HPOS page. title='{self.driver.title}'"
            )

        log(f"Current URL: {self.driver.current_url}")

        # Navigate directly to MBS Items Online Checker via securityRedirect URL.
        # The HPOS menu links all go through this redirect pattern.
        mbs_url = (
            "https://www2.medicareaustralia.gov.au:5447"
            "/pcert/hpos/securityRedirect.do?target=HPOS.NAVMENU.ITEM.MBSIOC"
        )
        log(f"Navigating directly to MBS checker URL")
        self.driver.get(mbs_url)

        # Wait for MBS checker form to load (allow extra time for redirect)
        try:
            self._wait(
                EC.presence_of_element_located(
                    (By.ID, "guiForm:guiMedicareCardNumber")
                ),
                timeout=self.page_timeout * 2
            )
            log(f"Reached MBS Items Online Checker page ({self.driver.current_url})")
        except TimeoutException:
            log(f"MBS form not found. title='{self.driver.title}' "
                f"url='{self.driver.current_url}'")
            self._dump_menu_links()
            raise NavigationError(
                "Failed to load MBS Items Online Checker form"
            )

    def navigate_to_mbs_checker_full(self):
        """Execute the full navigation: My Services -> HPOS -> MBS Checker."""
        self.navigate_to_hpos()
        self.navigate_to_mbs_checker()
