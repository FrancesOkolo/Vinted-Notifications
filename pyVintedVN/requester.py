import json
import proxies
import sys
import os
import db
import random
import requests
import time

# Add the parent directory to sys.path to import logger
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

REQUEST_TIMEOUT = (10, 30)
FORBIDDEN_RETRY_DELAY_SECONDS = 3


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
        authentication_attempt = 1

        while True:
            with self.session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            ) as response:
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

                if response.status_code in (403, 429):
                    logger.warning(
                        "Vinted returned confirmed HTTP %s; deferring further "
                        "retry policy to the scraper circuit breaker.",
                        response.status_code,
                    )
                    return response

                if (
                    response.status_code in (401, 404)
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

        response = self.session.post(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
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
            self.session.head(
                self.VINTED_AUTH_URL,
                timeout=REQUEST_TIMEOUT,
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
