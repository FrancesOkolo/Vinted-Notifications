import json
import proxies
import sys
import os
import db
import random
import requests
import threading
import time
from urllib.parse import urlsplit

from scraper_rate import (
    REQUEST_JITTER_MAX_SECONDS,
    bounded_request_spacing,
)

# Add the parent directory to sys.path to import logger
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

REQUEST_TIMEOUT = (10, 30)
FORBIDDEN_RETRY_DELAY_SECONDS = 3
SHARED_REQUEST_LEASE_SECONDS = 120.0
SHARED_REQUEST_GATE_POLL_SECONDS = 1.0
# A reset/aborted TCP connection (typically a stale keep-alive socket reused on
# the first request of a scrape cycle) is transient; retry a couple of times on
# a fresh connection before surfacing it as a network error.
CONNECTION_RESET_MAX_RETRIES = 2
CATALOGUE_REQUEST_SPACING_PARAMETER = "catalogue_request_spacing_seconds"
_CATALOGUE_REQUEST_GATE_LOCK = threading.Lock()
_CATALOGUE_LAST_COMPLETED_AT = None
_SHARED_REQUEST_GATE_LOCK = None
_SHARED_REQUEST_NEXT_ALLOWED = None
_SHARED_REQUEST_LEASE_UNTIL = None
_SHARED_REQUEST_OWNER_COUNTER = None
_SHARED_REQUEST_CURRENT_OWNER = None


def catalogue_request_spacing_seconds():
    """Read the process-wide catalogue gap without allowing unsafe values."""
    try:
        configured = db.get_parameter(CATALOGUE_REQUEST_SPACING_PARAMETER)
    except Exception:
        configured = None
    return bounded_request_spacing(configured)


def configure_shared_request_gate(
    lock=None,
    next_allowed=None,
    lease_until=None,
    owner_counter=None,
    current_owner=None,
):
    """Attach parent-owned pacing state shared by all Vinted child processes."""
    global _SHARED_REQUEST_GATE_LOCK
    global _SHARED_REQUEST_NEXT_ALLOWED
    global _SHARED_REQUEST_LEASE_UNTIL
    global _SHARED_REQUEST_OWNER_COUNTER
    global _SHARED_REQUEST_CURRENT_OWNER
    _SHARED_REQUEST_GATE_LOCK = lock
    _SHARED_REQUEST_NEXT_ALLOWED = next_allowed
    _SHARED_REQUEST_LEASE_UNTIL = lease_until
    _SHARED_REQUEST_OWNER_COUNTER = owner_counter
    _SHARED_REQUEST_CURRENT_OWNER = current_owner


def _is_vinted_request(url):
    """Return True only when a URL hostname contains the exact Vinted label."""
    try:
        hostname = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return "vinted" in hostname.split(".")


def _shared_gate_configured():
    return (
        _SHARED_REQUEST_GATE_LOCK is not None
        and _SHARED_REQUEST_NEXT_ALLOWED is not None
        and hasattr(_SHARED_REQUEST_NEXT_ALLOWED, "value")
        and _SHARED_REQUEST_LEASE_UNTIL is not None
        and hasattr(_SHARED_REQUEST_LEASE_UNTIL, "value")
        and _SHARED_REQUEST_OWNER_COUNTER is not None
        and hasattr(_SHARED_REQUEST_OWNER_COUNTER, "value")
        and _SHARED_REQUEST_CURRENT_OWNER is not None
        and hasattr(_SHARED_REQUEST_CURRENT_OWNER, "value")
    )


def _cancel_requested(cancel_if):
    if cancel_if is None:
        return False
    try:
        return bool(cancel_if())
    except Exception:
        logger.warning(
            "Optional Vinted request cancel check failed; skipping the request.",
            exc_info=True,
        )
        return True


