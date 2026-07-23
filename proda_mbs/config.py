import os
import yaml
from dataclasses import dataclass, field
from typing import List, Optional

from selenium import webdriver


@dataclass
class ProdaConfig:
    username: str = ""
    password: str = ""
    url: str = "https://proda.humanservices.gov.au/"


@dataclass
class GmailConfig:
    client_secret_path: str = "client_secret.json"
    token_path: str = "token.json"
    scopes: List[str] = field(default_factory=lambda: [
        "https://www.googleapis.com/auth/gmail.modify"
    ])


@dataclass
class MbsConfig:
    provider_location: str = "8X"
    items_to_check: List[str] = field(default_factory=lambda: [
        "00965", "00967", "2715", "2717"
    ])


@dataclass
class SessionConfig:
    keepalive_interval_seconds: int = 300
    # Re-enter HPOS proactively before its absolute session cap expires
    # (observed at ~1 hour; keep-alive pings only defer the idle timeout).
    # Recovery walks back in through My Services, so this costs no OTP.
    # 0 disables.
    preemptive_relogin_seconds: int = 2700
    page_load_timeout: int = 30
    element_wait_timeout: int = 15
    ajax_stability_delay: float = 0.05
    retry_count: int = 3


@dataclass
class BrowserConfig:
    type: str = "firefox"  # "firefox" or "chrome"
    headless: bool = False
    driver_path: str = ""  # Path to browser driver executable (leave empty for auto-detect)


@dataclass
class AppConfig:
    proda: ProdaConfig = field(default_factory=ProdaConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    mbs: MbsConfig = field(default_factory=MbsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""
    pass


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file with environment variable fallback."""
    config = AppConfig()

    # Try to load YAML config
    if config_path is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "config.yaml")

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}
        except PermissionError:
            if os.name == "nt":
                fix_hint = f"  Fix: right-click '{config_path}' > Properties > Security > grant your user read access"
            else:
                fix_hint = f"  Fix with: sudo chown $(whoami) '{config_path}'"
            raise ConfigError(
                f"Permission denied reading '{config_path}'.\n{fix_hint}"
            )

        proda = data.get("proda", {})
        config.proda.username = proda.get("username", "")
        config.proda.password = proda.get("password", "")
        config.proda.url = proda.get("url", config.proda.url)

        gmail = data.get("gmail", {})
        config.gmail.client_secret_path = gmail.get(
            "client_secret_path", config.gmail.client_secret_path
        )
        config.gmail.token_path = gmail.get("token_path", config.gmail.token_path)

        mbs = data.get("mbs", {})
        config.mbs.provider_location = mbs.get(
            "provider_location", config.mbs.provider_location
        )
        if "items_to_check" in mbs:
            config.mbs.items_to_check = [str(i) for i in mbs["items_to_check"]]

        session = data.get("session", {})
        config.session.keepalive_interval_seconds = session.get(
            "keepalive_interval_seconds", config.session.keepalive_interval_seconds
        )
        config.session.preemptive_relogin_seconds = session.get(
            "preemptive_relogin_seconds", config.session.preemptive_relogin_seconds
        )
        config.session.page_load_timeout = session.get(
            "page_load_timeout", config.session.page_load_timeout
        )
        config.session.element_wait_timeout = session.get(
            "element_wait_timeout", config.session.element_wait_timeout
        )
        config.session.ajax_stability_delay = session.get(
            "ajax_stability_delay", config.session.ajax_stability_delay
        )
        config.session.retry_count = session.get(
            "retry_count", config.session.retry_count
        )

        browser = data.get("browser", {})
        config.browser.type = browser.get("type", config.browser.type)
        config.browser.headless = browser.get("headless", config.browser.headless)
        config.browser.driver_path = browser.get("driver_path", config.browser.driver_path)

    # Environment variable fallback for credentials
    config.proda.username = os.environ.get("PRODA_USERNAME", config.proda.username)
    config.proda.password = os.environ.get("PRODA_PASSWORD", config.proda.password)

    # Resolve relative paths for Gmail files
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isabs(config.gmail.client_secret_path):
        config.gmail.client_secret_path = os.path.join(
            base_dir, config.gmail.client_secret_path
        )
    if not os.path.isabs(config.gmail.token_path):
        config.gmail.token_path = os.path.join(base_dir, config.gmail.token_path)

    # Validate required fields
    _validate_config(config)

    return config


def _validate_config(config: AppConfig):
    """Validate that required configuration fields are present."""
    if not config.proda.username:
        raise ConfigError(
            "PRODA username is required. Set in config.yaml or "
            "PRODA_USERNAME environment variable."
        )
    if not config.proda.password:
        raise ConfigError(
            "PRODA password is required. Set in config.yaml or "
            "PRODA_PASSWORD environment variable."
        )
    if config.browser.type not in ("firefox", "chrome"):
        raise ConfigError(
            f"Invalid browser type '{config.browser.type}'. "
            "Must be 'firefox' or 'chrome'."
        )
    if not config.mbs.items_to_check:
        raise ConfigError("At least one MBS item must be configured.")
    if len(config.mbs.items_to_check) > 5:
        raise ConfigError("Maximum of 5 MBS items can be checked at once.")

    # Gmail auth: need either token.json OR client_secret.json (for first-time setup)
    has_token = os.path.exists(config.gmail.token_path)
    has_secret = os.path.exists(config.gmail.client_secret_path)
    if not has_token and not has_secret:
        raise ConfigError(
            "Gmail authentication not configured.\n"
            f"  Neither token file nor client secret found:\n"
            f"    token:  {config.gmail.token_path}\n"
            f"    secret: {config.gmail.client_secret_path}\n"
            f"  \n"
            f"  For first-time setup, place client_secret.json from\n"
            f"  Google Cloud Console (Gmail API, OAuth Desktop client)\n"
            f"  at the path above. A token.json will be created after\n"
            f"  the first successful login."
        )


def create_driver(browser_config: BrowserConfig) -> webdriver.Remote:
    """Create a Selenium WebDriver instance based on config."""
    if browser_config.type.lower() == "chrome":
        options = webdriver.ChromeOptions()
        if browser_config.headless:
            options.add_argument("--headless=new")
        if browser_config.driver_path:
            from selenium.webdriver.chrome.service import Service as ChromeService
            service = ChromeService(executable_path=browser_config.driver_path)
            return webdriver.Chrome(service=service, options=options)
        return webdriver.Chrome(options=options)
    else:
        options = webdriver.FirefoxOptions()
        if browser_config.headless:
            options.add_argument("--headless")
        if browser_config.driver_path:
            from selenium.webdriver.firefox.service import Service as GeckoService
            service = GeckoService(executable_path=browser_config.driver_path)
            return webdriver.Firefox(service=service, options=options)
        return webdriver.Firefox(options=options)
