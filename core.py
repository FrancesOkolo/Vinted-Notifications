import db
import html
import json
import random
import requests
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from email.utils import parsedate_to_datetime
from pyVintedVN import Vinted, requester
from urllib.parse import urlparse, parse_qs
from logger import get_logger
from url_normalizer import normalise_vinted_url

# Get logger for this module
logger = get_logger(__name__)

_VERSION_CACHE = None
_VERSION_CACHE_TIME = 0.0
_VERSION_CACHE_TTL_SECONDS = 6 * 60 * 60
_VERSION_CACHE_LOCK = threading.Lock()
_ITEM_PAGE_REQUEST_TIMEOUT = (5, 10)
_SCRAPER_FORBIDDEN_LIMIT = 3
_SCRAPER_COOLDOWN_SECONDS = (5 * 60, 10 * 60, 15 * 60)
_SCRAPER_MIN_QUERY_SPACING_SECONDS = 1.0
_SCRAPER_MAX_QUERY_SPACING_SECONDS = 5.0
_SCRAPER_TARGET_ACTIVE_FRACTION = 0.50
_ITEM_PAGE_NAVIGATION_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


def process_query(query, name=None, chat_id=None):
    """
    Normalise a Vinted URL, create one shared query if necessary, and
    subscribe the requesting Telegram account to it.

    When chat_id is omitted, the configured primary Telegram chat is used.
    This keeps web-added queries routed to the primary account.
    """
    processed_query = normalise_vinted_url(query)

    if chat_id is None:
        configured_chat_id = db.get_parameter("telegram_chat_id")
        chat_id = str(configured_chat_id).strip() if configured_chat_id else None

    if chat_id is not None and not db.is_telegram_user_approved(chat_id):
        return "This Telegram account is not approved.", False

    query_id, query_created, subscription_created = db.add_query_to_db(
        processed_query,
        name=name,
        chat_id=chat_id,
    )

    if query_id is None:
        return "Failed to add query.", False

    if chat_id is not None and not subscription_created:
        return "You already follow this query.", False

    if query_created:
        return "Query added.", True

    return "Query already existed; you are now subscribed to it.", True


def get_formatted_query_list(chat_id=None):
    """
    Return a numbered query list. Telegram users see only searches to
    which their account is subscribed.
    """
    all_queries = db.get_queries(chat_id=chat_id)
    queries_keywords = []

    for query in all_queries:
        parsed_url = urlparse(query[1])
        query_params = parse_qs(parsed_url.query)
        query_name = query[3] or query_params.get("search_text", [None])[0]

        if not query_name:
            query_name = query[1]

        queries_keywords.append(str(query_name))

    if not queries_keywords:
        return "No queries saved."

    return "\n".join(
        f"{index}. {query_name}"
        for index, query_name in enumerate(queries_keywords, start=1)
    )


def process_remove_query(number, chat_id=None):
    """
    Remove a query globally for web/admin calls, or unsubscribe only the
    requesting Telegram user when chat_id is supplied.
    """
    if number == "all":
        if chat_id is None:
            success = db.remove_all_queries_from_db()
            return (
                ("All queries removed.", True)
                if success
                else ("Failed to remove queries.", False)
            )

        success = db.remove_all_query_subscriptions(chat_id)
        return (
            ("You have been unsubscribed from all queries.", True)
            if success
            else ("Failed to remove your queries.", False)
        )

    if not str(number).isdigit():
        return "Invalid number.", False

    query_id = int(number)

    if chat_id is None:
        success = db.remove_query_from_db(query_id)
        return (
            ("Query removed.", True) if success else ("Failed to remove query.", False)
        )

    success = db.remove_query_subscription(query_id, chat_id)
    return (
        ("Query removed from your account.", True)
        if success
        else ("Query not found in your account.", False)
    )


def process_update_query(query_id, query, name):
    """
    Normalise and update a query from the web interface.
    """
    processed_query = normalise_vinted_url(query)

    if db.update_query_in_db(query_id, processed_query, name):
        return "Query updated.", True

    return "Failed to update query.", False


