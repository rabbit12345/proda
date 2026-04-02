from __future__ import annotations

import threading

from .waits import log

# Patterns checked (lowercased) against:
#   - XHR response body (keepalive ping)
#   - current browser page title / body snippet
_SESSION_LOST_BODY_PATTERNS = (
    "loginformandstuff",       # PRODA login form element ID
    "sign in to proda",        # PRODA login page heading
    "session has expired",
    "session timeout",
    "your session has timed",  # HPOS variant
    "you have been logged out",
    "logged out",
    "unauthori",               # "unauthorized" / "unauthorised"
    "access denied",
)

# Substrings checked (lowercased) against the current browser URL and the
# XHR responseURL (final URL after any redirects).
_SESSION_LOST_URL_PATTERNS = (
    "proda.humanservices",     # PRODA identity-provider domain
    "proda.gov",
    "/login",
    "/signin",
    "signin",
    "loggedout",
    "logged-out",
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
        self._schedule_lock = threading.Lock()
        self._generation = 0

    @property
    def is_session_valid(self) -> bool:
        return not self._session_lost.is_set()

    def start(self):
        self._running = True
        self._session_lost.clear()
        self._consecutive_failures = 0
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
                self._consecutive_failures = 0
                self._schedule_next_locked()

    def _schedule_next(self):
        with self._schedule_lock:
            self._schedule_next_locked()

    def _schedule_next_locked(self):
        """Must be called while holding self._schedule_lock."""
        if self._running:
            gen = self._generation
            self._timer = threading.Timer(self.interval, self._ping, args=[gen])
            self._timer.daemon = True
            self._timer.start()

    def _ping(self, generation):
        if not self._running:
            return
        if generation != self._generation:
            return  # A reset() happened after this ping was scheduled; discard
        try:
            try:
                with self.driver_lock:
                    # ── 1. Check where the browser actually is right now ──────
                    # This catches JS-redirect / meta-refresh logouts that XHR
                    # would never see, and requires no network round-trip.
                    current_url = (self.driver.current_url or "").lower()
                    current_title = (self.driver.title or "").lower()
                    if (self._url_indicates_session_lost(current_url)
                            or self._body_indicates_session_lost(current_title)):
                        log(f"Session lost detected via browser page: "
                            f"url={current_url!r} title={current_title!r}")
                        self._signal_session_lost()
                        return

                    # ── 2. fetch keepalive ping ──────────────────────────────
                    # Use execute_async_script + fetch instead of synchronous
                    # XHR (which is deprecated and is killed by the 5-second
                    # script_timeout set by wait_for_page_load).
                    self.driver.set_script_timeout(15)
                    result = self.driver.execute_async_script(
                        "var done = arguments[arguments.length - 1];"
                        f"fetch('{self.KEEPALIVE_URL}',"
                        " {method:'GET', credentials:'include', cache:'no-cache'})"
                        ".then(function(r){"
                        "  return r.text().then(function(t){"
                        "    done({status:r.status, url:r.url,"
                        "          body:t.substring(0,2000)});"
                        "  });"
                        "}).catch(function(e){"
                        "  done({status:-1, url:'', body:String(e)});"
                        "});"
                    )
                    self.driver.set_script_timeout(5)
                    status = result.get("status", -1)
                    response_url = (result.get("url") or "").lower()
                    body = (result.get("body", "") or "").lower()

                    # Redirect to a login/PRODA URL is a definitive sign.
                    # fetch() follows redirects and sets Response.url to the
                    # final destination, so a cross-domain redirect is visible
                    # here even though the body would be CORS-blocked.
                    if self._url_indicates_session_lost(response_url):
                        log(f"Session lost: XHR redirected to {response_url!r}")
                        self._signal_session_lost()
                        return

                    if status == 200 and not self._body_indicates_session_lost(body):
                        log("Session keep-alive ping successful")
                        self._consecutive_failures = 0
                        self._schedule_next()
                        return

                    log(f"Session keep-alive ping: status={status} "
                        f"responseURL={response_url!r} body={body[:120]!r}")
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
        except Exception as e:
            log(f"Session keep-alive unexpected error: {e}")
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._signal_session_lost()
            else:
                self._schedule_next()

    @staticmethod
    def _body_indicates_session_lost(text_lower: str) -> bool:
        """Return True if a page body / response body looks like a login page."""
        return any(pat in text_lower for pat in _SESSION_LOST_BODY_PATTERNS)

    @staticmethod
    def _url_indicates_session_lost(url_lower: str) -> bool:
        """Return True if a URL (current page or XHR responseURL) points to a
        login / logged-out destination."""
        return any(pat in url_lower for pat in _SESSION_LOST_URL_PATTERNS)

    def mark_session_lost(self):
        """Forcibly mark the session as lost. Call when external code
        detects the browser has navigated to a login / session-expired page."""
        self._signal_session_lost()

    def _signal_session_lost(self):
        """Mark the session as lost and stop scheduling further pings."""
        if not self._session_lost.is_set():
            log("SESSION LOST: another login may have invalidated this session")
        self._session_lost.set()
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
