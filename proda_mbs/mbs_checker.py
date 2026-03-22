import time
import re
from typing import List, Dict, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .config import AppConfig


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


MEDICARE_PATTERN = re.compile(r"^\d{10}$")
IRN_PATTERN = re.compile(r"^\d$")


class MbsCheckerError(Exception):
    """Raised when MBS checker operations fail."""
    pass


class MbsChecker:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self.ajax_delay = config.session.ajax_stability_delay

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

    def wait_for_page_ready(self, timeout=None):
        """Wait until the MBS checker page is fully loaded and interactive.
        Checks: DOM ready, no active AJAX, all key form elements present and enabled."""
        timeout = timeout or self.config.session.page_load_timeout * 2
        log("Waiting for MBS checker page to be fully loaded...")

        # 1. Wait for document.readyState == 'complete'
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        log("DOM ready")

        # 2. Wait for PrimeFaces AJAX to finish (if PrimeFaces is present)
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script(
                    "return typeof PrimeFaces === 'undefined' || "
                    "PrimeFaces.ajax.Queue.isEmpty()"
                )
            )
            log("No active AJAX requests")
        except Exception:
            # PrimeFaces might not be available, continue anyway
            pass

        # 3. Wait for all key form elements to be present and interactable
        key_elements = [
            "guiForm:guiMedicareCardNumber",
            "guiForm:guiIndividualReferenceNumber",
            "guiForm:guiFirstName",
            "guiForm:gui_patientConsentGiven",
            "guiForm:gui_providerLocation",
        ]
        for el_id in key_elements:
            self._wait(EC.element_to_be_clickable((By.ID, el_id)), timeout=timeout)

        # 4. Wait for the tab view with MBS items to be present
        self._wait(
            EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "div[id*='tabView'] ul.ui-tabs-nav, "
                ".ui-tabs-nav, [role='tablist']"
            )),
            timeout=timeout
        )

        # 5. Small settle delay for any remaining JS rendering
        time.sleep(1)
        log("MBS checker page fully loaded and ready")

    def fill_patient_form(
        self, medicare_number: str, irn: str, first_name: str
    ):
        """Step 6: Fill the patient details form."""
        # Validate inputs
        if not MEDICARE_PATTERN.match(medicare_number):
            raise MbsCheckerError(
                f"Invalid Medicare number '{medicare_number}': "
                "must be exactly 10 digits"
            )
        if not IRN_PATTERN.match(irn):
            raise MbsCheckerError(
                f"Invalid IRN '{irn}': must be a single digit"
            )
        if not first_name.strip():
            raise MbsCheckerError("First name cannot be empty")

        log(f"Filling patient form: {first_name}")

        # Medicare card number
        mc_field = self._wait(EC.element_to_be_clickable(
            (By.ID, "guiForm:guiMedicareCardNumber")
        ))
        mc_field.clear()
        mc_field.send_keys(medicare_number)

        # Individual reference number
        irn_field = self.driver.find_element(
            By.ID, "guiForm:guiIndividualReferenceNumber"
        )
        irn_field.clear()
        irn_field.send_keys(irn)

        # First name
        name_field = self.driver.find_element(By.ID, "guiForm:guiFirstName")
        name_field.clear()
        name_field.send_keys(first_name)

        # Declaration checkbox
        consent_cb = self.driver.find_element(
            By.ID, "guiForm:gui_patientConsentGiven"
        )
        if not consent_cb.is_selected():
            self.driver.execute_script("arguments[0].click();", consent_cb)

        # Provider location dropdown
        location_select = Select(
            self.driver.find_element(By.ID, "guiForm:gui_providerLocation")
        )
        location_select.select_by_value(self.config.mbs.provider_location)

        log("Patient form filled")

    def select_mbs_items(self, items: Optional[List[str]] = None):
        """Step 7: Select MBS items by navigating tabs and clicking checkboxes.

        The MBS items UI has tabs organized by number ranges (e.g. 00104-00699,
        00701-10970). Each tab contains checkboxes with item numbers. We need to:
        1. Find which tab contains the item number
        2. Click that tab to show its checkboxes
        3. Check the checkbox for the item
        """
        if items is None:
            items = self.config.mbs.items_to_check

        if len(items) > 5:
            raise MbsCheckerError("Maximum of 5 MBS items can be selected")

        log(f"Selecting MBS items: {items}")

        self._current_tab_text = None  # Track which tab is active

        for item_number in items:
            self._select_single_item(item_number)

        log(f"All {len(items)} MBS items selected")

    def _get_tab_ranges(self) -> List[Dict]:
        """Re-parse tab headers fresh each time to avoid stale element references."""
        tabs = []
        tab_links = self.driver.find_elements(
            By.CSS_SELECTOR, "div[id*='tabView'] ul.ui-tabs-nav li a, "
            ".ui-tabs-nav li a, "
            "[role='tablist'] li a, "
            ".ui-tabs .ui-tabs-nav a"
        )

        if not tab_links:
            tab_links = self.driver.find_elements(
                By.XPATH,
                "//a[contains(text(), '-') and string-length(text()) < 20]"
            )

        for link in tab_links:
            text = link.text.strip()
            match = re.match(r"(\d+)\s*-\s*(\d+)", text)
            if match:
                tabs.append({
                    "element": link,
                    "text": text,
                    "low": int(match.group(1)),
                    "high": int(match.group(2)),
                })

        return tabs

    def _find_tab_for_item(self, item_num: int, tabs: List[Dict]):
        """Find which tab contains the given item number."""
        for tab in tabs:
            if tab["low"] <= item_num <= tab["high"]:
                return tab
        return None

    def _click_tab_for_item(self, padded: str):
        """Navigate to the correct tab for an item. Skips clicking if the
        correct tab is already active to avoid AJAX reload that resets checkboxes."""
        item_num = int(padded)
        tabs = self._get_tab_ranges()

        if not tabs:
            log("Warning: no tabs found")
            return tabs

        tab = self._find_tab_for_item(item_num, tabs)
        if tab:
            # Skip clicking if we're already on this tab
            if self._current_tab_text == tab["text"]:
                log(f"Item {padded} is in current tab: {tab['text']} (already active)")
                return tabs

            log(f"Item {padded} is in tab: {tab['text']} (switching)")
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView(true);", tab["element"]
                )
                time.sleep(0.3)
                tab["element"].click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", tab["element"])
            time.sleep(self.ajax_delay)
            self._current_tab_text = tab["text"]
        else:
            log(f"Item {padded} ({item_num}) not in any tab range")

        return tabs

    def _select_single_item(self, item_number: str):
        """Select a single MBS item by navigating to the correct tab."""
        padded = item_number.zfill(5)
        log(f"Selecting MBS item: {padded}")

        # Click the correct tab (re-fetches tabs fresh each time)
        tabs = self._click_tab_for_item(padded)

        # Find the checkbox for this item number
        try:
            self._click_item_checkbox(padded)
            log(f"Selected MBS item: {padded}")
        except (TimeoutException, NoSuchElementException):
            # Item not found in expected tab — search all tabs
            log(f"Item {padded} not found in expected tab, searching all tabs")
            if not self._search_all_tabs(padded):
                raise MbsCheckerError(
                    f"Could not find MBS item {padded} in any tab"
                )

    def _click_item_checkbox(self, padded_item: str):
        """Find and click the checkbox for a specific MBS item number."""
        selectors = [
            f"//label[normalize-space(text())='{padded_item}']",
            f"//label[contains(text(), '{padded_item}')]",
        ]

        for xpath in selectors:
            try:
                label = self.driver.find_element(By.XPATH, xpath)
                checkbox_id = label.get_attribute("for")
                if checkbox_id:
                    checkbox = self.driver.find_element(By.ID, checkbox_id)
                    if not checkbox.is_selected():
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView(true);", checkbox
                        )
                        time.sleep(0.2)
                        self.driver.execute_script(
                            "arguments[0].click();", checkbox
                        )
                    time.sleep(self.ajax_delay)
                    return
            except NoSuchElementException:
                continue

        # Fallback: checkbox next to text cell
        try:
            xpath = (f"//td[normalize-space(text())='{padded_item}']"
                     f"/preceding-sibling::td//input[@type='checkbox']")
            checkbox = self.driver.find_element(By.XPATH, xpath)
            if not checkbox.is_selected():
                self.driver.execute_script("arguments[0].click();", checkbox)
            time.sleep(self.ajax_delay)
            return
        except NoSuchElementException:
            pass

        raise NoSuchElementException(f"Checkbox for item {padded_item} not found")

    def _search_all_tabs(self, padded_item: str) -> bool:
        """Search through all tabs to find and select an item.
        Re-fetches tabs each iteration to avoid stale references."""
        tabs = self._get_tab_ranges()
        for i, tab in enumerate(tabs):
            try:
                # Re-fetch to avoid stale element after previous tab click
                fresh_tabs = self._get_tab_ranges()
                if i >= len(fresh_tabs):
                    break
                fresh_tabs[i]["element"].click()
                time.sleep(self.ajax_delay)
                self._click_item_checkbox(padded_item)
                log(f"Found and selected {padded_item} in tab {tab['text']}")
                return True
            except (NoSuchElementException, TimeoutException):
                continue
        return False

    def submit_check(self):
        """Step 8: Click 'Check items' and handle confirmation dialog."""
        log("Submitting check items")

        # First try: look for a visible "Check items" button
        try:
            check_btn = self._wait(EC.element_to_be_clickable((
                By.XPATH,
                "//div[@id='guiForm:buttonsAndModal']//input[@type='submit' and contains(@title, 'Check')]"
            )))
            check_btn.click()
        except TimeoutException:
            # Fallback: directly trigger the hidden button via JS
            hidden_btn = self.driver.find_element(
                By.ID, "guiForm:gui_search3"
            )
            self.driver.execute_script("arguments[0].click();", hidden_btn)

        # Check if confirmation dialog appeared (for multiple items)
        time.sleep(self.ajax_delay)
        try:
            dialog = self.driver.find_element(
                By.ID, "guiForm:multipleMBSItemsConfirmDiag"
            )
            if dialog.is_displayed():
                log("Multiple items confirmation dialog - clicking Continue")
                continue_btn = self._wait(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "#guiForm\\:multipleMBSItemsConfirmDiag a.confirm-btn"
                )))
                continue_btn.click()
        except NoSuchElementException:
            pass

        # Wait for results table to appear
        try:
            self._wait(
                EC.presence_of_element_located(
                    (By.ID, "guiForm:guiMbsItemNumberSearchResults")
                ),
                timeout=self.config.session.page_load_timeout
            )
            log("Results table loaded")
        except TimeoutException:
            raise MbsCheckerError("Results table did not appear after submission")

    def extract_results(self) -> List[Dict[str, str]]:
        """Step 9: Extract the results table data."""
        log("Extracting results")

        table = self.driver.find_element(
            By.ID, "guiForm:guiMbsItemNumberSearchResults"
        )
        tbody = table.find_element(
            By.ID, "guiForm:guiMbsItemNumberSearchResults:tbody_element"
        )
        rows = tbody.find_elements(By.TAG_NAME, "tr")

        results = []
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 3:
                results.append({
                    "mbs_item": cells[0].text.strip(),
                    "claimable": cells[1].text.strip(),
                    "response": cells[2].text.strip(),
                })

        log(f"Extracted {len(results)} results")
        return results

    def new_check(self):
        """Click 'New check' to reset the form for another patient."""
        log("Starting new check")
        try:
            new_check_btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "guiForm:gui_searchAgain")
            ))
            new_check_btn.click()

            # Wait for the form to reload
            self._wait(EC.presence_of_element_located(
                (By.ID, "guiForm:guiMedicareCardNumber")
            ))
            log("Form reset for new check")
        except TimeoutException:
            raise MbsCheckerError("Failed to reset form for new check")

    def check_patient(
        self,
        medicare_number: str,
        irn: str,
        first_name: str,
        items: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """Run the full check flow for a single patient."""
        self.wait_for_page_ready()
        self.fill_patient_form(medicare_number, irn, first_name)
        self.select_mbs_items(items)
        self.submit_check()
        return self.extract_results()


def format_results(
    medicare_number: str, name: str, results: List[Dict[str, str]]
) -> str:
    """Format results for console display."""
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"Patient: {name}  |  Medicare: {medicare_number}")
    lines.append("-" * 60)
    lines.append(f"{'MBS Item':<12} {'Claimable':<15} {'Response'}")
    lines.append("-" * 60)

    for r in results:
        response = r["response"]
        if len(response) > 50:
            response = response[:47] + "..."
        lines.append(f"{r['mbs_item']:<12} {r['claimable']:<15} {response}")

    lines.append("=" * 60)
    return "\n".join(lines)
