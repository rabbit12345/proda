from __future__ import annotations

import argparse
import ctypes
import sys
import threading

from .config import load_config, create_driver, ConfigError
from .auth import ProdaAuthenticator, LoginError
from .navigator import HposNavigator, NavigationError
from .mbs_checker import MbsChecker, MbsCheckerError, InvalidPatientError, format_results
from .session_keeper import SessionKeeper
from .waits import log

_MAX_RECOVERY_ATTEMPTS = 3
_HPOS_HOST = "www2.medicareaustralia.gov.au"


def _prime_clipboard():
    """Force delayed clipboard rendering held by the browser.

    After Selenium automation Chrome/Firefox becomes the clipboard owner
    with delayed rendering.  The first Ctrl+V in the console sends
    WM_RENDERFORMAT to Chrome, but Chrome isn't processing Windows messages
    yet so the request silently fails.  Calling GetClipboardData here forces
    Chrome to materialise the data now, while we still have the driver
    connection active, so all subsequent pastes work immediately.
    """
    try:
        u32 = ctypes.windll.user32
        CF_UNICODETEXT = 13
        if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return
        if not u32.OpenClipboard(None):
            return
        try:
            u32.GetClipboardData(CF_UNICODETEXT)  # triggers WM_RENDERFORMAT
        finally:
            u32.CloseClipboard()
    except Exception:
        pass



def _refocus_console():
    """Bring the console window to the foreground after browser automation.

    SetForegroundWindow is silently ignored by Windows when the calling
    process is not the current foreground owner.  Temporarily attaching
    our thread input to the foreground thread bypasses that restriction.
    """
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        foreground_hwnd = u32.GetForegroundWindow()
        foreground_tid = u32.GetWindowThreadProcessId(foreground_hwnd, None)
        current_tid = k32.GetCurrentThreadId()
        attached = False
        if foreground_tid and foreground_tid != current_tid:
            u32.AttachThreadInput(foreground_tid, current_tid, True)
            attached = True
        try:
            u32.BringWindowToTop(hwnd)
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(foreground_tid, current_tid, False)
    except Exception:
        pass


