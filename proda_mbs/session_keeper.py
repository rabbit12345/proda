import threading
import time


def log(msg: str):
    print(f"{time.strftime('%d/%m/%y %H:%M:%S')} {msg}")


class SessionKeeper:
    """Keeps the HPOS session alive via periodic JS fetch in the browser."""

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
        self._running = True
        self._schedule_next()
        log(f"Session keeper started (interval: {self.interval}s)")

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        log("Session keeper stopped")

    def reset(self):
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
        if not self._running:
            return
        try:
            with self._lock:
                status = self.driver.execute_script(
                    "try { var x = new XMLHttpRequest();"
                    f"x.open('GET', '{self.KEEPALIVE_URL}', false);"
                    "x.send(); return x.status; }"
                    "catch(e) { return -1; }"
                )
                if status == 200:
                    log("Session keep-alive ping successful")
                else:
                    log(f"Session keep-alive ping: status {status}")
        except Exception as e:
            log(f"Session keep-alive ping failed: {e}")
        self._schedule_next()