def process_add_country(country):
    """
    Process the addition of a country to the allowlist.

    Args:
        country (str): The country code to add

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")
    country_list = db.get_allowlist()

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", country_list

    # Check if the country is already in the allowlist
    # If country_list is 0, it means the allowlist is empty
    if country_list != 0 and country.upper() in country_list:
        return f'Country "{country.upper()}" already in allowlist.', country_list

    # Add the country to the allowlist
    db.add_to_allowlist(country.upper())
    return "Country added.", db.get_allowlist()


def process_remove_country(country):
    """
    Process the removal of a country from the allowlist.

    Args:
        country (str): The country code to remove

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", db.get_allowlist()

    # Remove the country from the allowlist
    db.remove_from_allowlist(country.upper())
    return "Country removed.", db.get_allowlist()


def get_user_country(profile_id):
    """
    Get the country code for a Vinted user.

    Makes an API request to retrieve the user's country code.
    Handles rate limiting by trying an alternative endpoint.

    Args:
        profile_id (str): The Vinted user's profile ID

    Returns:
        str: The user's country code (2-letter ISO code) or "XX" if it can't be determined
    """
    # Users are shared between all Vinted platforms, so we can use whatever locale we want
    url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
    response = requester.get(url)
    # That's a LOT of requests, so if we get a 429 we wait a bit before retrying once
    if response.status_code == 429:
        # In case of rate limit, we're switching the endpoint. This one is slower, but it doesn't RL as soon.
        # We're limiting the items per page to 1 to grab as little data as possible
        url = f"https://www.vinted.fr/api/v2/users/{profile_id}/items?page=1&per_page=1"
        response = requester.get(url)
        try:
            user_country = response.json()["items"][0]["user"]["country_iso_code"]
        except KeyError:
            logger.warning(
                "Couldn't get the country due to too many requests. Returning default value."
            )
            user_country = "XX"
    else:
        user_country = response.json()["user"]["country_iso_code"]
    return user_country


def _parse_quiet_time(value, fallback):
    """Parse an HH:MM setting, returning the fallback on invalid data."""
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (TypeError, ValueError):
        logger.warning(
            "Invalid quiet-hours time %r; using %s.",
            value,
            fallback,
        )
        return datetime.strptime(fallback, "%H:%M").time()


def _parse_quiet_days(value):
    """Parse the quiet-hours weekday set (Mon=0 .. Sun=6).

    None means the setting was never configured, which keeps the original
    behaviour of applying quiet hours every day. An explicitly saved empty
    value means no quiet days (quiet hours effectively off).
    """
    if value is None:
        return {0, 1, 2, 3, 4, 5, 6}
    days = set()
    for part in str(value).split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.add(int(part))
    return days


def get_quiet_hours_status(now=None):
    """
    Return (active, start_text, end_text, timezone_name).

    Quiet hours use the configured IANA timezone rather than the server's
    local timezone. This keeps UK quiet hours correct on remote servers that
    run in UTC and automatically follows BST/GMT daylight-saving changes.

    Both ordinary windows (01:00-06:00) and windows crossing midnight
    (23:00-06:00) are supported. Equal start and end times are treated as
    disabled to avoid accidentally pausing the scraper all day.
    """
    enabled = str(db.get_parameter("quiet_hours_enabled") or "False").lower()
    enabled = enabled == "true"

    start_text = str(db.get_parameter("quiet_hours_start") or "01:00")
    end_text = str(db.get_parameter("quiet_hours_end") or "06:00")
    timezone_name = (
        str(db.get_parameter("quiet_hours_timezone") or "Europe/London").strip()
        or "Europe/London"
    )

    if not enabled:
        return False, start_text, end_text, timezone_name

    try:
        quiet_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Unknown quiet-hours timezone %r; using Europe/London.",
            timezone_name,
        )
        timezone_name = "Europe/London"
        try:
            quiet_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.error("Timezone data is unavailable; quiet hours are disabled.")
            return False, start_text, end_text, timezone_name

    start = _parse_quiet_time(start_text, "01:00")
    end = _parse_quiet_time(end_text, "06:00")

    if start == end:
        return False, start_text, end_text, timezone_name

    weekday = None
    if now is None:
        now_local = datetime.now(quiet_timezone)
        current = now_local.time().replace(tzinfo=None)
        weekday = now_local.weekday()
    elif isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=quiet_timezone)
        now_local = now.astimezone(quiet_timezone)
        current = now_local.time().replace(tzinfo=None)
        weekday = now_local.weekday()
    else:
        # A time object is useful for unit tests and is interpreted directly
        # in the configured quiet-hours timezone. Without a date the weekday
        # restriction is skipped.
        current = now.replace(tzinfo=None)

    if start < end:
        active = start <= current < end
    else:
        active = current >= start or current < end

    # Restrict quiet hours to the configured days of the week when the date is
    # known (e.g. leave Sat/Sun unchecked to keep weekends noisy).
    if active and weekday is not None:
        if weekday not in _parse_quiet_days(db.get_parameter("quiet_hours_days")):
            active = False

    return active, start_text, end_text, timezone_name