def _quit_driver_with_timeout(driver, timeout: int = 8):
    t = threading.Thread(target=driver.quit, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        log("Browser did not close within timeout — continuing anyway")


def _prime_clipboard_async():
    """Run clipboard priming in background thread with timeout."""
    def _do_prime():
        try:
            _prime_clipboard()
        except Exception:
            pass
    t = threading.Thread(target=_do_prime, daemon=True)
    t.start()


def _refocus_console_async():
    """Run console refocus in background thread with timeout."""
    def _do_refocus():
        try:
            _refocus_console()
        except Exception:
            pass
    t = threading.Thread(target=_do_refocus, daemon=True)
    t.start()


def _still_on_hpos(driver) -> bool:
    """Return True if the browser is still on an HPOS page."""
    try:
        return _HPOS_HOST in driver.current_url
    except Exception:
        return True  # assume OK if we can't read the URL


def prompt_patient_details() -> tuple[str, str, str] | None:
    """Prompt the user for patient details interactively.

    Navigation:
      'q'   — quit
      'b'   — go back to previous field
      'r'   — restart entry (discard current values)
    Validation:
      Medicare must be exactly 10 digits.
      IRN must be exactly 1 digit.
    """
    fields = [
        ("Medicare card number (10 digits)", "medicare"),
        ("Individual reference number (1 digit)", "irn"),
        ("First name", "name"),
    ]
    values: list[str] = [""] * len(fields)
    idx = 0

    print("\n--- Enter patient details ('q' quit, 'b' back, 'r' restart) ---")
    while idx < len(fields):
        prompt_label = fields[idx][0]
        raw = input(f"  {prompt_label}: ").strip()

        if raw.lower() == "q":
            return None
        if raw.lower() == "r":
            values = [""] * len(fields)
            idx = 0
            print("  (restarted)")
            continue
        if raw.lower() == "b":
            if idx > 0:
                idx -= 1
                print(f"  (back to {fields[idx][0]})")
            else:
                print("  (already at first field)")
            continue

        # Validate numeric fields
        field_key = fields[idx][1]
        if field_key == "medicare":
            if not raw.isdigit() or len(raw) != 10:
                print("  Invalid: must be exactly 10 digits")
                continue
        elif field_key == "irn":
            if not raw.isdigit() or len(raw) != 1:
                print("  Invalid: must be a single digit")
                continue
        elif field_key == "name":
            if not raw:
                print("  Invalid: name cannot be empty")
                continue

        values[idx] = raw
        idx += 1

    return values[0], values[1], values[2]


def _recover_session(
    driver,
    config,
    old_session_keeper: SessionKeeper,
    driver_lock: threading.Lock,
    after_ping=None,
) -> tuple[SessionKeeper, MbsChecker, HposNavigator]:
    """Re-login and navigate back to MBS checker after session loss.

    Returns a new (session_keeper, checker, navigator) triple.
    Raises LoginError or NavigationError on failure.
    """
    log("Attempting session recovery (re-login)...")
    old_session_keeper.stop()

    with driver_lock:
        auth = ProdaAuthenticator(driver, config)
        auth.login()

        navigator = HposNavigator(driver, config)
        navigator.navigate_to_mbs_checker_full()

    new_sk = SessionKeeper(driver, config.session.keepalive_interval_seconds,
                           driver_lock=driver_lock,
                           after_ping=after_ping)
    new_sk.start()

    checker = MbsChecker(driver, config)
    new_sk.set_keepalive_action(checker.new_check)
    log("Session recovery successful")
    return new_sk, checker, navigator


def run_single_check(
    checker: MbsChecker,
    session_keeper: SessionKeeper,
    medicare: str,
    irn: str,
    first_name: str,
    items: list[str] | None = None,
    driver_lock: threading.Lock | None = None,
):
    """Run a single patient check and display results.

    Raises InvalidPatientError so the caller can reset to patient entry.
    """
    try:
        lock = driver_lock or threading.Lock()
        with lock:
            results = checker.check_patient(medicare, irn, first_name, items)
        session_keeper.reset()
        print(format_results(medicare, first_name, results))
        return results
    except InvalidPatientError:
        raise
    except MbsCheckerError as e:
        log(f"MBS check failed: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="PRODA MBS Items Online Checker Automation"
    )
    parser.add_argument(
        "--config", type=str, default=None, help="Path to config.yaml"
    )
    parser.add_argument(
        "--browser",
        type=str,
        choices=["firefox", "chrome"],
        default=None,
        help="Browser to use (overrides config)",
    )
    parser.add_argument(
        "--medicare", type=str, default=None, help="Medicare card number"
    )
    parser.add_argument(
        "--irn", type=str, default=None, help="Individual reference number"
    )
    parser.add_argument(
        "--name", type=str, default=None, help="Patient first name"
    )
    parser.add_argument(
        "--items",
        type=str,
        nargs="+",
        default=None,
        help="MBS item numbers to check (overrides config)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )

    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except ConfigError as e:
        log(f"Configuration error: {e}")
        sys.exit(1)

    if args.browser:
        config.browser.type = args.browser
    if args.headless:
        config.browser.headless = True

    # Create browser driver
    log(f"Starting {config.browser.type} browser...")
    driver = create_driver(config.browser)

    # Shared lock so the keepalive timer thread and main thread
    # never issue concurrent Selenium commands.
    driver_lock = threading.Lock()

    session_keeper = None
    try:
        # Login
        log("Starting PRODA login...")
        auth = ProdaAuthenticator(driver, config)
        auth.login()

        # Navigate to MBS checker
        log("Navigating to MBS Items Online Checker...")
        navigator = HposNavigator(driver, config)
        navigator.navigate_to_mbs_checker_full()

        # Start session keep-alive
        def _after_ping():
            _prime_clipboard_async()
            _refocus_console_async()

        session_keeper = SessionKeeper(
            driver, config.session.keepalive_interval_seconds,
            driver_lock=driver_lock,
            after_ping=_after_ping,
        )
        session_keeper.start()

        # Create checker
        checker = MbsChecker(driver, config)
        session_keeper.set_keepalive_action(checker.new_check)

        # Single check mode
        if args.medicare and args.irn and args.name:
            run_single_check(
                checker, session_keeper,
                args.medicare, args.irn, args.name, args.items,
                driver_lock=driver_lock,
            )
            print("\nCheck complete. Enter another patient or 'q' to quit.")

        else:
            print("\nReady for patient checks.")

        # Interactive loop — keeps running until user quits
        first_check = not (args.medicare and args.irn and args.name)
        skip_form_reset = False
        recovery_attempts = 0
        patient = None

        while True:
            # Recover if session was invalidated by another login
            if not session_keeper.is_session_valid:
                recovery_attempts += 1
                if recovery_attempts > _MAX_RECOVERY_ATTEMPTS:
                    log(f"Session recovery failed {_MAX_RECOVERY_ATTEMPTS} "
                        "times, giving up")
                    break
                log(f"Session invalid, recovering "
                    f"(attempt {recovery_attempts}/{_MAX_RECOVERY_ATTEMPTS})...")
                try:
                    session_keeper, checker, navigator = _recover_session(
                        driver, config, session_keeper, driver_lock,
                        after_ping=_after_ping,
                    )
                    first_check = True
                    recovery_attempts = 0
                except (LoginError, NavigationError) as e:
                    log(f"Session recovery failed: {e}")
                    continue

            patient = prompt_patient_details()
            if patient is None:
                break

            # Session may have died while waiting for user input
            if not session_keeper.is_session_valid:
                log("Session lost while waiting for input, will recover")
                continue

            if not first_check and not skip_form_reset:
                try:
                    with driver_lock:
                        checker.new_check()
                except MbsCheckerError:
                    log("Could not reset form, attempting page reload")
                    try:
                        with driver_lock:
                            navigator.navigate_to_mbs_checker()
                    except NavigationError:
                        if not session_keeper.is_session_valid:
                            log("Session lost during recovery, will re-login next iteration")
                            continue
                        with driver_lock:
                            if not _still_on_hpos(driver):
                                log("Navigation failed and browser left HPOS — marking session lost")
                                session_keeper.mark_session_lost()
                                continue
                        log("Navigation failed after form reset error")
                        continue

            skip_form_reset = False
            try:
                result = run_single_check(
                    checker, session_keeper,
                    patient[0], patient[1], patient[2], args.items,
                    driver_lock=driver_lock,
                )
            except InvalidPatientError as e:
                print(f"\n  ** {e}")
                print("  Patient fields cleared — item selection preserved.")
                print("  Please re-enter patient details.\n")
                # InvalidPatientError already cleared patient fields via JS.
                # Skip form reset — no need to click button or wait for form.
                skip_form_reset = True
                _prime_clipboard_async()
                _refocus_console_async()
                continue

            _prime_clipboard_async()
            _refocus_console_async()

            if result is None and session_keeper.is_session_valid:
                # Check whether the failure was due to the session expiring
                # (browser navigated away from HPOS) rather than a transient error.
                with driver_lock:
                    if not _still_on_hpos(driver):
                        log("Check failed and browser left HPOS — marking session lost")
                        session_keeper.mark_session_lost()

            first_check = False

    except LoginError as e:
        log(f"Login failed: {e}")
        sys.exit(1)
    except NavigationError as e:
        log(f"Navigation failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        if session_keeper:
            session_keeper.stop()
        log("Closing browser...")
        _quit_driver_with_timeout(driver)
        log("Done.")


if __name__ == "__main__":
    main()
