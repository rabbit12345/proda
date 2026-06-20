from __future__ import annotations

import re
from typing import List, Dict, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)

from .config import AppConfig
from .page_state import PageSnapshot, PageStateDetector, PortalPageState
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
    "the patients details could not be matched",
    "medicare card number entered is not valid",
    "individual reference number is not valid",
    "please check the details and try again",
    "patient details are not valid",
    "patient details do not match",
    "patient could not be matched",
    "patient not found",
    "no patient record",
    "unable to match patient",
]


class MbsChecker:
    def __init__(self, driver, config: AppConfig):
        self.driver = driver
        self.config = config
        self.wait_timeout = config.session.element_wait_timeout
        self.ajax_stability_delay = config.session.ajax_stability_delay
        self._current_tab_text = None
        self._tab_ranges_cache: List[Dict] | None = None
        self.state_detector = PageStateDetector(driver)

    def _wait(self, condition, timeout=None, poll_frequency=0.5):
        return WebDriverWait(
            self.driver, timeout or self.wait_timeout, poll_frequency=poll_frequency
        ).until(condition)

    def _describe_snapshot(self, snapshot: PageSnapshot) -> str:
        details = [f"state={snapshot.state.value}"]
        if snapshot.title:
            details.append(f"title='{snapshot.title}'")
        if snapshot.url:
            details.append(f"url='{snapshot.url}'")
        if snapshot.ready_state:
            details.append(f"readyState='{snapshot.ready_state}'")
        if snapshot.body_excerpt:
            details.append(f"body='{snapshot.body_excerpt[:160]}'")
        return " ".join(details)

    def _raise_for_terminal_state(
        self,
        snapshot: PageSnapshot,
        *,
        action: str,
        allow_mbs_states: bool,
    ):
        terminal_states = {
            PortalPageState.LOGIN,
            PortalPageState.OTP,
            PortalPageState.SESSION_EXPIRED,
            PortalPageState.LOGGED_OUT,
            PortalPageState.OFFSITE,
            PortalPageState.BROWSER_UNAVAILABLE,
            PortalPageState.UNKNOWN,
        }
        if snapshot.state in terminal_states:
            raise MbsCheckerError(
                f"{action} interrupted by portal state change: "
                f"{self._describe_snapshot(snapshot)}"
            )
        if snapshot.state not in {
            PortalPageState.MBS_FORM,
            PortalPageState.MBS_RESULTS,
        }:
            raise MbsCheckerError(
                f"{action} left the MBS workflow: {self._describe_snapshot(snapshot)}"
            )

    def _wait_for_expected_or_terminal(
        self,
        condition,
        *,
        action: str,
        timeout: int,
        allow_mbs_states: bool,
        poll_frequency: float = 0.5,
    ):
        def _guarded(driver):
            # Check the expected outcome first; the state snapshot is only
            # needed when the outcome has not appeared yet.
            result = condition(driver)
            if result:
                return result
            snapshot = self.state_detector.snapshot()
            self._raise_for_terminal_state(
                snapshot,
                action=action,
                allow_mbs_states=allow_mbs_states,
            )
            return False

        try:
            return self._wait(_guarded, timeout=timeout, poll_frequency=poll_frequency)
        except TimeoutException as exc:
            snapshot = self.state_detector.snapshot()
            self._raise_for_terminal_state(
                snapshot,
                action=action,
                allow_mbs_states=allow_mbs_states,
            )
            raise MbsCheckerError(
                f"{action} timed out while waiting for expected portal response: "
                f"{self._describe_snapshot(snapshot)}"
            ) from exc

    def _is_form_ready_now(self) -> bool:
        snapshot = self.state_detector.snapshot()
        if snapshot.state not in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
            return False

        required_ids = [
            "guiForm:guiMedicareCardNumber",
            "guiForm:guiIndividualReferenceNumber",
            "guiForm:guiFirstName",
            "guiForm:gui_providerLocation",
        ]
        for element_id in required_ids:
            elements = self.driver.find_elements(By.ID, element_id)
            if not elements or not elements[0].is_displayed():
                return False

        return bool(self.driver.find_elements(By.CSS_SELECTOR, _TAB_NAV_CSS))

    def _wait_for_page_ready(self, timeout=None):
        """Wait for the MBS page to be usable without over-waiting."""
        timeout = timeout or self.wait_timeout

        if self._is_form_ready_now():
            return

        log("Waiting for page to be fully loaded...")

        wait_for_page_load(self.driver, timeout)
        wait_for_ajax(self.driver, timeout)

        snapshot = self.state_detector.snapshot()
        if snapshot.state not in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
            raise MbsCheckerError(
                f"MBS page not ready; state={snapshot.state.value} "
                f"title='{snapshot.title}' url='{snapshot.url}'"
            )

        self._wait(
            EC.presence_of_element_located((By.ID, "guiForm:guiMedicareCardNumber")),
            timeout=min(timeout, 5)
        )
        self._wait(
            EC.presence_of_element_located((By.ID, "guiForm:gui_providerLocation")),
            timeout=min(timeout, 5)
        )

        self._wait(
            EC.presence_of_element_located((By.CSS_SELECTOR, _TAB_NAV_CSS)),
            timeout=min(timeout, 5)
        )

        log("Page fully loaded and ready")

    def get_page_snapshot(self) -> PageSnapshot:
        return self.state_detector.snapshot()

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

    def _fill_patient_form_fast(self, medicare_number: str, irn: str, first_name: str):
        provider_location = self.config.mbs.provider_location
        changed = self.driver.execute_script(
            """
            const [medicare, irn, firstName, providerLocation] = arguments;

            function byId(id) {
                return document.getElementById(id);
            }

            function setValue(el, value) {
                if (!el) return false;
                const changed = el.value !== value;
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return changed;
            }

            const mc = byId('guiForm:guiMedicareCardNumber');
            const irnEl = byId('guiForm:guiIndividualReferenceNumber');
            const name = byId('guiForm:guiFirstName');
            const consent = byId('guiForm:gui_patientConsentGiven');
            const provider = byId('guiForm:gui_providerLocation');

            if (!mc || !irnEl || !name || !consent || !provider) {
                return {ok: false, changed: false};
            }

            let changedAny = false;
            changedAny = setValue(mc, medicare) || changedAny;
            changedAny = setValue(irnEl, irn) || changedAny;
            changedAny = setValue(name, firstName) || changedAny;

            if (!consent.checked) {
                consent.click();
                changedAny = true;
            }

            if (provider.value !== providerLocation) {
                provider.value = providerLocation;
                provider.dispatchEvent(new Event('change', {bubbles: true}));
                provider.dispatchEvent(new Event('blur', {bubbles: true}));
                changedAny = true;
            }

            return {ok: true, changed: changedAny};
            """,
            medicare_number,
            irn,
            first_name,
            provider_location,
        )

        if not changed or not changed.get("ok"):
            raise MbsCheckerError("Patient form fields are not available on the page")

        self._wait(
            lambda d: (
                d.find_element(By.ID, "guiForm:guiMedicareCardNumber").get_attribute("value") == medicare_number
                and d.find_element(By.ID, "guiForm:guiIndividualReferenceNumber").get_attribute("value") == irn
                and d.find_element(By.ID, "guiForm:guiFirstName").get_attribute("value") == first_name
                and d.find_element(By.ID, "guiForm:gui_patientConsentGiven").is_selected()
                and Select(d.find_element(By.ID, "guiForm:gui_providerLocation")).first_selected_option.get_attribute("value") == provider_location
            ),
            timeout=3,
        )

        wait_for_ajax(
            self.driver,
            timeout=self.wait_timeout,
            settle_delay=self.ajax_stability_delay,
        )

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
        self._wait(
            EC.presence_of_element_located((By.ID, "guiForm:guiMedicareCardNumber")),
            timeout=3,
        )
        self._fill_patient_form_fast(medicare_number, irn, first_name)

        log("Patient form filled")

    # -- MBS item selection ---------------------------------------------------

    def select_mbs_items(self, items: Optional[List[str]] = None):
        if items is None:
            items = self.config.mbs.items_to_check
        if len(items) > 5:
            raise MbsCheckerError("Maximum of 5 MBS items can be selected")

        padded_items = [item.zfill(5) for item in items]
        log(f"Selecting MBS items: {padded_items}")
        self._current_tab_text = None
        current_selected = self._get_selected_items()

        tab_groups, fallback_items = self._group_items_by_tab(padded_items)

        # Each tick fires a PrimeFaces AJAX round-trip that re-renders the
        # selection panel; clicking the next box before the previous item
        # appears in the side panel silently drops earlier selections. So
        # confirm each item in the panel (fast 0.1s poll) before moving on.
        for tab_text, tab_items in tab_groups:
            self._switch_to_tab_by_text(tab_text)
            for padded_item in tab_items:
                if padded_item in current_selected:
                    log(f"MBS item already selected: {padded_item}")
                    continue
                self._click_item_checkbox(
                    padded_item,
                    previous_selected=current_selected,
                )
                current_selected.add(padded_item)
                log(f"Selected MBS item: {padded_item}")

        for padded_item in fallback_items:
            if padded_item in current_selected:
                log(f"MBS item already selected: {padded_item}")
                continue
            self._select_single_item(
                padded_item,
                previous_selected=current_selected,
            )
            current_selected.add(padded_item)

        log(f"All {len(padded_items)} MBS items selected")

    def _group_items_by_tab(self, padded_items: list[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
        tab_ranges = self._get_tab_ranges()
        grouped: list[tuple[str, list[str]]] = []
        fallback_items: list[str] = []

        for padded_item in padded_items:
            item_num = int(padded_item)
            target_tab = next(
                (
                    tab["text"]
                    for tab in tab_ranges
                    if tab["low"] <= item_num <= tab["high"]
                ),
                None,
            )
            if target_tab is None:
                fallback_items.append(padded_item)
                continue

            if grouped and grouped[-1][0] == target_tab:
                grouped[-1][1].append(padded_item)
            else:
                grouped.append((target_tab, [padded_item]))

        return grouped, fallback_items

    def _get_active_tab_text(self) -> str | None:
        selectors = [
            "div[id*='tabView'] li.ui-tabs-selected a",
            "div[id*='tabView'] li.ui-state-active a",
            "div[id*='tabView'] li[aria-selected='true'] a",
        ]
        for selector in selectors:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                text = element.text.strip()
                if text:
                    return text
        return None

    def _get_selected_items(self) -> set[str]:
        items = set()
        try:
            panel = self.driver.find_element(By.ID, "guiForm:itemSelector")
            for label in panel.find_elements(By.XPATH, "./div"):
                text = label.text.strip()
                match = re.match(r"^(\d{5})", text)
                if match:
                    items.add(match.group(1))
        except (NoSuchElementException, StaleElementReferenceException):
            # The selector panel re-renders via AJAX during selection; a stale
            # read means it is mid-update, so return what was gathered and let
            # the caller's poll retry on the next tick.
            return items
        return items

    def _wait_for_item_selected(
        self,
        padded_item: str,
        *,
        previous_selected: set[str],
        timeout: int = 4,
    ):
        def _item_selected(driver):
            selected_items = self._get_selected_items()
            if padded_item not in selected_items:
                return False

            if padded_item not in previous_selected:
                return True

            try:
                checkbox = self._find_item_checkbox(padded_item, prefer_active_panel=False)
            except NoSuchElementException:
                return True
            return checkbox.is_selected()

        self._wait_for_expected_or_terminal(
            _item_selected,
            action=f"Selecting item {padded_item}",
            timeout=timeout,
            allow_mbs_states=True,
            poll_frequency=0.1,
        )

    def _get_tab_ranges(self) -> List[Dict]:
        if self._tab_ranges_cache:
            return list(self._tab_ranges_cache)

        for attempt in range(3):
            tabs = []
            tab_links = self.driver.find_elements(By.CSS_SELECTOR, _TAB_NAV_CSS)

            if not tab_links:
                tab_links = self.driver.find_elements(
                    By.XPATH,
                    "//a[contains(text(), '-') and string-length(text()) < 20]"
                )

            try:
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
                self._tab_ranges_cache = list(tabs)
                return tabs
            except StaleElementReferenceException:
                log(f"Tab list became stale while reading ranges, retrying ({attempt + 1}/3)")
                self._tab_ranges_cache = None
                wait_for_ajax(
                    self.driver,
                    timeout=self.wait_timeout,
                    settle_delay=self.ajax_stability_delay,
                )

        raise MbsCheckerError("Tab navigation became stale while reading item ranges")

    def _switch_to_tab_by_text(self, target_text: str):
        tabs = self._get_tab_ranges()
        target = next((tab for tab in tabs if tab["text"] == target_text), None)
        if target is None:
            raise MbsCheckerError(f"Could not find tab {target_text}")

        active_tab_text = self._get_active_tab_text()
        if self._current_tab_text == target["text"] and active_tab_text == target["text"]:
            return

        log(f"Switching to tab: {target['text']}")
        try:
            target["element"].click()
        except StaleElementReferenceException:
            log("Target tab went stale before click, re-reading tab list")
            self._tab_ranges_cache = None
            refreshed_tabs = self._get_tab_ranges()
            refreshed_target = next(
                (tab for tab in refreshed_tabs if tab["text"] == target["text"]),
                None,
            )
            if refreshed_target is None:
                raise MbsCheckerError(f"Could not re-find tab {target['text']}")
            try:
                refreshed_target["element"].click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", refreshed_target["element"])
        except Exception:
            self.driver.execute_script("arguments[0].click();", target["element"])

        wait_for_ajax(
            self.driver,
            timeout=self.wait_timeout,
            settle_delay=self.ajax_stability_delay,
        )

        try:
            self._wait_for_expected_or_terminal(
                lambda d: self._get_active_tab_text() == target["text"],
                action=f"Switching to item tab {target['text']}",
                timeout=3,
                allow_mbs_states=True,
            )
        except MbsCheckerError:
            raise
        except Exception:
            log(f"Warning: active tab did not confirm as {target['text']}")

        try:
            self._wait_for_expected_or_terminal(
                lambda d: len(d.find_elements(
                    By.CSS_SELECTOR,
                    "div.ui-tabs-panel:not(.ui-helper-hidden) input[type='checkbox']",
                )) > 0,
                action=f"Loading item tab {target['text']}",
                timeout=3,
                allow_mbs_states=True,
            )
        except MbsCheckerError:
            raise
        except Exception:
            log("Warning: no checkboxes found in active tab panel")

        self._current_tab_text = self._get_active_tab_text() or target["text"]

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

        active_tab_text = self._get_active_tab_text()
        if self._current_tab_text == target["text"] and active_tab_text == target["text"]:
            return

        self._switch_to_tab_by_text(target["text"])

    def _select_single_item(
        self,
        item_number: str,
        *,
        previous_selected: set[str] | None = None,
    ):
        padded = item_number.zfill(5)
        log(f"Selecting MBS item: {padded}")

        self._switch_to_tab(padded)
        if padded in self._get_selected_items():
            log(f"MBS item already selected: {padded}")
            return

        try:
            self._click_item_checkbox(
                padded,
                previous_selected=previous_selected,
            )
            log(f"Selected MBS item: {padded}")
        except NoSuchElementException:
            log(f"Item {padded} not found in expected tab, searching all tabs")
            if not self._search_all_tabs(
                padded,
                previous_selected=previous_selected,
            ):
                raise MbsCheckerError(f"Could not find MBS item {padded} in any tab")

    def _find_item_checkbox(self, padded_item: str, *, prefer_active_panel: bool) -> object:
        root_xpaths = [
            ".//div[contains(@class, 'ui-tabs-panel') and not(contains(@class, 'ui-helper-hidden'))]",
            ".",
        ] if prefer_active_panel else ["."]

        for root_xpath in root_xpaths:
            for xpath in [
                f"{root_xpath}//label[normalize-space(text())='{padded_item}']",
                f"{root_xpath}//label[contains(text(), '{padded_item}')]",
            ]:
                try:
                    label = self.driver.find_element(By.XPATH, xpath)
                    checkbox_id = label.get_attribute("for")
                    if checkbox_id:
                        return self.driver.find_element(By.ID, checkbox_id)
                except NoSuchElementException:
                    continue

            xpath = (
                f"{root_xpath}//td[normalize-space(text())='{padded_item}']"
                f"/preceding-sibling::td//input[@type='checkbox']"
            )
            try:
                return self.driver.find_element(By.XPATH, xpath)
            except NoSuchElementException:
                continue

        raise NoSuchElementException(f"Checkbox for item {padded_item} not found")

    def _click_item_checkbox(
        self,
        padded_item: str,
        *,
        previous_selected: set[str] | None = None,
    ):
        if previous_selected is None:
            previous_selected = self._get_selected_items()
        else:
            previous_selected = set(previous_selected)

        checkbox = self._find_item_checkbox(padded_item, prefer_active_panel=True)
        if not checkbox.is_selected():
            self.driver.execute_script("arguments[0].click();", checkbox)
        self._wait_for_item_selected(padded_item, previous_selected=previous_selected)
        self._current_tab_text = self._get_active_tab_text()

    def _search_all_tabs(
        self,
        padded_item: str,
        *,
        previous_selected: set[str] | None = None,
    ) -> bool:
        i = 0
        while True:
            try:
                fresh_tabs = self._get_tab_ranges()
                if i >= len(fresh_tabs):
                    break
                fresh_tabs[i]["element"].click()
                wait_for_ajax(
                    self.driver,
                    timeout=self.wait_timeout,
                    settle_delay=self.ajax_stability_delay,
                )
                self._click_item_checkbox(
                    padded_item,
                    previous_selected=previous_selected,
                )
                self._current_tab_text = fresh_tabs[i]["text"]
                log(f"Found and selected {padded_item} in tab {fresh_tabs[i]['text']}")
                return True
            except MbsCheckerError:
                raise
            except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
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
            outcome = self._wait_for_expected_or_terminal(
                lambda d: self._detect_submit_outcome(d),
                action="Submitting MBS check",
                timeout=timeout,
                allow_mbs_states=True,
            )
        except MbsCheckerError:
            self._check_patient_validation_error()
            raise

        if outcome == "dialog":
            log("Multiple items confirmation dialog - clicking Continue")
            continue_btn = self._wait(EC.element_to_be_clickable((
                By.CSS_SELECTOR,
                "#guiForm\\:multipleMBSItemsConfirmDiag a.confirm-btn"
            )))
            continue_btn.click()
            # After dialog, wait for results or no-results
            try:
                self._wait_for_expected_or_terminal(
                    lambda d: (
                        d.find_elements(By.ID, "guiForm:guiMbsItemNumberSearchResults")
                        or self._is_no_results_visible(d)
                    ),
                    action="Waiting for MBS results after confirmation",
                    timeout=timeout,
                    allow_mbs_states=True,
                )
            except MbsCheckerError:
                self._check_patient_validation_error()
                raise

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

        # Check for error div (HPOS displays patient matching errors in <div class="error">)
        if not error_msg:
            try:
                error_divs = self.driver.find_elements(By.CLASS_NAME, "error")
                if error_divs:
                    for div in error_divs:
                        error_text = div.text.strip()
                        if error_text and div.is_displayed():
                            error_msg = error_text[:200]
                            break
            except Exception:
                pass

        # Check for field validation errors (e.g., highlighted input fields with error messages)
        if not error_msg:
            try:
                error_elements = self.driver.find_elements(By.CLASS_NAME, "fielderror")
                if error_elements:
                    error_text = " ".join(el.text for el in error_elements if el.text)
                    if error_text:
                        error_msg = f"Field validation error: {error_text[:100]}"
            except Exception:
                pass

        # Check for error patterns in page body text
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
        # Wait for results table to be present (may have brief delay after AJAX)
        timeout = self.config.session.page_load_timeout
        table = self._wait(
            EC.presence_of_element_located(
                (By.ID, "guiForm:guiMbsItemNumberSearchResults")
            ),
            timeout=timeout
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

    def reset_form_button(self):
        """Click the form reset button to clear all fields."""
        log("Attempting form reset via button")
        self._tab_ranges_cache = None
        selectors = [
            (By.ID, "guiForm:gui_reset"),
            (By.ID, "guiForm:gui_searchAgain"),
        ]

        for by, value in selectors:
            try:
                btn = self._wait(EC.element_to_be_clickable((by, value)), timeout=5)
                try:
                    btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", btn)
                self.wait_until_form_ready()
                log(f"Form reset button clicked successfully via {value}")
                return
            except TimeoutException:
                continue

        raise MbsCheckerError("Failed to reset form via button")

    def refresh_page_f5(self):
        """Refresh the page using F5 keyboard shortcut."""
        log("Refreshing page with F5")
        self._tab_ranges_cache = None
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.F5)
            self.wait_until_form_ready(timeout=self.config.session.page_load_timeout)
            log("Page refreshed with F5, form ready")
        except Exception as e:
            raise MbsCheckerError(f"Failed to refresh page with F5: {e}")

    def wait_until_form_ready(self, timeout: int | None = None) -> PageSnapshot:
        timeout = timeout or self.config.session.page_load_timeout
        self._wait_for_page_ready(timeout=timeout)
        snapshot = self.get_page_snapshot()
        if snapshot.state not in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
            raise MbsCheckerError(
                f"MBS page did not become ready; state={snapshot.state.value}"
            )
        return snapshot

    def recover_mbs_page(self, snapshot: PageSnapshot | None = None) -> PageSnapshot:
        snapshot = snapshot or self.get_page_snapshot()
        log(f"Recovering MBS page from state={snapshot.state.value}")

        if snapshot.state not in {
            PortalPageState.MBS_FORM,
            PortalPageState.MBS_RESULTS,
        }:
            raise MbsCheckerError(
                f"Cannot recover MBS page from state={snapshot.state.value}"
            )

        if snapshot.has_reset_button:
            try:
                self.reset_form_button()
                return self.get_page_snapshot()
            except MbsCheckerError as exc:
                log(f"Reset button recovery failed, falling back to refresh: {exc}")

        self.refresh_page_f5()
        refreshed = self.get_page_snapshot()
        if refreshed.state not in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
            raise MbsCheckerError(
                f"Page refresh did not restore MBS form; state={refreshed.state.value}"
            )
        return refreshed

    def new_check(self):
        """Reset the form for a fresh check with bounded recovery."""
        snapshot = self.get_page_snapshot()
        if snapshot.state in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
            self.recover_mbs_page(snapshot)
            return
        raise MbsCheckerError(
            f"Cannot start new check from state={snapshot.state.value}"
        )

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