def _quiet_hours_active():
    return get_quiet_hours_status()[0]


def _get_retry_after_seconds(response, fallback_seconds):
    if response is None:
        return fallback_seconds

    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return fallback_seconds

    try:
        return max(1, int(retry_after))
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = int((retry_at - datetime.now(timezone.utc)).total_seconds())
        return max(1, seconds)
    except (TypeError, ValueError, OverflowError):
        return fallback_seconds


def _get_query_spacing_seconds(query_count):
    if query_count <= 1:
        return 0.0

    try:
        refresh_delay = int(db.get_parameter("query_refresh_delay") or 600)
    except (TypeError, ValueError):
        refresh_delay = 600

    # Pace requests during roughly the first half of the configured interval,
    # then leave a real idle period before the next cycle. The previous
    # nine-minute minimum made a ten-minute schedule almost continuous, which
    # is both unlike the original app and an unnecessarily regular pattern.
    usable_window = max(30, refresh_delay * _SCRAPER_TARGET_ACTIVE_FRACTION)
    calculated_spacing = usable_window / max(1, query_count - 1)
    return max(
        _SCRAPER_MIN_QUERY_SPACING_SECONDS,
        min(_SCRAPER_MAX_QUERY_SPACING_SECONDS, calculated_spacing),
    )


def _parameter_int(key, default=0):
    try:
        return int(db.get_parameter(key) or default)
    except (TypeError, ValueError):
        return default


def get_scraper_cooldown(now=None):
    """Return the persisted Vinted-block cooldown state."""
    now = int(now if now is not None else time.time())
    until = max(0, _parameter_int("scraper_cooldown_until"))
    level = max(0, _parameter_int("scraper_cooldown_level"))
    status_code = _parameter_int("scraper_last_block_status") or None
    remaining = max(0, until - now)
    return {
        "active": remaining > 0,
        "until": until,
        "remaining": remaining,
        "level": level,
        "status_code": status_code,
    }


def _activate_scraper_cooldown(status_code, now=None):
    """Open or escalate the persistent 5/10/15-minute circuit breaker."""
    now = int(now if now is not None else time.time())
    current_level = min(
        get_scraper_cooldown(now=now)["level"],
        len(_SCRAPER_COOLDOWN_SECONDS),
    )
    duration = _SCRAPER_COOLDOWN_SECONDS[
        min(current_level, len(_SCRAPER_COOLDOWN_SECONDS) - 1)
    ]
    new_level = min(current_level + 1, len(_SCRAPER_COOLDOWN_SECONDS))
    until = now + duration
    if not db.set_parameters(
        {
            "scraper_cooldown_until": str(until),
            "scraper_cooldown_level": str(new_level),
            "scraper_last_block_status": str(status_code),
            "scraper_consecutive_403s": "0",
        }
    ):
        logger.warning("Could not persist the scraper cooldown state.")
    return {
        "active": True,
        "until": until,
        "remaining": duration,
        "level": new_level,
        "status_code": status_code,
    }


def _clear_scraper_cooldown():
    """Reset the circuit breaker after a successful scrape cycle."""
    cooldown = get_scraper_cooldown()
    consecutive_403s = _parameter_int("scraper_consecutive_403s")
    if cooldown["level"] or cooldown["until"] or consecutive_403s:
        if not db.set_parameters(
            {
                "scraper_cooldown_until": "0",
                "scraper_cooldown_level": "0",
                "scraper_last_block_status": "",
                "scraper_consecutive_403s": "0",
            }
        ):
            logger.warning("Could not clear the scraper cooldown state.")


def record_scraper_heartbeat():
    """Record that the scrape cycle just fired, proving the scraper is alive.

    Written at the start of every process_items run — including quiet-hours
    skips, since the process is healthy then, just intentionally idle. The
    main-process watchdog treats a frozen heartbeat as a stalled scraper.
    """
    try:
        db.set_parameter("scraper_heartbeat", str(int(time.time())))
    except Exception:
        logger.warning("Could not record scraper heartbeat.", exc_info=True)


