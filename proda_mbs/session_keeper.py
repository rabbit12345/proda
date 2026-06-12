from __future__ import annotations

import threading
import time

from selenium.webdriver.common.by import By

from .waits import log

_LOGGED_OFF_URL_MARKERS = (
    "timeout.jsf",
    "/timeout",
    "ajaxtimeout",
    "loggedout",
    "logged-out",
)


class SessionKeeper:
    """Keep the portal session alive while the workflow is idle.

    Every interval the background timer tries to take the driver lock without
    blocking. If the foreground workflow is busy the session is being used
    anyway, so nothing needs to happen. If the lock is free, the keeper checks
    for an obvious logged-off state (URL markers or the login field) and
    otherwise fires a same-origin fetch from the page so the server-side
    session is renewed without touching the DOM.
    """

    def __init__(
        self,
        driver,
        interval_seconds: int = 300,
        driver_lock: threading.Lock | None = None,
        after_ping=None,
    ):
        self.driver = driver
        self.interval = interval_seconds
        self.driver_lock = driver_lock or threading.Lock()
        self._after_ping = after_ping
        self._timer = None
        self._running = False
        self._session_lost = threading.Event()
        self._refresh_due = threading.Event()
        self._schedule_lock = threading.Lock()
        self._generation = 0
        self._last_reset_at = time.monotonic()

    @property
    def is_session_valid(self) -> bool:
        return not self._session_lost.is_set()

    @property
    def needs_refresh(self) -> bool:
        return self._refresh_due.is_set()

    def start(self):
        self._running = True
        self._session_lost.clear()
        self._refresh_due.clear()
        self._last_reset_at = time.monotonic()
        with self._schedule_lock:
            self._generation += 1
            self._schedule_next_locked()
        log(f"Session keeper started (interval: {self.interval}s)")

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        log("Session keeper stopped")

    def reset(self):
        if self._running:
            with self._schedule_lock:
                self._generation += 1
                if self._timer:
                    self._timer.cancel()
                self._refresh_due.clear()
                self._last_reset_at = time.monotonic()
                self._schedule_next_locked()

    def _schedule_next_locked(self):
        if self._running:
            generation = self._generation
            self._timer = threading.Timer(self.interval, self._ping, args=[generation])
            self._timer.daemon = True
            self._timer.start()

    def _ping(self, generation):
        if not self._running or generation != self._generation:
            return

        pinged = False
        if self.driver_lock.acquire(blocking=False):
            try:
                pinged = self._do_keepalive()
            finally:
                self.driver_lock.release()
        else:
            # Foreground is actively using the browser, so the session is
            # being kept alive by real activity.
            pinged = True

        if not pinged:
            self._refresh_due.set()
            idle_seconds = int(time.monotonic() - self._last_reset_at)
            log(
                "Keepalive ping failed; next foreground action will validate "
                f"page state and refresh or relogin as needed (idle {idle_seconds}s)"
            )

        if self._after_ping:
            self._after_ping()
        with self._schedule_lock:
            self._schedule_next_locked()

    def _do_keepalive(self) -> bool:
        try:
            url = str(self.driver.current_url or "").lower()
            logged_off = any(marker in url for marker in _LOGGED_OFF_URL_MARKERS)
            if not logged_off:
                logged_off = bool(
                    self.driver.find_elements(By.ID, "loginFormAndStuff:username")
                )
            if logged_off:
                self.mark_session_lost()
                return True

            self.driver.execute_script(
                "try { fetch(window.location.href, "
                "{credentials: 'same-origin', cache: 'no-store'}); } catch (e) {}"
            )
            self._last_reset_at = time.monotonic()
            log("Keepalive ping sent (session renewed)")
            return True
        except Exception as exc:
            log(f"Keepalive ping error: {exc}")
            return False

    def mark_session_lost(self):
        if not self._session_lost.is_set():
            log("SESSION LOST: session expired, logged out, or browser state diverged")
        self._session_lost.set()
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
