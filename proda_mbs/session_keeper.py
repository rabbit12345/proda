from __future__ import annotations

import threading
import time

from .page_state import PageStateDetector, PortalPageState
from .waits import log


class SessionKeeper:
    """Keep the portal session alive while the workflow is idle.

    Every interval the background timer tries to take the driver lock without
    blocking. If the foreground workflow is busy the session is being used
    anyway, so nothing needs to happen. If the lock is free, the keeper takes a
    page-state snapshot; a logged-off/expired state marks the session lost so
    recovery can run, otherwise it fires a same-origin fetch from the page so
    the server-side session is renewed without touching the DOM.
    """

    def __init__(
        self,
        driver,
        interval_seconds: int = 300,
        driver_lock: threading.Lock | None = None,
        after_ping=None,
    ):
        self.driver = driver
        self._detector = PageStateDetector(driver)
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
        with self._schedule_lock:
            self._running = False
            self._generation += 1
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

        renewed = False
        if self.driver_lock.acquire(blocking=False):
            try:
                # Re-check after acquiring the lock: stop()/mark_session_lost()
                # may have run while the timer was firing, and the driver may
                # be tearing down. Pinging it then can wedge the process.
                if not self._running or generation != self._generation:
                    return
                renewed = self._do_keepalive()
            finally:
                self.driver_lock.release()

            if not renewed and not self._session_lost.is_set():
                self._refresh_due.set()
                idle_seconds = int(time.monotonic() - self._last_reset_at)
                log(
                    "Keepalive ping failed; next foreground action will validate "
                    f"page state and refresh or relogin as needed (idle {idle_seconds}s)"
                )
        # else: foreground is actively using the browser under the lock, so the
        # session is being kept alive by real activity — nothing to do.

        # Only run side effects (console refocus / clipboard) on a genuine
        # renewal, never on no-op or failed pings.
        if renewed and self._after_ping:
            self._after_ping()
        with self._schedule_lock:
            if self._running and generation == self._generation:
                self._schedule_next_locked()

    def _do_keepalive(self) -> bool:
        """Validate the session, then renew it. Returns True only on a genuine
        renewal of a still-live session."""
        try:
            snapshot = self._detector.snapshot()

            if snapshot.state == PortalPageState.BROWSER_UNAVAILABLE:
                # Transient transport glitch reading the page. Don't tear the
                # keeper down on a single bad read — defer to the next
                # foreground action to validate and recover.
                return False

            if snapshot.needs_relogin:
                # A positive logged-off/expired signal (login form, OTP page,
                # session-expired or logged-out interstitial, off-site URL).
                self.mark_session_lost()
                return False

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
        with self._schedule_lock:
            already_lost = self._session_lost.is_set()
            self._session_lost.set()
            self._running = False
            self._generation += 1
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if not already_lost:
            log("SESSION LOST: session expired, logged out, or browser state diverged")
