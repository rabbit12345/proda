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
        """Step 7: Select MBS items using the search field."""
        if items is None:
            items = self.config.mbs.items_to_check

        if len(items) > 5:
            raise MbsCheckerError("Maximum of 5 MBS items can be selected")

        log(f"Selecting MBS items: {items}")

        for item_number in items:
            self._select_single_item(item_number)

        log(f"All {len(items)} MBS items selected")

    def _select_single_item(self, item_number: str):
        """Select a single MBS item by searching and clicking its checkbox."""
        # Strip leading zeros for search (form says "leading zeros not required")
        search_term = item_number.lstrip("0") or item_number

        log(f"Searching for MBS item: {item_number} (search: {search_term})")

        # Clear and type in search field
        search_field = self._wait(EC.element_to_be_clickable(
            (By.ID, "guiForm:searchItemNumber")
        ))
        search_field.clear()
        time.sleep(self.ajax_delay)

        # Type the search term character by character to trigger AJAX keyup
        for char in search_term:
            search_field.send_keys(char)
            time.sleep(0.3)

        # Wait for AJAX to update the tab view with filtered results
        time.sleep(self.ajax_delay)

        # Find and click the checkbox for this item
        try:
            checkbox = self._find_item_checkbox(item_number)
            if not checkbox.is_selected():
                self.driver.execute_script("arguments[0].click();", checkbox)

            # Wait for selected items panel to update
            time.sleep(self.ajax_delay)
            log(f"Selected MBS item: {item_number}")
        except (TimeoutException, NoSuchElementException):
            raise MbsCheckerError(
                f"Could not find or select MBS item: {item_number}"
            )

        # Clear search field for next item
        search_field = self.driver.find_element(
            By.ID, "guiForm:searchItemNumber"
        )
        search_field.clear()
        # Trigger the clear button via JS to reset the AJAX filter
        try:
            clear_btn = self.driver.find_element(By.ID, "guiForm:clearBtn")
            self.driver.execute_script("arguments[0].click();", clear_btn)
        except NoSuchElementException:
            pass
        time.sleep(self.ajax_delay)

    def _find_item_checkbox(self, item_number: str):
        """Find the checkbox for a specific MBS item number."""
        # Pad item number to match label format (e.g., "965" -> "00965")
        padded = item_number.zfill(5)

        # Try to find label with matching text, then get its associated checkbox
        try:
            label = self._wait(EC.presence_of_element_located((
                By.XPATH,
                f"//div[@id='guiForm:tabView']//label[contains(@class, 'label-normal') and normalize-space(text())='{padded}']"
            )))
            checkbox_id = label.get_attribute("for")
            return self.driver.find_element(By.ID, checkbox_id)
        except TimeoutException:
            # Fallback: try with the original number format
            label = self._wait(EC.presence_of_element_located((
                By.XPATH,
                f"//div[@id='guiForm:tabView']//label[contains(@class, 'label-normal') and normalize-space(text())='{item_number}']"
            )))
            checkbox_id = label.get_attribute("for")
            return self.driver.find_element(By.ID, checkbox_id)

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
