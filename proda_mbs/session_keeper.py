import threading
import time


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


class SessionKeeper:
    """Keeps the HPOS session alive by periodically executing a JS fetch
    through the Selenium browser, preserving the real session context."""

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
        """Send a keep-alive request using the browser's JS context.
        This ensures cookies, session state, and CSRF tokens are all valid."""
        if not self._running:
            return

        try:
            with self._lock:
                # Use JS fetch within the browser to keep the real session alive.
                # This runs in the same browser context with all cookies/headers.
                result = self.driver.execute_script(f"""
                    try {{
                        var xhr = new XMLHttpRequest();
                        xhr.open('GET', '{self.KEEPALIVE_URL}', false);
                        xhr.send();
                        return xhr.status;
                    }} catch(e) {{
                        return -1;
                    }}
                """)
                if result == 200:
                    log("Session keep-alive ping successful")
                elif result == -1:
                    log("Session keep-alive ping: JS error (may still be alive)")
                else:
                    log(f"Session keep-alive ping: status {result}")
        except Exception as e:
            log(f"Session keep-alive ping failed: {e}")

        self._schedule_next()
