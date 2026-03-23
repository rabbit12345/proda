from __future__ import annotations

import time

from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


def wait_for_ajax(driver, timeout: int = 15):
    """Wait for PrimeFaces AJAX queue to drain."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                "return typeof PrimeFaces === 'undefined' || "
                "PrimeFaces.ajax.Queue.isEmpty()"
            )
        )
    except TimeoutException:
        log("Warning: AJAX queue did not empty within timeout")


def wait_for_page_load(driver, timeout: int = 30):
    """Wait for document.readyState == 'complete'.

    Uses an async-friendly script with a short browser-side timeout so
    that a single execute_script call cannot block indefinitely when the
    page has slow/stuck resources (common on government portals).
    """
    script = (
        "try { return document.readyState; } "
        "catch(e) { return 'unknown'; }"
    )
    end = time.time() + timeout
    while time.time() < end:
        try:
            # Set a per-command timeout so execute_script cannot hang
            # longer than 5 seconds even if the browser is blocked.
            driver.set_script_timeout(5)
            state = driver.execute_script(script)
            if state == "complete":
                return
        except Exception:
            pass  # Transport-level hang, browser busy — retry
        time.sleep(0.5)
    raise TimeoutException(
        f"document.readyState did not reach 'complete' within {timeout}s"
    )
