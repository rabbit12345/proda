import argparse
import sys

from .config import load_config, create_driver
from .auth import ProdaAuthenticator, LoginError
from .navigator import HposNavigator, NavigationError
from .mbs_checker import MbsChecker, MbsCheckerError, format_results
from .session_keeper import SessionKeeper


def log(msg: str):
    import time
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


def prompt_patient_details() -> tuple[str, str, str] | None:
    """Prompt the user for patient details interactively."""
    print("\n--- Enter patient details (or 'q' to quit) ---")
    medicare = input("Medicare card number: ").strip()
    if medicare.lower() == "q":
        return None

    irn = input("Individual reference number: ").strip()
    if irn.lower() == "q":
        return None

    first_name = input("First name: ").strip()
    if first_name.lower() == "q":
        return None

    return medicare, irn, first_name


def run_single_check(
    checker: MbsChecker,
    session_keeper: SessionKeeper,
    medicare: str,
    irn: str,
    first_name: str,
    items: list[str] | None = None,
):
    """Run a single patient check and display results."""
    try:
        results = checker.check_patient(medicare, irn, first_name, items)
        session_keeper.reset()
        print(format_results(medicare, first_name, results))
        return results
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
    config = load_config(args.config)
    if args.browser:
        config.browser.type = args.browser
    if args.headless:
        config.browser.headless = True

    # Create browser driver
    log(f"Starting {config.browser.type} browser...")
    driver = create_driver(config.browser)

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
        session_keeper = SessionKeeper(
            driver, config.session.keepalive_interval_seconds
        )
        session_keeper.start()

        # Create checker
        checker = MbsChecker(driver, config)

        # Single check mode
        if args.medicare and args.irn and args.name:
            run_single_check(
                checker, session_keeper,
                args.medicare, args.irn, args.name, args.items
            )

            # After single check, offer interactive mode
            print("\nCheck complete. Enter another patient or 'q' to quit.")
            while True:
                patient = prompt_patient_details()
                if patient is None:
                    break
                try:
                    checker.new_check()
                except MbsCheckerError:
                    log("Could not reset form, attempting page reload")
                    navigator.navigate_to_mbs_checker()
                run_single_check(
                    checker, session_keeper,
                    patient[0], patient[1], patient[2], args.items
                )
        else:
            # Interactive mode
            print("\nReady for patient checks.")
            first_check = True
            while True:
                patient = prompt_patient_details()
                if patient is None:
                    break

                if not first_check:
                    try:
                        checker.new_check()
                    except MbsCheckerError:
                        log("Could not reset form, attempting page reload")
                        navigator.navigate_to_mbs_checker()

                run_single_check(
                    checker, session_keeper,
                    patient[0], patient[1], patient[2], args.items
                )
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
        driver.quit()
        log("Done.")


if __name__ == "__main__":
    main()
