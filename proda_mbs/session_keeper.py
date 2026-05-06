from __future__ import annotations

import threading
import time

from .waits import log


class SessionKeeper:
    """Track when the foreground workflow must refresh or revalidate session state.

    This class deliberately does not touch Selenium from a background thread.
    The timer only marks that a refresh check is due; the foreground workflow
    performs all browser inspection, reset, refresh, and relogin actions.
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
        self._keepalive_action = None
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

    def set_keepalive_action(self, action):
        self._keepalive_action = action

    def _schedule_next_locked(self):
        if self._running:
            generation = self._generation
            self._timer = threading.Timer(self.interval, self._ping, args=[generation])
            self._timer.daemon = True
            self._timer.start()

    def _ping(self, generation):
        if not self._running or generation != self._generation:
            return

        self._refresh_due.set()
        idle_seconds = int(time.monotonic() - self._last_reset_at)
        log(
            "Session refresh due; next foreground action will validate page state "
            f"and refresh or relogin as needed (idle {idle_seconds}s)"
        )
        if self._after_ping:
            self._after_ping()
        with self._schedule_lock:
            self._schedule_next_locked()

    def mark_session_lost(self):
        if not self._session_lost.is_set():
            log("SESSION LOST: session expired, logged out, or browser state diverged")
        self._session_lost.set()
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
