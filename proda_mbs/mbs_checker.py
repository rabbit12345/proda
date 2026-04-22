from __future__ import annotations

import re
from typing import List, Dict, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from .config import AppConfig
from .waits import wait_for_ajax, wait_for_page_load, log


MEDICARE_PATTERN = re.compile(r"^\d{10}$")
IRN_PATTERN = re.compile(r"^\d$")
TAB_RANGE_PATTERN = re.compile(r"(\d+)\s*-\s*(\d+)")

_FORM_IDS = [
    "guiForm:guiMedicareCardNumber",
    "guiForm:guiIndividualReferenceNumber",
    "guiForm:guiFirstName",
    "guiForm:gui_patientConsentGiven",
    "guiForm:gui_providerLocation",
]

_TAB_NAV_CSS = (
    "div[id*='tabView'] ul.ui-tabs-nav li a, "
    ".ui-tabs-nav li a, "
    "[role='tablist'] li a, "
    ".ui-tabs .ui-tabs-nav a"
)


class MbsCheckerError(Exception):
    pass


class InvalidPatientError(MbsCheckerError):
    """Raised when the portal rejects patient details (e.g. invalid Medicare number)."""
    pass


_PATIENT_ERROR_PATTERNS = [
    "medicare card number entered is not valid",
    "individual reference number is not valid",
    "please check the details and try again",
    "patient details are not valid",
]