def _finalize_scrape_cycle(
    successful_fetches,
    query_count,
    blocked_status=None,
):
    """Update health counters after a scrape cycle completes."""
    now = int(time.time())
    try:
        db.set_parameter("scraper_last_cycle", str(now))
        if blocked_status is not None:
            failed = _parameter_int("scraper_failed_cycles")
            db.set_parameter("scraper_failed_cycles", str(failed + 1))
        elif successful_fetches > 0:
            db.set_parameter("scraper_last_ok", str(now))
            db.set_parameter("scraper_failed_cycles", "0")
            _clear_scraper_cooldown()
        elif query_count > 0:
            # A full cycle that reached nothing usually means Vinted is
            # blocking every request (403/429), not an empty marketplace.
            failed = _parameter_int("scraper_failed_cycles")
            db.set_parameter("scraper_failed_cycles", str(failed + 1))
    except Exception:
        logger.warning("Could not update scrape-cycle health.", exc_info=True)


def get_scraper_health(now=None):
    """Return a health snapshot for the main-process watchdog.

    Keys:
      heartbeat_age  seconds since the scrape cycle last fired (None if never)
      stalled        heartbeat is older than a few refresh intervals
      blocked        several consecutive cycles reached no items at all
      failed_cycles  current consecutive-failure count
      last_ok_age    seconds since the last successful fetch (None if never)
      stale_after    the staleness threshold in seconds

    A missing heartbeat (fresh boot, before the first cycle) is never reported
    as stalled, avoiding false alarms at startup.
    """
    now = int(now if now is not None else time.time())

    def _age(key):
        raw = db.get_parameter(key)
        try:
            return now - int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    try:
        refresh_delay = int(db.get_parameter("query_refresh_delay") or 300)
    except (TypeError, ValueError):
        refresh_delay = 300
    stale_after = max(refresh_delay * 3, 600)

    heartbeat_age = _age("scraper_heartbeat")
    last_ok_age = _age("scraper_last_ok")

    try:
        failed_cycles = int(db.get_parameter("scraper_failed_cycles") or 0)
    except (TypeError, ValueError):
        failed_cycles = 0
    cooldown = get_scraper_cooldown(now=now)

    return {
        "heartbeat_age": heartbeat_age,
        "stalled": heartbeat_age is not None and heartbeat_age > stale_after,
        # Keep reporting a block after the timer expires until a successful
        # cycle proves that Vinted is accepting requests again.
        "blocked": cooldown["level"] > 0 or failed_cycles >= 3,
        "failed_cycles": failed_cycles,
        "last_ok_age": last_ok_age,
        "stale_after": stale_after,
        "cooldown_active": cooldown["active"],
        "cooldown_remaining": cooldown["remaining"],
        "cooldown_level": cooldown["level"],
        "last_block_status": cooldown["status_code"],
    }


