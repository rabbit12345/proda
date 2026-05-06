from __future__ import annotations

import argparse
import ctypes
import sys
import threading

from .auth import LoginError, ProdaAuthenticator
from .config import ConfigError, create_driver, load_config
from .mbs_checker import InvalidPatientError, MbsChecker, MbsCheckerError, format_results
from .navigator import HposNavigator, NavigationError
from .page_state import PageStateDetector, PortalPageState
from .session_keeper import SessionKeeper
from .waits import log

_MAX_RECOVERY_ATTEMPTS = 3


def _prime_clipboard():
    try:
        u32 = ctypes.windll.user32
        cf_unicode_text = 13
        if not u32.IsClipboardFormatAvailable(cf_unicode_text):
            return
        if not u32.OpenClipboard(None):
            return
        try:
            u32.GetClipboardData(cf_unicode_text)
        finally:
            u32.CloseClipboard()
    except Exception:
        pass


def _refocus_console():
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
            u32.ShowWindow(hwnd, 9)
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(foreground_tid, current_tid, False)
    except Exception:
        pass


def _quit_driver_with_timeout(driver, timeout: int = 8):
    thread = threading.Thread(target=driver.quit, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        log("Browser did not close within timeout - continuing anyway")


def _prime_clipboard_async():
    thread = threading.Thread(target=_prime_clipboard, daemon=True)
    thread.start()


def _refocus_console_async():
    thread = threading.Thread(target=_refocus_console, daemon=True)
    thread.start()


def _state_requires_relogin(state: PortalPageState) -> bool:
    return state in {
        PortalPageState.BROWSER_UNAVAILABLE,
        PortalPageState.LOGIN,
        PortalPageState.OTP,
        PortalPageState.SESSION_EXPIRED,
        PortalPageState.LOGGED_OUT,
        PortalPageState.OFFSITE,
    }


def prompt_patient_details() -> tuple[str, str, str] | None:
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

        field_key = fields[idx][1]
        if field_key == "medicare":
            if not raw.isdigit() or len(raw) != 10:
                print("  Invalid: must be exactly 10 digits")
                continue
        elif field_key == "irn":
            if not raw.isdigit() or len(raw) != 1:
                print("  Invalid: must be a single digit")
                continue
        elif field_key == "name" and not raw:
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
) -> tuple[SessionKeeper, MbsChecker, HposNavigator, PageStateDetector]:
    log("Attempting session recovery (re-login)...")
    old_session_keeper.stop()

    with driver_lock:
        auth = ProdaAuthenticator(driver, config)
        auth.login()

        navigator = HposNavigator(driver, config)
        navigator.navigate_to_mbs_checker_full()
        checker = MbsChecker(driver, config)
        detector = PageStateDetector(driver)
        checker.wait_until_form_ready()

    new_session_keeper = SessionKeeper(
        driver,
        config.session.keepalive_interval_seconds,
        driver_lock=driver_lock,
        after_ping=after_ping,
    )
    new_session_keeper.set_keepalive_action(checker.new_check)
    new_session_keeper.start()

    log("Session recovery successful")
    return new_session_keeper, checker, navigator, detector


def _ensure_mbs_context(
    navigator: HposNavigator,
    checker: MbsChecker,
    session_keeper: SessionKeeper,
    detector: PageStateDetector,
    *,
    require_fresh_form: bool,
):
    snapshot = detector.snapshot()
    log(f"Checking portal state before action: {snapshot.state.value}")

    if _state_requires_relogin(snapshot.state):
        session_keeper.mark_session_lost()
        raise MbsCheckerError(
            f"Session is not recoverable in-place: state={snapshot.state.value}"
        )

    if snapshot.state in {PortalPageState.MY_SERVICES, PortalPageState.HPOS_LANDING}:
        try:
            navigator.navigate_to_mbs_checker()
        except NavigationError:
            snapshot = detector.snapshot()
            if _state_requires_relogin(snapshot.state):
                session_keeper.mark_session_lost()
            raise
        snapshot = detector.snapshot()

    if snapshot.state == PortalPageState.UNKNOWN:
        try:
            snapshot = checker.recover_mbs_page(snapshot)
        except MbsCheckerError:
            session_keeper.mark_session_lost()
            raise

    if snapshot.state not in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
        if snapshot.state == PortalPageState.UNKNOWN:
            session_keeper.mark_session_lost()
        raise MbsCheckerError(
            f"Unexpected portal state after navigation: {snapshot.state.value}"
        )

    if require_fresh_form:
        try:
            snapshot = checker.recover_mbs_page(snapshot)
        except MbsCheckerError:
            session_keeper.mark_session_lost()
            raise
    else:
        snapshot = checker.wait_until_form_ready()

    if _state_requires_relogin(snapshot.state):
        session_keeper.mark_session_lost()
        raise MbsCheckerError(
            f"Session expired while preparing the MBS page: {snapshot.state.value}"
        )

    if snapshot.state not in {PortalPageState.MBS_FORM, PortalPageState.MBS_RESULTS}:
        raise MbsCheckerError(f"MBS page is not ready: {snapshot.state.value}")


