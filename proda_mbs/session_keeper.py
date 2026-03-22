import threading
import time

import requests


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


class SessionKeeper:
    """Keeps the HPOS session alive by periodically pinging the server."""

    KEEPALIVE_URL = (
        "https://www2.medicareaustralia.gov.au:5447"
        "/pcert/hpos/faces/landingHome.xhtml"
    )

    def __init__(self, driver, interval_seconds: int = 300):
        self.driver = driver
        self.interval = interval_seconds
        self._timer = None
        self._lock = threading.Lock()
        self._running = False
        self._session = requests.Session()

    def start(self):
        """Start the keep-alive timer."""
        self._running = True
        self._schedule_next()
        log(f"Session keeper started (interval: {self.interval}s)")

    def stop(self):
        """Stop the keep-alive timer."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._session.close()
        log("Session keeper stopped")

    def reset(self):
        """Reset the timer after a page interaction."""
        if self._running:
            if self._timer:
                self._timer.cancel()
            self._schedule_next()

    def _schedule_next(self):
        if self._running:
            self._timer = threading.Timer(self.interval, self._ping)
            self._timer.daemon = True
            self._timer.start()

    def _ping(self):
        """Send a keep-alive request using the browser's session cookies."""
        if not self._running:
            return

        try:
            with self._lock:
                # Sync cookies from the Selenium driver
                self._session.cookies.clear()
                for cookie in self.driver.get_cookies():
                    self._session.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=cookie.get("domain", ""),
                    )

                response = self._session.get(
                    self.KEEPALIVE_URL, timeout=10, allow_redirects=False
                )
                if response.status_code == 200:
                    log("Session keep-alive ping successful")
                else:
                    log(f"Session keep-alive ping: status {response.status_code}")
        except requests.RequestException as e:
            log(f"Session keep-alive ping failed: {e}")

        self._schedule_next()
