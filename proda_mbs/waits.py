from __future__ import annotations

import time

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


def wait_for_ajax(driver, timeout: int = 15):
    """Bounded settle wait without JavaScript execution.

    PrimeFaces updates frequently replace parts of the DOM. A short body-presence
    wait plus a tiny settle delay is safer than hot-path execute_script calls,
    which can wedge the whole process when the browser transport stalls.
    """
    try:
        WebDriverWait(driver, min(timeout, 5)).until(
            lambda d: len(d.find_elements(By.TAG_NAME, "body")) > 0
        )
        time.sleep(0.25)
    except (TimeoutException, WebDriverException):
        log("Warning: AJAX settle wait did not complete cleanly")


def wait_for_page_load(driver, timeout: int = 30):
    """Wait for a usable page without synchronous JavaScript probes."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.find_elements(By.TAG_NAME, "body")) > 0
        )
        time.sleep(0.3)
    except WebDriverException as exc:
        raise TimeoutException(f"Browser did not reach a usable page within {timeout}s: {exc}") from exc