def run_single_check(
    checker: MbsChecker,
    session_keeper: SessionKeeper,
    detector: PageStateDetector,
    medicare: str,
    irn: str,
    first_name: str,
    items: list[str] | None = None,
    driver_lock: threading.Lock | None = None,
):
    try:
        lock = driver_lock or threading.Lock()
        with lock:
            pre_check_snapshot = detector.snapshot()
            if pre_check_snapshot.state not in {
                PortalPageState.MBS_FORM,
                PortalPageState.MBS_RESULTS,
            }:
                raise MbsCheckerError(
                    f"Cannot run check from page state {pre_check_snapshot.state.value}"
                )
            results = checker.check_patient(medicare, irn, first_name, items)
            post_check_snapshot = detector.snapshot()
            if _state_requires_relogin(post_check_snapshot.state):
                session_keeper.mark_session_lost()
                raise MbsCheckerError(
                    f"Session expired during patient check: {post_check_snapshot.state.value}"
                )

        session_keeper.reset()
        print(format_results(medicare, first_name, results))
        return results
    except InvalidPatientError:
        raise
    except MbsCheckerError as exc:
        log(f"MBS check failed: {exc}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="PRODA MBS Items Online Checker Automation"
    )
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--browser",
        type=str,
        choices=["firefox", "chrome"],
        default=None,
        help="Browser to use (overrides config)",
    )
    parser.add_argument("--medicare", type=str, default=None, help="Medicare card number")
    parser.add_argument("--irn", type=str, default=None, help="Individual reference number")
    parser.add_argument("--name", type=str, default=None, help="Patient first name")
    parser.add_argument(
        "--items",
        type=str,
        nargs="+",
        default=None,
        help="MBS item numbers to check (overrides config)",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log(f"Configuration error: {exc}")
        sys.exit(1)

    if args.browser:
        config.browser.type = args.browser
    if args.headless:
        config.browser.headless = True

    log(f"Starting {config.browser.type} browser...")
    driver = create_driver(config.browser)
    driver_lock = threading.Lock()
    session_keeper = None

    try:
        log("Starting PRODA login...")
        auth = ProdaAuthenticator(driver, config)
        auth.login()

        log("Navigating to MBS Items Online Checker...")
        navigator = HposNavigator(driver, config)
        navigator.navigate_to_mbs_checker_full()
        checker = MbsChecker(driver, config)
        state_detector = PageStateDetector(driver)
        with driver_lock:
            checker.wait_until_form_ready()

        def _after_ping():
            _prime_clipboard_async()
            _refocus_console_async()

        session_keeper = SessionKeeper(
            driver,
            config.session.keepalive_interval_seconds,
            driver_lock=driver_lock,
            after_ping=_after_ping,
        )
        session_keeper.set_keepalive_action(checker.new_check)
        session_keeper.start()

        if args.medicare and args.irn and args.name:
            with driver_lock:
                _ensure_mbs_context(
                    navigator,
                    checker,
                    session_keeper,
                    state_detector,
                    require_fresh_form=False,
                )
            run_single_check(
                checker,
                session_keeper,
                state_detector,
                args.medicare,
                args.irn,
                args.name,
                args.items,
                driver_lock=driver_lock,
            )
            print("\nCheck complete. Enter another patient or 'q' to quit.")
        else:
            print("\nReady for patient checks.")

        first_check = not (args.medicare and args.irn and args.name)
        skip_form_reset = False
        recovery_attempts = 0

        while True:
            if not session_keeper.is_session_valid:
                recovery_attempts += 1
                if recovery_attempts > _MAX_RECOVERY_ATTEMPTS:
                    log(f"Session recovery failed {_MAX_RECOVERY_ATTEMPTS} times, giving up")
                    break
                log(
                    f"Session invalid, recovering "
                    f"(attempt {recovery_attempts}/{_MAX_RECOVERY_ATTEMPTS})..."
                )
                try:
                    session_keeper, checker, navigator, state_detector = _recover_session(
                        driver, config, session_keeper, driver_lock, after_ping=_after_ping
                    )
                    first_check = True
                    recovery_attempts = 0
                except (LoginError, NavigationError, MbsCheckerError) as exc:
                    log(f"Session recovery failed: {exc}")
                    continue

            patient = prompt_patient_details()
            if patient is None:
                break

            if not session_keeper.is_session_valid:
                log("Session lost while waiting for input, will recover")
                continue

            try:
                with driver_lock:
                    _ensure_mbs_context(
                        navigator,
                        checker,
                        session_keeper,
                        state_detector,
                        require_fresh_form=(
                            session_keeper.needs_refresh
                            or (not first_check and not skip_form_reset)
                        ),
                    )
            except (MbsCheckerError, NavigationError) as exc:
                log(f"Could not prepare MBS page: {exc}")
                continue

            skip_form_reset = False
            try:
                result = run_single_check(
                    checker,
                    session_keeper,
                    state_detector,
                    patient[0],
                    patient[1],
                    patient[2],
                    args.items,
                    driver_lock=driver_lock,
                )
            except InvalidPatientError as exc:
                print(f"\n  ** {exc}")
                print("  Patient fields cleared - item selection preserved.")
                print("  Please re-enter patient details.\n")
                skip_form_reset = True
                _prime_clipboard_async()
                _refocus_console_async()
                continue

            _prime_clipboard_async()
            _refocus_console_async()

            with driver_lock:
                post_check_snapshot = state_detector.snapshot()
                if _state_requires_relogin(post_check_snapshot.state):
                    log(
                        "Patient check ended on relogin-required page state: "
                        f"{post_check_snapshot.state.value}"
                    )
                    session_keeper.mark_session_lost()
                elif result is None and post_check_snapshot.state == PortalPageState.UNKNOWN:
                    log("Patient check left browser in unknown state - marking session lost")
                    session_keeper.mark_session_lost()

            first_check = False

    except LoginError as exc:
        log(f"Login failed: {exc}")
        sys.exit(1)
    except NavigationError as exc:
        log(f"Navigation failed: {exc}")
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