def _wait_for_shared_request_slot(cancel_if=None):
    """Acquire one expiring owner lease without holding the lock over HTTP."""
    while True:
        if _cancel_requested(cancel_if):
            return None
        with _SHARED_REQUEST_GATE_LOCK:
            now = time.monotonic()
            current_owner = int(_SHARED_REQUEST_CURRENT_OWNER.value or 0)
            lease_until = float(_SHARED_REQUEST_LEASE_UNTIL.value or 0.0)
            next_allowed = float(_SHARED_REQUEST_NEXT_ALLOWED.value or 0.0)

            if current_owner and lease_until <= now:
                # Recover a lease abandoned by a terminated child. A late
                # completion carries the old token and cannot clear a new owner.
                _SHARED_REQUEST_CURRENT_OWNER.value = 0
                _SHARED_REQUEST_LEASE_UNTIL.value = 0.0
                current_owner = 0

            if not current_owner and now >= next_allowed:
                token = int(_SHARED_REQUEST_OWNER_COUNTER.value or 0) + 1
                if token <= 0:
                    token = 1
                _SHARED_REQUEST_OWNER_COUNTER.value = token
                _SHARED_REQUEST_CURRENT_OWNER.value = token
                _SHARED_REQUEST_LEASE_UNTIL.value = now + SHARED_REQUEST_LEASE_SECONDS
                return token

            target = lease_until if current_owner else next_allowed
            delay = max(
                0.001,
                min(SHARED_REQUEST_GATE_POLL_SECONDS, max(0.0, target - now)),
            )
        time.sleep(delay)


def _cancel_shared_request_slot(token):
    if token is None or not _shared_gate_configured():
        return
    with _SHARED_REQUEST_GATE_LOCK:
        if int(_SHARED_REQUEST_CURRENT_OWNER.value or 0) == int(token):
            _SHARED_REQUEST_CURRENT_OWNER.value = 0
            _SHARED_REQUEST_LEASE_UNTIL.value = 0.0


def _mark_shared_request_completed(token):
    if token is None or not _shared_gate_configured():
        return False
    completed = time.monotonic()
    next_start = (
        completed
        + catalogue_request_spacing_seconds()
        + random.uniform(0.0, REQUEST_JITTER_MAX_SECONDS)
    )
    with _SHARED_REQUEST_GATE_LOCK:
        if int(_SHARED_REQUEST_CURRENT_OWNER.value or 0) != int(token):
            return False
        _SHARED_REQUEST_NEXT_ALLOWED.value = next_start
        _SHARED_REQUEST_CURRENT_OWNER.value = 0
        _SHARED_REQUEST_LEASE_UNTIL.value = 0.0
        return True


def _reset_catalogue_request_gate():
    """Reset process-local Vinted timing state (used at start and in tests)."""
    global _CATALOGUE_LAST_COMPLETED_AT
    with _CATALOGUE_REQUEST_GATE_LOCK:
        _CATALOGUE_LAST_COMPLETED_AT = None


def _session_request(
    session,
    method,
    url,
    params=None,
    force_gate=False,
    cancel_if=None,
):
    """Issue one request, serializing every Vinted HTTP attempt.

    The parent-shared state coordinates every process that contacts Vinted. A
    process-local lock remains held through HTTP, while the multiprocessing
    lock is held only long enough to reserve/update a slot.
    """
    global _CATALOGUE_LAST_COMPLETED_AT

    request = getattr(session, method)
    if not force_gate and not _is_vinted_request(url):
        return request(url, params=params, timeout=REQUEST_TIMEOUT)

    with _CATALOGUE_REQUEST_GATE_LOCK:
        if _cancel_requested(cancel_if):
            return None
        shared_token = None
        if _shared_gate_configured():
            shared_token = _wait_for_shared_request_slot(cancel_if=cancel_if)
            if shared_token is None:
                return None
        elif _CATALOGUE_LAST_COMPLETED_AT is not None:
            earliest = (
                _CATALOGUE_LAST_COMPLETED_AT
                + catalogue_request_spacing_seconds()
                + random.uniform(0.0, REQUEST_JITTER_MAX_SECONDS)
            )
            delay = max(0.0, earliest - time.monotonic())
            if delay:
                time.sleep(delay)
        if _cancel_requested(cancel_if):
            _cancel_shared_request_slot(shared_token)
            return None
        try:
            return request(url, params=params, timeout=REQUEST_TIMEOUT)
        finally:
            _CATALOGUE_LAST_COMPLETED_AT = time.monotonic()
            _mark_shared_request_completed(shared_token)