class MbsChecker:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self._current_tab_text = None

    def _wait(self, condition, timeout=None):
        return WebDriverWait(self.driver, timeout or self.wait_timeout).until(condition)

    def _wait_for_page_ready(self, timeout=None):
        """Wait for DOM + AJAX idle + all form elements clickable + tabs present."""
        timeout = timeout or self.wait_timeout
        log("Waiting for page to be fully loaded...")

        wait_for_page_load(self.driver, timeout)
        wait_for_ajax(self.driver, timeout)

        for el_id in _FORM_IDS:
            self._wait(EC.element_to_be_clickable((By.ID, el_id)), timeout=timeout)

        self._wait(
            EC.presence_of_element_located((By.CSS_SELECTOR, _TAB_NAV_CSS)),
            timeout=timeout
        )

        log("Page fully loaded and ready")

    def _set_field_value(self, element, value: str):
        """Set a form field value via JS and dispatch events so PrimeFaces
        JSF validation and AJAX listeners fire reliably."""
        self.driver.execute_script(
            "arguments[0].value = arguments[1]", element, value
        )
        self.driver.execute_script("""
            var el = arguments[0];
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
        """, element)

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
        mc_field.click()
        self._set_field_value(mc_field, medicare_number)

        irn_field = self.driver.find_element(
            By.ID, "guiForm:guiIndividualReferenceNumber"
        )
        irn_field.click()
        self._set_field_value(irn_field, irn)

        name_field = self.driver.find_element(By.ID, "guiForm:guiFirstName")
        name_field.click()
        self._set_field_value(name_field, first_name)

        consent_cb = self.driver.find_element(
            By.ID, "guiForm:gui_patientConsentGiven"
        )
        if not consent_cb.is_selected():
            self.driver.execute_script("arguments[0].click();", consent_cb)
            wait_for_ajax(self.driver)

        location_select = Select(
            self.driver.find_element(By.ID, "guiForm:gui_providerLocation")
        )
        location_select.select_by_value(self.config.mbs.provider_location)
        wait_for_ajax(self.driver)

        log("Patient form filled")

    # -- MBS item selection ---------------------------------------------------

    def select_mbs_items(self, items: Optional[List[str]] = None):
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

        wait_for_ajax(self.driver)

        # Verify tab content loaded by checking for checkboxes in the active panel
        try:
            self._wait(
                lambda d: len(d.find_elements(By.CSS_SELECTOR,
                    "div.ui-tabs-panel:not(.ui-helper-hidden) input[type='checkbox']"
                )) > 0,
                timeout=5
            )
        except TimeoutException:
            log("Warning: no checkboxes found in active tab panel")

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
                    wait_for_ajax(self.driver)
                    return
            except NoSuchElementException:
                continue

        xpath = (f"//td[normalize-space(text())='{padded_item}']"
                 f"/preceding-sibling::td//input[@type='checkbox']")
        try:
            checkbox = self.driver.find_element(By.XPATH, xpath)
            if not checkbox.is_selected():
                self.driver.execute_script("arguments[0].click();", checkbox)
            wait_for_ajax(self.driver)
            return
        except NoSuchElementException:
            pass

        raise NoSuchElementException(f"Checkbox for item {padded_item} not found")

    def _search_all_tabs(self, padded_item: str) -> bool:
        i = 0
        while True:
            try:
                fresh_tabs = self._get_tab_ranges()
                if i >= len(fresh_tabs):
                    break
                fresh_tabs[i]["element"].click()
                wait_for_ajax(self.driver)
                self._click_item_checkbox(padded_item)
                self._current_tab_text = fresh_tabs[i]["text"]
                log(f"Found and selected {padded_item} in tab {fresh_tabs[i]['text']}")
                return True
            except (NoSuchElementException, TimeoutException):
                i += 1
                continue
        return False

    # -- Submit and results ---------------------------------------------------

    def submit_check(self):
        log("Submitting check items")

        # Ensure AJAX from item selection is complete
        wait_for_ajax(self.driver)

        # Find button — try visible XPath first, fall back to hidden ID
        try:
            btn = self._wait(EC.presence_of_element_located((
                By.XPATH,
                "//div[@id='guiForm:buttonsAndModal']"
                "//input[@type='submit' and contains(@title, 'Check')]"
            )))
        except TimeoutException:
            try:
                btn = self.driver.find_element(By.ID, "guiForm:gui_search3")
            except NoSuchElementException:
                raise MbsCheckerError("Check button not found on page")

        # Scroll into view and click via JS (bypasses PrimeFaces overlay issues)
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", btn
        )
        self.driver.execute_script("arguments[0].click();", btn)
        log("Check button clicked (JS)")

        # Wait for any outcome: results table, no-results label,
        # confirmation dialog, or validation error text.
        # The portal responds instantly so we just need to detect
        # whichever DOM change appears first.
        timeout = self.config.session.page_load_timeout
        try:
            outcome = self._wait(
                lambda d: self._detect_submit_outcome(d),
                timeout=timeout
            )
        except TimeoutException:
            self._check_patient_validation_error()
            raise MbsCheckerError("No response from portal after submission")

        if outcome == "dialog":
            log("Multiple items confirmation dialog - clicking Continue")
            continue_btn = self._wait(EC.element_to_be_clickable((
                By.CSS_SELECTOR,
                "#guiForm\\:multipleMBSItemsConfirmDiag a.confirm-btn"
            )))
            continue_btn.click()
            # After dialog, wait for results or no-results
            try:
                self._wait(
                    lambda d: (
                        d.find_elements(By.ID, "guiForm:guiMbsItemNumberSearchResults")
                        or self._is_no_results_visible(d)
                    ),
                    timeout=timeout
                )
            except TimeoutException:
                self._check_patient_validation_error()
                raise MbsCheckerError("Results table did not appear after confirmation")

        self._check_patient_validation_error()
        log("Results table loaded")

    def _detect_submit_outcome(self, driver):
        """Return a truthy string indicating which outcome appeared after submit."""
        if driver.find_elements(By.ID, "guiForm:guiMbsItemNumberSearchResults"):
            return "results"
        if self._is_no_results_visible(driver):
            return "no_results"
        try:
            dialog = driver.find_element(By.ID, "guiForm:multipleMBSItemsConfirmDiag")
            if dialog.is_displayed():
                return "dialog"
        except NoSuchElementException:
            pass
        # Check for validation error text without waiting
        try:
            body = driver.find_element(By.TAG_NAME, "body").text.lower()
            for pattern in _PATIENT_ERROR_PATTERNS:
                if pattern in body:
                    return "validation_error"
        except Exception:
            pass
        return ""

    @staticmethod
    def _is_no_results_visible(driver) -> bool:
        """Check if the 'No results found' label is visible on the page."""
        try:
            el = driver.find_element(By.ID, "guiForm:noResultsLabel")
            return el.is_displayed()
        except NoSuchElementException:
            return False

    def _check_patient_validation_error(self):
        """Raise InvalidPatientError if the portal shows a validation message
        or the 'No results found' label is visible.  Clears patient fields
        (but leaves MBS item selection intact) before raising."""
        error_msg = None

        if self._is_no_results_visible(self.driver):
            error_msg = "No results found — check patient details"

        if not error_msg:
            try:
                body = self.driver.find_element(By.TAG_NAME, "body").text.lower()
                for pattern in _PATIENT_ERROR_PATTERNS:
                    if pattern in body:
                        error_msg = pattern
                        break
            except Exception:
                pass

        if error_msg:
            log(f"Portal validation error: {error_msg}")
            self._clear_patient_fields()
            raise InvalidPatientError(error_msg)

    def _clear_patient_fields(self):
        """Clear Medicare, IRN and name fields via JS to avoid stale element
        references after AJAX DOM replacement.  Leaves MBS item selection intact."""
        self.driver.execute_script("""
            var ids = [
                'guiForm:guiMedicareCardNumber',
                'guiForm:guiIndividualReferenceNumber',
                'guiForm:guiFirstName'
            ];
            ids.forEach(function(id) {
                var el = document.getElementById(id);
                if (el) {
                    el.value = '';
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }
            });
        """)
        log("Patient fields cleared (item selection preserved)")

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
        log("Starting new check")
        try:
            btn = self._wait(EC.element_to_be_clickable(
                (By.ID, "guiForm:gui_searchAgain")
            ))
            btn.click()
            wait_for_ajax(self.driver)
            self._wait(EC.element_to_be_clickable(
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
        try:
            self._wait_for_page_ready()
            self.fill_patient_form(medicare_number, irn, first_name)
            self.select_mbs_items(items)
            self.submit_check()
            return self.extract_results()
        except MbsCheckerError:
            raise
        except TimeoutException as e:
            raise MbsCheckerError(f"Page timed out during check: {e.msg or 'timeout'}") from e


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