def process_items(queue):
    """
    Scrape every unique query once, pacing requests across the configured
    interval. Shared subscriptions do not create duplicate Vinted requests.
    """
    record_scraper_heartbeat()

    quiet_active, quiet_start, quiet_end, quiet_timezone = get_quiet_hours_status()
    if quiet_active:
        logger.info(
            "Quiet hours active (%s-%s, %s); skipping this scrape cycle.",
            quiet_start,
            quiet_end,
            quiet_timezone,
        )
        return

    cooldown = get_scraper_cooldown()
    if cooldown["active"]:
        logger.warning(
            "Scraper circuit breaker is open after HTTP %s; skipping this "
            "cycle with approximately %s minute(s) remaining.",
            cooldown["status_code"] or "403",
            max(1, (cooldown["remaining"] + 59) // 60),
        )
        return

    # Only scrape enabled queries; paused ones stay in the database but make
    # no requests.
    all_queries = db.get_queries(enabled_only=True)

    if not all_queries:
        logger.info("No active Vinted queries configured.")
        return

    vinted = Vinted()

    try:
        items_per_query = int(db.get_parameter("items_per_query") or 20)
    except (TypeError, ValueError):
        items_per_query = 20

    query_count = len(all_queries)
    base_spacing = _get_query_spacing_seconds(query_count)

    logger.info(
        "Starting paced scrape of %s unique queries with approximately "
        "%.1f seconds between requests.",
        query_count,
        base_spacing,
    )

    total_429s = 0
    successful_fetches = 0
    consecutive_403s = _parameter_int("scraper_consecutive_403s")
    cycle_block_status = None

    for position, query in enumerate(all_queries, start=1):
        if _quiet_hours_active():
            logger.info(
                "Quiet hours began during the scrape. Stopping after %s/%s queries.",
                position - 1,
                query_count,
            )
            break

        query_id = query[0]
        query_url = query[1]
        all_items = None

        # The enabled-query list is a snapshot taken at the beginning of the
        # cycle. Honour a pause made while that cycle is already running.
        if not db.is_query_enabled(query_id):
            logger.info("Skipping query %s because it was paused mid-cycle.", query_id)
            continue

        for attempt in range(2):
            if _quiet_hours_active():
                logger.info(
                    "Quiet hours began before a retry; ending this scrape cycle."
                )
                break

            try:
                all_items = vinted.items.search(
                    query_url,
                    nbr_items=items_per_query,
                )
                consecutive_403s = 0
                break
            except requests.exceptions.HTTPError as error:
                response = error.response
                status_code = response.status_code if response is not None else None

                if status_code == 403:
                    consecutive_403s += 1
                    logger.warning(
                        "Vinted rejected query %s/%s with HTTP 403 "
                        "(%s/%s consecutive).",
                        position,
                        query_count,
                        consecutive_403s,
                        _SCRAPER_FORBIDDEN_LIMIT,
                    )
                    if consecutive_403s >= _SCRAPER_FORBIDDEN_LIMIT:
                        cooldown = _activate_scraper_cooldown(403)
                        cycle_block_status = 403
                        logger.error(
                            "Scraper circuit breaker opened after %s "
                            "consecutive HTTP 403 responses. Stopping the "
                            "cycle and cooling down for %s minutes.",
                            consecutive_403s,
                            cooldown["remaining"] // 60,
                        )
                    break

                consecutive_403s = 0
                if status_code != 429:
                    logger.error(
                        "HTTP error while scraping query %s/%s: %s",
                        position,
                        query_count,
                        query_url,
                        exc_info=True,
                    )
                    break

                total_429s += 1
                if total_429s >= 3:
                    # The outer loop logs and stops the complete cycle. Do not
                    # sleep and issue a fourth rate-limited request first.
                    break

                fallback = 60 * (attempt + 1)
                wait_seconds = _get_retry_after_seconds(
                    response,
                    fallback_seconds=fallback,
                )
                wait_seconds = min(max(wait_seconds, 30), 300)

                logger.warning(
                    "Vinted rate-limited query %s/%s. Waiting %s seconds " "before %s.",
                    position,
                    query_count,
                    wait_seconds,
                    "retrying" if attempt == 0 else "continuing",
                )
                time.sleep(wait_seconds)

                if attempt == 1:
                    logger.error(
                        "Skipping query after repeated 429 responses: %s",
                        query_url,
                    )
            except requests.exceptions.RequestException:
                logger.error(
                    "Network error while scraping query %s/%s: %s",
                    position,
                    query_count,
                    query_url,
                    exc_info=True,
                )
                break
            except Exception:
                logger.error(
                    "Unexpected error while scraping query %s/%s: %s",
                    position,
                    query_count,
                    query_url,
                    exc_info=True,
                )
                break

        if cycle_block_status is not None:
            break

        if all_items is not None:
            successful_fetches += 1
            if db.is_query_enabled(query_id):
                data = [item for item in all_items if item.is_new_item()]
                queue.put((data, query_id))
            else:
                data = []
                logger.info(
                    "Discarding results for query %s because it was paused "
                    "while the request was in progress.",
                    query_id,
                )
            logger.info(
                "Scraped %s items for query %s/%s: %s",
                len(data),
                position,
                query_count,
                query_url,
            )

        if total_429s >= 3:
            logger.error(
                "Stopping this scrape cycle after %s rate-limit responses. "
                "The next scheduled cycle will try again.",
                total_429s,
            )
            break

        if position < query_count and base_spacing > 0:
            jittered_spacing = base_spacing * random.uniform(0.85, 1.15)
            time.sleep(jittered_spacing)

    if cycle_block_status is None:
        db.set_parameter(
            "scraper_consecutive_403s",
            str(consecutive_403s),
        )
    _finalize_scrape_cycle(
        successful_fetches,
        query_count,
        blocked_status=cycle_block_status,
    )


class _ProductJsonLdParser(HTMLParser):
    """Collect JSON-LD documents embedded in an item page."""

    def __init__(self):
        super().__init__()
        self.documents = []
        self._capturing = False
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return

        attributes = {str(name).lower(): value or "" for name, value in attrs}
        if attributes.get("type", "").lower() == "application/ld+json":
            self._capturing = True
            self._chunks = []

    def handle_data(self, data):
        if self._capturing:
            self._chunks.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._capturing:
            self.documents.append("".join(self._chunks))
            self._capturing = False
            self._chunks = []


def _find_product_description(value):
    if isinstance(value, dict):
        item_type = value.get("@type", [])
        item_types = item_type if isinstance(item_type, list) else [item_type]
        if any(str(name).lower() == "product" for name in item_types):
            description = value.get("description")
            if description:
                return str(description).strip()

        for child in value.values():
            description = _find_product_description(child)
            if description:
                return description

    elif isinstance(value, list):
        for child in value:
            description = _find_product_description(child)
            if description:
                return description

    return None


def _description_from_item_page(page_html):
    parser = _ProductJsonLdParser()
    parser.feed(page_html)

    for document in parser.documents:
        try:
            data = json.loads(html.unescape(document))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

        description = _find_product_description(data)
        if description:
            return description

    return None


def _get_item_description(item):
    description = getattr(item, "description", None)
    if description:
        return str(description).strip()

    item_url = urlparse(str(getattr(item, "url", "")))
    item_id = str(getattr(item, "id", ""))
    if (
        item_url.scheme not in ("http", "https")
        or not item_url.netloc
        or not item_id.isdigit()
    ):
        logger.warning("Could not build a safe detail URL for Vinted item %r", item_id)
        return None

    origin = f"{item_url.scheme}://{item_url.netloc}"
    detail_url = f"{origin}/items/{item_id}"
    navigation_headers = {
        **_ITEM_PAGE_NAVIGATION_HEADERS,
        "Referer": f"{origin}/",
    }

    try:
        # Reuse the catalogue session and its current proxy/cookies, but make
        # only one browser-style page request. Vinted rejects API-style page
        # requests with 403, while a normal same-origin navigation exposes the
        # product description in JSON-LD. Never retry here: enrichment must not
        # amplify rate limits or hold up a notification indefinitely.
        with requester.session.get(
            detail_url,
            headers=navigation_headers,
            timeout=_ITEM_PAGE_REQUEST_TIMEOUT,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            return _description_from_item_page(response.text)
    except requests.exceptions.RequestException as error:
        logger.warning(
            "Could not load the description for Vinted item %s: %s",
            item.id,
            error,
        )
        return None


def _notification_value(value, fallback="Not provided", max_length=None):
    text = str(value).strip() if value is not None else ""
    if not text:
        text = fallback

    if max_length and len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"

    return html.escape(text, quote=False)


def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    if not items_queue.empty():
        data, query_id = items_queue.get()
        if not db.is_query_enabled(query_id):
            logger.info(
                "Discarding queued results for paused query %s.",
                query_id,
            )
            return
        banwords_str = db.get_parameter("banwords")
        for item in reversed(data):

            if not db.is_query_enabled(query_id):
                logger.info(
                    "Stopping queued item processing because query %s was paused.",
                    query_id,
                )
                break

            # If already in db, pass
            last_query_timestamp = db.get_last_timestamp(query_id)
            if (
                last_query_timestamp is not None
                and last_query_timestamp >= item.raw_timestamp
            ):
                pass
            # In case of multiple queries, we need to check if the item is already in the db
            elif db.is_item_in_db_by_id(item.id) is True:
                # We update the timestamp
                db.update_last_timestamp(query_id, item.raw_timestamp)
                pass
            # If there's an allowlist and
            # If the user's country is not in the allowlist, we just update the timestamp
            elif db.get_allowlist() != 0 and (
                get_user_country(item.raw_data["user"]["id"])
            ) not in (db.get_allowlist() + ["XX"]):
                db.update_last_timestamp(query_id, item.raw_timestamp)
                pass
            # Check if the item title contains any banwords
            elif banwords_str and contains_banwords(item.title, banwords_str):
                # If it contains banwords, just update the timestamp and skip
                db.update_last_timestamp(query_id, item.raw_timestamp)
                pass
            else:
                # We create the message
                message_template = (
                    db.get_parameter("message_template") or db.DEFAULT_MESSAGE_TEMPLATE
                )
                description = (
                    _get_item_description(item)
                    if "{description}" in message_template
                    else getattr(item, "description", None)
                )
                format_values = dict(
                    title=_notification_value(item.title),
                    price=_notification_value(str(item.price) + " " + item.currency),
                    brand=_notification_value(
                        item.brand_title,
                        fallback="Not specified",
                    ),
                    condition=_notification_value(
                        getattr(item, "condition", None),
                        fallback="Not specified",
                        max_length=200,
                    ),
                    description=_notification_value(
                        description,
                        max_length=1800,
                    ),
                    image=(
                        ""
                        if item.photo is None
                        else html.escape(str(item.photo), quote=True)
                    ),
                )
                try:
                    content = message_template.format(**format_values)
                except (KeyError, IndexError, ValueError):
                    logger.error(
                        "Invalid notification template; using the safe default.",
                        exc_info=True,
                    )
                    content = db.DEFAULT_MESSAGE_TEMPLATE.format(**format_values)
                # Route this alert only to approved subscribers of the
                # matching query. One query may notify several accounts.
                subscriber_chat_ids = db.get_query_subscribers(query_id)

                # Persist the Telegram notification to the durable outbox BEFORE
                # the item is marked "seen" below, so a crash or restart between
                # finding and delivering it cannot lose the alert. The Telegram
                # bot drains the outbox and retries until delivered.
                if subscriber_chat_ids:
                    db.enqueue_notification(
                        content,
                        item.url,
                        "Open Vinted",
                        subscriber_chat_ids,
                        query_id=query_id,
                    )
                else:
                    logger.warning(
                        "No approved Telegram subscribers for query %s; "
                        "the item will still be available to RSS.",
                        query_id,
                    )

                # RSS-only installations still receive every item via the
                # ephemeral in-memory queue.
                new_items_queue.put(
                    (
                        content,
                        item.url,
                        "Open Vinted",
                        None,
                        None,
                        subscriber_chat_ids,
                    )
                )
                # Mark the item as seen only after it has been persisted above.
                db.add_item_to_db(
                    id=item.id,
                    timestamp=item.raw_timestamp,
                    price=item.price,
                    title=item.title,
                    photo_url=item.photo,
                    query_id=query_id,
                    currency=item.currency,
                )


def contains_banwords(title, banwords_str):
    """
    Check if a title contains any banwords.

    Args:
        title (str): The title to check
        banwords_str (str): List of banwords separated by 3 pipe character
    Returns:
        bool: True if the title contains any banwords, False otherwise
    """

    # Split the banwords string into a list using pipe as delimiter
    banwords = [
        word.strip().lower() for word in banwords_str.split("|||") if word.strip()
    ]

    # If the list is empty, return False
    if not banwords:
        return False

    # Check if any banword is in the title (case-insensitive)
    title_lower = title.lower()
    for word in banwords:
        if word in title_lower:
            return True

    return False


def check_version(force=False):
    """
    Check if the application is up to date
    """
    global _VERSION_CACHE, _VERSION_CACHE_TIME

    now = time.monotonic()
    with _VERSION_CACHE_LOCK:
        if (
            not force
            and _VERSION_CACHE is not None
            and now - _VERSION_CACHE_TIME < _VERSION_CACHE_TTL_SECONDS
        ):
            return _VERSION_CACHE

        github_url = (
            db.get_parameter("github_url")
            or "https://github.com/FrancesOkolo/Vinted-Notifications"
        )
        version = db.get_parameter("version") or "unknown"
        result = (True, version, version, github_url)

        try:
            response = requests.get(
                f"{github_url}/releases/latest",
                timeout=(3.05, 5),
            )
            # Only treat it as a real release when GitHub redirects to a tag
            # (…/releases/tag/<tag>). A repo with no published releases
            # redirects to …/releases, which must NOT be read as a version —
            # otherwise the UI shows a bogus "Update: releases" banner.
            if response.status_code == 200 and "/releases/tag/" in response.url:
                latest_version = response.url.rstrip("/").split("/")[-1]
                result = (
                    version == latest_version,
                    version,
                    latest_version,
                    github_url,
                )
        except requests.RequestException as error:
            logger.warning("Could not check for a new version: %s", error)

        _VERSION_CACHE = result
        _VERSION_CACHE_TIME = now
        return result
