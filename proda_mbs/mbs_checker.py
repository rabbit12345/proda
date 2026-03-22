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
TAB_RANGE_PATTERN = re.compile(r"(\d+)\s*-\s*(\d+)")

# Form element IDs
_FORM_IDS = [
    "guiForm:guiMedicareCardNumber",
    "guiForm:guiIndividualReferenceNumber",
    "guiForm:guiFirstName",
    "guiForm:gui_patientConsentGiven",
    "guiForm:gui_providerLocation",
]

# Tab view CSS selectors (tried in order)
_TAB_NAV_CSS = (
    "div[id*='tabView'] ul.ui-tabs-nav li a, "
    ".ui-tabs-nav li a, "
    "[role='tablist'] li a, "
    ".ui-tabs .ui-tabs-nav a"
)


class MbsCheckerError(Exception):
    pass


class MbsChecker:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self.ajax_delay = config.session.ajax_stability_delay
        self._current_tab_text = None

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

    def _wait_for_ajax(self, timeout=None):
        """Wait for PrimeFaces AJAX queue to be empty."""
        timeout = timeout or self.wait_timeout
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script(
                    "return typeof PrimeFaces === 'undefined' || "
                    "PrimeFaces.ajax.Queue.isEmpty()"
                )
            )
        except Exception:
            pass

    def _wait_for_page_ready(self, timeout=None):
        """Wait for DOM complete + AJAX idle + all form elements clickable."""
        timeout = timeout or self.config.session.page_load_timeout * 2
        log("Waiting for page to be fully loaded...")

        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        self._wait_for_ajax(timeout)

        for el_id in _FORM_IDS:
            self._wait(EC.element_to_be_clickable((By.ID, el_id)), timeout=timeout)

        self._wait(
            EC.presence_of_element_located((By.CSS_SELECTOR, _TAB_NAV_CSS)),
            timeout=timeout
        )

        log("Page fully loaded and ready")

    # -- Patient form ---------------------------------------------------------

    def fill_patient_form(self, medicare_number: str, irn: str, first_name: str):
        if not MEDICARE_PATTERN.match(medicare_number):
            raise MbsCheckerError(
                f"Invalid Medicare number '{medicare_number}': must be exactly 10 digits"
            )
        if not IRN_PATTERN.match(irn):
            raise MbsCheckerError(f"Invalid IRN '{irn}': must be a single digit")
        if not first_name.strip():
            raise MbsCheckerError("First name cannot be empty")

        log(f"Filling patient form: {first_name}")

        mc_field = self._wait(EC.element_to_be_clickable(
            (By.ID, "guiForm:guiMedicareCardNumber")
        ))
        mc_field.clear()
        mc_field.send_keys(medicare_number)

        irn_field = self.driver.find_element(
            By.ID, "guiForm:guiIndividualReferenceNumber"
        )
        irn_field.clear()
        irn_field.send_keys(irn)

        name_field = self.driver.find_element(By.ID, "guiForm:guiFirstName")
        name_field.clear()
        name_field.send_keys(first_name)

        consent_cb = self.driver.find_element(
            By.ID, "guiForm:gui_patientConsentGiven"
        )
        if not consent_cb.is_selected():
            self.driver.execute_script("arguments[0].click();", consent_cb)

        location_select = Select(
            self.driver.find_element(By.ID, "guiForm:gui_providerLocation")
        )
        location_select.select_by_value(self.config.mbs.provider_location)

        log("Patient form filled")

    # -- MBS item selection ---------------------------------------------------

    def select_mbs_items(self, items: Optional[List[str]] = None):
        """Select MBS items by navigating to correct tab and clicking checkboxes."""
        if items is None:
            items = self.config.mbs.items_to_check
        if len(items) > 5:
            raise MbsCheckerError("Maximum of 5 MBS items can be selected")

        log(f"Selecting MBS items: {items}")
        self._current_tab_text = None

        for item_number in items:
            self._select_single_item(item_number)

        log(f"All {len(items)} MBS items selected")

    def _get_tab_ranges(self) -> List[Dict]:
        """Parse tab headers fresh to avoid stale element references."""
        tabs = []
        tab_links = self.driver.find_elements(By.CSS_SELECTOR, _TAB_NAV_CSS)

        if not tab_links:
            tab_links = self.driver.find_elements(
                By.XPATH,
                "//a[contains(text(), '-') and string-length(text()) < 20]"
            )

        for link in tab_links:
            text = link.text.strip()
            match = TAB_RANGE_PATTERN.match(text)
            if match:
                tabs.append({
                    "element": link,
                    "text": text,
                    "low": int(match.group(1)),
                    "high": int(match.group(2)),
                })
        return tabs

    def _switch_to_tab(self, padded: str):
        """Switch to the tab containing the item. Skips if already active."""
        item_num = int(padded)
        tabs = self._get_tab_ranges()

        if not tabs:
            log("Warning: no tabs found")
            return

        target = None
        for tab in tabs:
            if tab["low"] <= item_num <= tab["high"]:
                target = tab
                break

        if not target:
            log(f"Item {padded} ({item_num}) not in any tab range")
            return

        if self._current_tab_text == target["text"]:
            return

        log(f"Switching to tab: {target['text']}")
        try:
            target["element"].click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", target["element"])

        self._wait_for_ajax()
        self._current_tab_text = target["text"]

    def _select_single_item(self, item_number: str):
        padded = item_number.zfill(5)
        log(f"Selecting MBS item: {padded}")

        self._switch_to_tab(padded)

        try:
            self._click_item_checkbox(padded)
            log(f"Selected MBS item: {padded}")
        except NoSuchElementException:
            log(f"Item {padded} not found in expected tab, searching all tabs")
            if not self._search_all_tabs(padded):
                raise MbsCheckerError(f"Could not find MBS item {padded} in any tab")

    def _click_item_checkbox(self, padded_item: str):
        """Find and click the checkbox for a specific MBS item number."""
        # Try label-based lookup first (most common)
        for xpath in [
            f"//label[normalize-space(text())='{padded_item}']",
            f"//label[contains(text(), '{padded_item}')]",
        ]:
            try:
                label = self.driver.find_element(By.XPATH, xpath)
                checkbox_id = label.get_attribute("for")
                if checkbox_id:
                    checkbox = self.driver.find_element(By.ID, checkbox_id)
                    if not checkbox.is_selected():
                        self.driver.execute_script("arguments[0].click();", checkbox)
                    self._wait_for_ajax()
                    return
            except NoSuchElementException:
                continue

        # Fallback: checkbox next to text cell
        xpath = (f"//td[normalize-space(text())='{padded_item}']"
                 f"/preceding-sibling::td//input[@type='checkbox']")
        try:
            checkbox = self.driver.find_element(By.XPATH, xpath)
            if not checkbox.is_selected():
                self.driver.execute_script("arguments[0].click();", checkbox)
            self._wait_for_ajax()
            return
        except NoSuchElementException:
            pass

        raise NoSuchElementException(f"Checkbox for item {padded_item} not found")

    def _search_all_tabs(self, padded_item: str) -> bool:
        """Search all tabs to find and select an item."""
        tabs = self._get_tab_ranges()
        for i in range(len(tabs)):
            try:
                fresh_tabs = self._get_tab_ranges()
                if i >= len(fresh_tabs):
                    break
                fresh_tabs[i]["element"].click()
                self._wait_for_ajax()
                self._click_item_checkbox(padded_item)
                self._current_tab_text = fresh_tabs[i]["text"]
                log(f"Found and selected {padded_item} in tab {fresh_tabs[i]['text']}")
                return True
            except (NoSuchElementException, TimeoutException):
                continue
        return False

    # -- Submit and results ---------------------------------------------------

    def submit_check(self):
        """Click 'Check items' and handle confirmation dialog."""
        log("Submitting check items")

        try:
            check_btn = self._wait(EC.element_to_be_clickable((
                By.XPATH,
                "//div[@id='guiForm:buttonsAndModal']"
                "//input[@type='submit' and contains(@title, 'Check')]"
            )))
            check_btn.click()
        except TimeoutException:
            hidden_btn = self.driver.find_element(By.ID, "guiForm:gui_search3")
            self.driver.execute_script("arguments[0].click();", hidden_btn)

        self._wait_for_ajax()

        # Handle multiple items confirmation dialog
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
                self._wait_for_ajax()
        except NoSuchElementException:
            pass

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
        log("Extracting results")
        table = self.driver.find_element(
            By.ID, "guiForm:guiMbsItemNumberSearchResults"
        )
        tbody = table.find_element(
            By.ID, "guiForm:guiMbsItemNumberSearchResults:tbody_element"
        )

        results = []
        for row in tbody.find_elements(By.TAG_NAME, "tr"):
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
            btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "guiForm:gui_searchAgain")
            ))
            btn.click()
            self._wait_for_ajax()
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
        self._wait_for_page_ready()
        self.fill_patient_form(medicare_number, irn, first_name)
        self.select_mbs_items(items)
        self.submit_check()
        return self.extract_results()


def format_results(
    medicare_number: str, name: str, results: List[Dict[str, str]]
) -> str:
    lines = [
        "",
        "=" * 60,
        f"Patient: {name}  |  Medicare: {medicare_number}",
        "-" * 60,
        f"{'MBS Item':<12} {'Claimable':<15} {'Response'}",
        "-" * 60,
    ]
    for r in results:
        response = r["response"]
        if len(response) > 50:
            response = response[:47] + "..."
        lines.append(f"{r['mbs_item']:<12} {r['claimable']:<15} {response}")
    lines.append("=" * 60)
    return "\n".join(lines)
