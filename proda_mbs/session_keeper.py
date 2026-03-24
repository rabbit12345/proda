from __future__ import annotations

import threading

from .waits import log

# Patterns in response body that indicate the server returned a login/redirect
# page instead of the real HPOS landing page.  These are checked against
# the first 500 chars of the response (lowercased).
_SESSION_LOST_PATTERNS = (
    "loginformandstuff",   # PRODA login form element ID
    "sign in to proda",    # PRODA login page heading
    "session has expired",
    "session timeout",
    "unauthori",           # "unauthorized" / "unauthorised"
)

_MAX_CONSECUTIVE_FAILURES = 2


class SessionKeeper:
    """Keeps the HPOS session alive via periodic JS fetch in the browser.

    The ping runs on a daemon timer thread, but acquires ``driver_lock``
    before touching the WebDriver.  All code that uses the same driver
    should also acquire this lock to prevent concurrent Selenium calls.
    """

    KEEPALIVE_URL = (
        "https://www2.medicareaustralia.gov.au:5447"
        "/pcert/hpos/faces/landingHome.xhtml"
    )

    def __init__(self, driver, interval_seconds: int = 300,
                 driver_lock: threading.Lock | None = None):
        self.driver = driver
        self.interval = interval_seconds
        self.driver_lock = driver_lock or threading.Lock()
        self._timer = None
        self._running = False
        self._session_lost = threading.Event()
        self._consecutive_failures = 0

    @property
    def is_session_valid(self) -> bool:
        return not self._session_lost.is_set()

    def start(self):
        self._running = True
        self._session_lost.clear()
        self._consecutive_failures = 0
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
            self._consecutive_failures = 0
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
            with self.driver_lock:
                result = self.driver.execute_script(
                    "try { var x = new XMLHttpRequest();"
                    f"x.open('GET', '{self.KEEPALIVE_URL}', false);"
                    "x.send();"
                    "return {status: x.status, body: x.responseText.substring(0, 500)}; }"
                    "catch(e) { return {status: -1, body: String(e)}; }"
                )
                status = result.get("status", -1)
                body = (result.get("body", "") or "").lower()

                if status == 200 and not self._response_indicates_session_lost(body):
                    log("Session keep-alive ping successful")
                    self._consecutive_failures = 0
                    self._schedule_next()
                    return

                log(f"Session keep-alive ping: status {status}, "
                    f"body snippet: {body[:120]}")
        except Exception as e:
            log(f"Session keep-alive ping failed: {e}")

        # Transient failure — retry before declaring session lost
        self._consecutive_failures += 1
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._signal_session_lost()
        else:
            log(f"Ping failure {self._consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}, "
                "will retry next interval")
            self._schedule_next()

    @staticmethod
    def _response_indicates_session_lost(body_lower: str) -> bool:
        """Return True if the response body looks like a login/redirect page."""
        return any(pat in body_lower for pat in _SESSION_LOST_PATTERNS)

    def _signal_session_lost(self):
        """Mark the session as lost and stop scheduling further pings."""
        if not self._session_lost.is_set():
            log("SESSION LOST: another login may have invalidated this session")
        self._session_lost.set()
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