class Requester:
    """
    A class for handling HTTP requests to Vinted.

    This class manages session headers, cookies, and provides methods for making
    HTTP requests with retry logic for handling authentication issues.
    """

    def __init__(self, debug=False):
        """
        Initialize the Requester with default headers and session.

        Sets up the request headers with a randomly selected User-Agent,
        initializes the session, and configures default settings.

        Args:
            debug (bool, optional): Whether to print debug messages. Defaults to False.
        """

        # Add the parent directory to sys.path to import db
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import db

        # Get user agents and default headers from the database
        user_agents_json = db.get_parameter("user_agents")
        default_headers_json = db.get_parameter("default_headers")

        # Parse JSON strings
        user_agents = json.loads(user_agents_json) if user_agents_json else []
        default_headers = (
            json.loads(default_headers_json) if default_headers_json else {}
        )

        self.HEADER = {
            # Grabs a user agent from the database
            "User-Agent": random.choice(user_agents) if user_agents else "Mozilla/5.0",
            **(default_headers or {}),
            "Host": "www.vinted.fr",
        }
        self.VINTED_AUTH_URL = "https://www.vinted.fr/"
        self.MAX_RETRIES = 3
        self.session = requests.Session()
        self.session.headers.update(self.HEADER)
        self.debug = debug

        if self.debug:
            logger.debug(f"Using User-Agent: {self.HEADER['User-Agent']}")

    def set_locale(self, locale):
        """
        Set the locale of the requester.

        Updates the authentication URL and headers to use the specified locale.

        Args:
            locale (str): The locale domain to use (e.g., 'www.vinted.fr', 'www.vinted.de')
        """
        self.VINTED_AUTH_URL = f"https://{locale}/"
        # Get user agents and default headers from the database
        user_agents_json = db.get_parameter("user_agents")
        default_headers_json = db.get_parameter("default_headers")

        # Parse JSON strings
        user_agents = json.loads(user_agents_json) if user_agents_json else []
        default_headers = (
            json.loads(default_headers_json) if default_headers_json else {}
        )

        self.HEADER = {
            "User-Agent": random.choice(user_agents) if user_agents else "Mozilla/5.0",
            **(default_headers or {}),
            "Host": f"{locale}",
        }
        self.session.headers.update(self.HEADER)
        if self.debug:
            logger.debug(
                f"Locale set to {locale} with User-Agent: {self.HEADER['User-Agent']}"
            )

    def get_once(self, url, params=None, cancel_if=None):
        """Make exactly one paced GET without cookie/authentication retries."""
        proxy_configured = proxies.configure_proxy(self.session)
        if self.debug and proxy_configured:
            logger.debug("Using configured proxy")
        return _session_request(
            self.session,
            "get",
            url,
            params=params,
            force_gate=_is_vinted_request(url),
            cancel_if=cancel_if,
        )

    def get(self, url, params=None):
        """
        Make a GET request with retry logic.

        Authentication failures may refresh cookies and retry. The first 403
        gets one controlled retry with a completely fresh session; only a 403
        from that fresh session is returned to the scraper-level circuit
        breaker. Rate-limit responses are still returned immediately.

        Args:
            url (str): The URL to request
            params (dict, optional): Query parameters for the request

        Returns:
            requests.Response: The response object if successful

        Raises:
            HTTPError: If the request fails after all retries
        """

        # Set a random proxy for this request
        proxy_configured = proxies.configure_proxy(self.session)
        if self.debug and proxy_configured:
            logger.debug("Using configured proxy")

        forbidden_retry_used = False
        unauthorized_retry_used = False
        authentication_attempt = 1
        connection_retry = 0

        while True:
            try:
                response = _session_request(self.session, "get", url, params=params)
            except requests.exceptions.ConnectionError as error:
                # Almost always a stale keep-alive socket being reused on the
                # first request of a cycle (Vinted drops idle sockets between
                # scrapes). A retry dials a fresh connection and succeeds;
                # only surface the error to the scraper after a few tries.
                if connection_retry >= CONNECTION_RESET_MAX_RETRIES:
                    raise
                connection_retry += 1
                logger.warning(
                    "Connection to Vinted was reset (%s); retrying %s/%s.",
                    error.__class__.__name__,
                    connection_retry,
                    CONNECTION_RESET_MAX_RETRIES,
                )
                time.sleep(1)
                continue

            with response:
                if response.status_code == 200:
                    return response

                # A 403 can also mean that this session's cookies were rejected.
                # Retry it exactly once with a fresh session, rather than either
                # treating the first response as a site-wide block or repeating
                # the old six-request retry burst.
                if response.status_code == 403 and not forbidden_retry_used:
                    forbidden_retry_used = True
                    logger.warning(
                        "Vinted returned HTTP 403; refreshing the session and "
                        "retrying this query once."
                    )
                    self._rebuild_session()
                    time.sleep(FORBIDDEN_RETRY_DELAY_SECONDS)
                    continue

                # The catalogue session token expires independently of the
                # process lifetime (typically after about a day). Refreshing
                # cookies on the rejected session did not recover that state,
                # so replace the complete session and retry exactly once. A
                # second 401 is returned to the scraper-level global circuit
                # breaker instead of starting an unbounded per-query loop.
                if response.status_code == 401 and not unauthorized_retry_used:
                    unauthorized_retry_used = True
                    logger.warning(
                        "Vinted returned HTTP 401; rebuilding the session and "
                        "retrying this query once."
                    )
                    self._rebuild_session()
                    continue

                if response.status_code in (403, 429):
                    logger.warning(
                        "Vinted returned confirmed HTTP %s; deferring further "
                        "retry policy to the scraper circuit breaker.",
                        response.status_code,
                    )
                    return response

                if (
                    response.status_code == 404
                    and authentication_attempt < self.MAX_RETRIES
                ):
                    print(
                        "Cookies invalid, retrying "
                        f"{authentication_attempt}/{self.MAX_RETRIES}"
                    )
                    if self.debug:
                        logger.debug(
                            "Cookies invalid retrying %s/%s",
                            authentication_attempt,
                            self.MAX_RETRIES,
                        )
                    authentication_attempt += 1
                    self.set_cookies()
                    continue

                return response

    def _rebuild_session(self):
        """Replace the HTTP session and obtain new cookies for one 403 retry."""
        old_session = self.session
        try:
            old_session.close()
        except Exception:
            if self.debug:
                logger.debug("Could not close the rejected Vinted session.")

        self.session = requests.Session()
        self.session.headers.update(self.HEADER)
        proxies.configure_proxy(self.session)
        self.set_cookies()

    def post(self, url, params=None):
        """
        Make a POST request.

        Args:
            url (str): The URL to request
            params (dict, optional): Parameters for the request

        Returns:
            requests.Response: The response object if successful

        Raises:
            HTTPError: If the request fails
        """
        # Set a random proxy for this request
        proxy_configured = proxies.configure_proxy(self.session)
        if self.debug and proxy_configured:
            logger.debug("Using configured proxy")

        response = _session_request(
            self.session,
            "post",
            url,
            params=params,
            force_gate=_is_vinted_request(url),
        )
        response.raise_for_status()
        return response

    def set_cookies(self):
        """
        Reset and fetch new cookies for authentication.

        Clears the current session cookies and makes a HEAD request to
        the Vinted authentication URL to get new cookies.
        """
        self.session.cookies.clear_session_cookies()
        try:
            _session_request(
                self.session,
                "head",
                self.VINTED_AUTH_URL,
                force_gate=True,
            )
            if self.debug:
                logger.debug("Cookies set!")
        except Exception:
            if self.debug:
                logger.error(
                    "There was an error fetching cookies for vinted", exc_info=True
                )

    def update_cookies(self, cookies: dict):
        """
        Update the session cookies with the provided dictionary.

        Args:
            cookies (dict): Dictionary of cookies to update
        """
        self.session.cookies.update(cookies)
        if self.debug:
            logger.debug(f"Cookies manually updated ({len(cookies)} cookies received)")

    # Alias for backward compatibility
    setLocale = set_locale
    setCookies = set_cookies


# Singleton instance of the Requester class
requester = Requester()
