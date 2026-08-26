import db
import deal_evaluator
import html
import json
import query_observability
import random
import requests
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from queue import Empty
from types import SimpleNamespace
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
_USER_COUNTRY_CACHE = {}
_USER_COUNTRY_CACHE_LOCK = threading.Lock()
_USER_COUNTRY_CACHE_MAX_ENTRIES = 5000
_SCRAPER_FORBIDDEN_LIMIT = 1
_SCRAPER_COOLDOWN_SECONDS = (5 * 60, 10 * 60, 15 * 60)
_SCRAPER_FORBIDDEN_COOLDOWN_SECONDS = (
    5 * 60,
    30 * 60,
    2 * 60 * 60,
    8 * 60 * 60,
)
_SCRAPER_RATE_LIMIT_MIN_COOLDOWN_SECONDS = 30
_SCRAPER_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 5 * 60
_SCRAPER_MIN_QUERY_SPACING_SECONDS = 1.0
_SCRAPER_MAX_QUERY_SPACING_SECONDS = 5.0
_SCRAPER_TARGET_ACTIVE_FRACTION = 0.50
_SCRAPER_LOCAL_COOLDOWN_LOCK = threading.Lock()
_SCRAPER_LOCAL_COOLDOWN = {
    "until": 0,
    "level": 0,
    "status_code": None,
    "skip_logged_until": 0,
}
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
    if success is None:
        return "Could not update your subscription. Please try again.", False
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
    """Get one seller country with one paced request and no auth retry."""
    if not profile_id or get_scraper_cooldown()["active"]:
        return "XX"

    # Users are shared between all Vinted platforms, so we can use whatever locale we want.
    url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
    response = None
    try:
        response = requester.get_once(
            url,
            cancel_if=lambda: get_scraper_cooldown()["active"],
        )
    except requests.exceptions.RequestException:
        logger.warning(
            "Could not reach Vinted while determining the user country; "
            "using the unknown-country fallback."
        )
        return "XX"

    # A cooldown may open while this call waits for the shared request slot.
    # Cancellation returns None without performing the HTTP request.
    if response is None:
        return "XX"

    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        if status_code == 429:
            _activate_scraper_cooldown(
                status_code,
                duration_seconds=_get_bounded_retry_after_seconds(response),
            )
        elif status_code in (401, 403):
            _activate_scraper_cooldown(status_code)
        logger.warning(
            "Could not determine the Vinted user country after HTTP %s; "
            "using the unknown-country fallback.",
            status_code,
        )
        close = getattr(response, "close", None)
        if callable(close):
            close()
        return "XX"

    try:
        country = _normalise_country_code(response.json()["user"]["country_iso_code"])
        return country or "XX"
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "Vinted returned an invalid user-country response; using the "
            "unknown-country fallback."
        )
        return "XX"
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _normalise_country_code(value):
    text = str(value or "").strip().upper()
    return text if len(text) == 2 and text.isalpha() else None


def _embedded_item_country(item):
    """Return a seller country already embedded in the catalogue item."""
    raw_data = getattr(item, "raw_data", {})
    if not isinstance(raw_data, dict):
        return None

    user = raw_data.get("user")
    user = user if isinstance(user, dict) else {}
    country = user.get("country")
    country = country if isinstance(country, dict) else {}
    for value in (
        user.get("country_iso_code"),
        user.get("country_code"),
        country.get("iso_code"),
        country.get("code"),
        raw_data.get("country_iso_code"),
    ):
        normalised = _normalise_country_code(value)
        if normalised:
            return normalised
    return None


def _resolve_item_country(item):
    """Prefer catalogue metadata, then one cached and paced profile lookup."""
    embedded = _embedded_item_country(item)
    if embedded:
        return embedded

    raw_data = getattr(item, "raw_data", {})
    user = raw_data.get("user") if isinstance(raw_data, dict) else None
    profile_id = user.get("id") if isinstance(user, dict) else None
    if profile_id in (None, ""):
        return "XX"

    cache_key = str(profile_id)
    with _USER_COUNTRY_CACHE_LOCK:
        cached = _USER_COUNTRY_CACHE.get(cache_key)
    if cached:
        return cached

    country = get_user_country(profile_id)
    # Preserve the historical fail-open "XX" allowlist semantics, but do not
    # cache unknown: later catalogue data or a recovered request may resolve it.
    if country != "XX":
        with _USER_COUNTRY_CACHE_LOCK:
            if len(_USER_COUNTRY_CACHE) >= _USER_COUNTRY_CACHE_MAX_ENTRIES:
                oldest_key = next(iter(_USER_COUNTRY_CACHE), None)
                if oldest_key is not None:
                    _USER_COUNTRY_CACHE.pop(oldest_key, None)
            _USER_COUNTRY_CACHE[cache_key] = country
    return country


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

    # Restrict quiet hours to the configured *start* days when the date is
    # known. For a window crossing midnight, the after-midnight portion belongs
    # to the previous day's schedule (Friday 23:00-06:00 therefore remains
    # quiet until Saturday 06:00).
    if active and weekday is not None:
        schedule_weekday = weekday
        if start > end and current < end:
            schedule_weekday = (weekday - 1) % 7
        if schedule_weekday not in _parse_quiet_days(
            db.get_parameter("quiet_hours_days")
        ):
            active = False

    return active, start_text, end_text, timezone_name


def _quiet_hours_active():
    return get_quiet_hours_status()[0]


def _get_retry_after_seconds(response, fallback_seconds):
    if response is None:
        return fallback_seconds

    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
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


def _get_bounded_retry_after_seconds(response):
    """Return a safe global cooldown for one confirmed HTTP 429 response."""
    seconds = _get_retry_after_seconds(response, fallback_seconds=60)
    return min(
        max(seconds, _SCRAPER_RATE_LIMIT_MIN_COOLDOWN_SECONDS),
        _SCRAPER_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
    )


def _get_query_spacing_seconds(query_count, refresh_delay=None):
    if query_count <= 1:
        return 0.0

    if refresh_delay is None:
        try:
            refresh_delay = int(db.get_parameter("query_refresh_delay") or 600)
        except (TypeError, ValueError):
            refresh_delay = 600
    else:
        try:
            refresh_delay = int(refresh_delay)
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

    # Persistence is the source of truth across restarts, but a transient
    # SQLite write failure must not let the very next queued scheduler job hit
    # Vinted again. Merge in the scraper process's immediate fallback state.
    with _SCRAPER_LOCAL_COOLDOWN_LOCK:
        local = dict(_SCRAPER_LOCAL_COOLDOWN)
    if local["until"] > until:
        status_code = local["status_code"]
    until = max(until, local["until"])
    level = max(level, local["level"])
    if status_code is None and local["status_code"] is not None:
        status_code = local["status_code"]

    remaining = max(0, until - now)
    return {
        "active": remaining > 0,
        "until": until,
        "remaining": remaining,
        "level": level,
        "status_code": status_code,
    }


def _activate_scraper_cooldown(status_code, now=None, duration_seconds=None):
    """Open the persistent circuit breaker, optionally for an exact duration."""
    now = int(now if now is not None else time.time())
    cooldown_seconds = (
        _SCRAPER_FORBIDDEN_COOLDOWN_SECONDS
        if status_code == 403
        else _SCRAPER_COOLDOWN_SECONDS
    )
    current_level = min(
        get_scraper_cooldown(now=now)["level"],
        len(cooldown_seconds),
    )
    escalated_duration = cooldown_seconds[min(current_level, len(cooldown_seconds) - 1)]
    duration = (
        escalated_duration
        if duration_seconds is None
        else max(1, int(duration_seconds))
    )
    new_level = min(current_level + 1, len(cooldown_seconds))
    until = now + duration

    with _SCRAPER_LOCAL_COOLDOWN_LOCK:
        _SCRAPER_LOCAL_COOLDOWN.update(
            {
                "until": until,
                "level": new_level,
                "status_code": status_code,
                "skip_logged_until": 0,
            }
        )

    try:
        persisted = db.set_parameters(
            {
                "scraper_cooldown_until": str(until),
                "scraper_cooldown_level": str(new_level),
                "scraper_last_block_status": str(status_code),
                "scraper_consecutive_403s": "0",
            }
        )
    except Exception:
        persisted = False
    if not persisted:
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
        try:
            persisted = db.set_parameters(
                {
                    "scraper_cooldown_until": "0",
                    "scraper_cooldown_level": "0",
                    "scraper_last_block_status": "",
                    "scraper_consecutive_403s": "0",
                }
            )
        except Exception:
            persisted = False
        if not persisted:
            logger.warning("Could not clear the scraper cooldown state.")
    with _SCRAPER_LOCAL_COOLDOWN_LOCK:
        _SCRAPER_LOCAL_COOLDOWN.update(
            {
                "until": 0,
                "level": 0,
                "status_code": None,
                "skip_logged_until": 0,
            }
        )


def _log_scraper_cooldown_skip(cooldown):
    """Warn once per opened cooldown; queued jobs only emit debug noise."""
    with _SCRAPER_LOCAL_COOLDOWN_LOCK:
        already_logged = (
            _SCRAPER_LOCAL_COOLDOWN["skip_logged_until"] == cooldown["until"]
        )
        _SCRAPER_LOCAL_COOLDOWN["skip_logged_until"] = cooldown["until"]

    log = logger.debug if already_logged else logger.warning
    log(
        "Scraper circuit breaker is open after HTTP %s; skipping this "
        "cycle with approximately %s minute(s) remaining.",
        cooldown["status_code"] or "403",
        max(1, (cooldown["remaining"] + 59) // 60),
    )


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
    count_failed_cycle=True,
):
    """Update health counters after a complete cycle or scheduled query run.

    Serialized per-query scheduling calls :func:`process_items` once per due
    query. Such a call may update successful health, but one failed query must
    not masquerade as an entire failed sweep of every active query.
    """
    now = int(time.time())
    try:
        db.set_parameter("scraper_last_cycle", str(now))
        if blocked_status is not None and count_failed_cycle:
            failed = _parameter_int("scraper_failed_cycles")
            db.set_parameter("scraper_failed_cycles", str(failed + 1))
        elif successful_fetches > 0:
            db.set_parameter("scraper_last_ok", str(now))
            db.set_parameter("scraper_failed_cycles", "0")
            _clear_scraper_cooldown()
        elif query_count > 0 and count_failed_cycle:
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


def process_items(
    queue,
    query_ids=None,
    monitor_during_quiet_hours=False,
):
    """
    Scrape enabled queries once, pacing multi-query calls across the configured
    interval. Shared subscriptions do not create duplicate Vinted requests.

    ``query_ids`` is used by the per-query scheduler to run exactly one due
    search while preserving the original all-query entry point for callers and
    tests. ``monitor_during_quiet_hours`` is deliberately explicit: ordinary
    searches remain silent overnight, while an opted-in priority search can
    still be scraped and delivered immediately.
    """

    quiet_active, quiet_start, quiet_end, quiet_timezone = get_quiet_hours_status()
    if quiet_active and not monitor_during_quiet_hours:
        logger.info(
            "Quiet hours active (%s-%s, %s); skipping this scrape cycle.",
            quiet_start,
            quiet_end,
            quiet_timezone,
        )
        return

    cooldown = get_scraper_cooldown()
    if cooldown["active"]:
        _log_scraper_cooldown_skip(cooldown)
        return
    recovery_probe = cooldown["level"] > 0

    # Only scrape enabled queries; paused ones stay in the database but make
    # no requests.
    try:
        all_queries = db.get_queries(enabled_only=True, raise_errors=True)
    except Exception:
        logger.error(
            "Could not read active queries; skipping this dispatch safely.",
            exc_info=True,
        )
        return
    record_scraper_heartbeat()

    if query_ids is not None:
        if isinstance(query_ids, (str, int)):
            query_ids = [query_ids]
        selected_ids = set()
        for query_id in query_ids:
            try:
                selected_ids.add(int(query_id))
            except (TypeError, ValueError):
                continue
        all_queries = [query for query in all_queries if query[0] in selected_ids]

    if not all_queries:
        if query_ids is None:
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

    successful_fetches = 0
    consecutive_403s = _parameter_int("scraper_consecutive_403s")
    cycle_block_status = None

    for position, query in enumerate(all_queries, start=1):
        if _quiet_hours_active() and not monitor_during_quiet_hours:
            logger.info(
                "Quiet hours began during the scrape. Stopping after %s/%s queries.",
                position - 1,
                query_count,
            )
            break

        query_id = query[0]
        query_url = query[1]
        all_items = None
        execution_id = None

        # The enabled-query list is a snapshot taken at the beginning of the
        # cycle. Honour a pause made while that cycle is already running.
        if not db.is_query_enabled(query_id):
            logger.info("Skipping query %s because it was paused mid-cycle.", query_id)
            continue

        request_started_at = time.time()
        request_started_monotonic = time.monotonic()
        try:
            execution_id = query_observability.start_execution(
                query_id,
                query_url,
                items_per_query,
                started_at=request_started_at,
            )
        except Exception:
            # Observability must never prevent the established scraper path
            # from running. Startup normally creates the schema before this
            # point; this fallback also keeps isolated legacy tests working.
            logger.error(
                "Could not start catalogue execution telemetry for query %s; "
                "using legacy discovery for this request.",
                query_id,
                exc_info=True,
            )

        try:
            # Requester owns the one bounded session-refresh retry. The scrape
            # scheduler must never sleep and retry a rate-limited query itself.
            all_items = vinted.items.search(
                query_url,
                nbr_items=items_per_query,
            )
            consecutive_403s = 0
        except requests.exceptions.HTTPError as error:
            response = error.response
            status_code = response.status_code if response is not None else None
            if execution_id is not None:
                try:
                    query_observability.record_failure(
                        execution_id,
                        f"http_{status_code or 'error'}",
                        http_status=status_code,
                        duration_ms=(time.monotonic() - request_started_monotonic)
                        * 1000,
                    )
                except Exception:
                    logger.error(
                        "Could not finish catalogue failure telemetry for " "query %s.",
                        query_id,
                        exc_info=True,
                    )

            if status_code == 401:
                cooldown = _activate_scraper_cooldown(401)
                cycle_block_status = 401
                logger.error(
                    "Scraper circuit breaker opened after a confirmed "
                    "HTTP 401 response. Stopping requests and cooling "
                    "down for %s minutes.",
                    max(1, (cooldown["remaining"] + 59) // 60),
                )
            elif status_code == 403:
                consecutive_403s += 1
                forbidden_limit = 1 if recovery_probe else _SCRAPER_FORBIDDEN_LIMIT
                if consecutive_403s >= forbidden_limit:
                    cooldown = _activate_scraper_cooldown(403)
                    cycle_block_status = 403
                    if recovery_probe:
                        logger.error(
                            "Scraper recovery probe received a confirmed HTTP 403; "
                            "reopening the circuit breaker at level %s for %s "
                            "minutes.",
                            cooldown["level"],
                            cooldown["remaining"] // 60,
                        )
                    else:
                        logger.error(
                            "Scraper circuit breaker opened after %s "
                            "consecutive HTTP 403 responses. Stopping the "
                            "cycle and cooling down for %s minutes.",
                            consecutive_403s,
                            cooldown["remaining"] // 60,
                        )
                else:
                    logger.warning(
                        "Vinted rejected query %s/%s with HTTP 403 "
                        "(%s/%s consecutive).",
                        position,
                        query_count,
                        consecutive_403s,
                        forbidden_limit,
                    )
            elif status_code == 429:
                consecutive_403s = 0
                wait_seconds = _get_bounded_retry_after_seconds(response)
                cooldown = _activate_scraper_cooldown(
                    429,
                    duration_seconds=wait_seconds,
                )
                cycle_block_status = 429
                logger.error(
                    "Scraper circuit breaker opened after a confirmed "
                    "HTTP 429 response. Stopping requests and cooling "
                    "down for %s seconds.",
                    cooldown["remaining"],
                )
            else:
                consecutive_403s = 0
                logger.error(
                    "HTTP error while scraping query %s/%s: %s",
                    position,
                    query_count,
                    query_url,
                    exc_info=True,
                )
        except requests.exceptions.RequestException:
            if execution_id is not None:
                try:
                    query_observability.record_failure(
                        execution_id,
                        "network_error",
                        duration_ms=(time.monotonic() - request_started_monotonic)
                        * 1000,
                    )
                except Exception:
                    logger.error(
                        "Could not finish catalogue failure telemetry for " "query %s.",
                        query_id,
                        exc_info=True,
                    )
            logger.error(
                "Network error while scraping query %s/%s: %s",
                position,
                query_count,
                query_url,
                exc_info=True,
            )
        except Exception:
            if execution_id is not None:
                try:
                    query_observability.record_failure(
                        execution_id,
                        "unexpected_error",
                        duration_ms=(time.monotonic() - request_started_monotonic)
                        * 1000,
                    )
                except Exception:
                    logger.error(
                        "Could not finish catalogue failure telemetry for " "query %s.",
                        query_id,
                        exc_info=True,
                    )
            logger.error(
                "Unexpected error while scraping query %s/%s: %s",
                position,
                query_count,
                query_url,
                exc_info=True,
            )

        if cycle_block_status is not None:
            break

        if all_items is not None:
            successful_fetches += 1
            if db.is_query_enabled(query_id):
                data = None
                if execution_id is not None:
                    try:
                        observation = query_observability.record_success(
                            execution_id,
                            query_id,
                            query_url,
                            [
                                query_observability.item_snapshot(item)
                                for item in all_items
                            ],
                            duration_ms=(time.monotonic() - request_started_monotonic)
                            * 1000,
                        )
                        candidate_ids = {
                            str(item_id) for item_id in observation.candidate_ids
                        }
                        data = [
                            item for item in all_items if str(item.id) in candidate_ids
                        ]
                    except Exception:
                        logger.error(
                            "Could not persist catalogue observations for "
                            "query %s; using legacy discovery for this response.",
                            query_id,
                            exc_info=True,
                        )
                        try:
                            query_observability.record_failure(
                                execution_id,
                                "observation_persistence_error",
                                http_status=200,
                                duration_ms=(
                                    time.monotonic() - request_started_monotonic
                                )
                                * 1000,
                            )
                        except Exception:
                            logger.error(
                                "Could not close failed catalogue telemetry for "
                                "query %s.",
                                query_id,
                                exc_info=True,
                            )
                if data is not None:
                    try:
                        # Pending snapshots were committed with the successful
                        # progress marker. If this handoff is lost, the queue
                        # consumer recovers the same batch from SQLite.
                        queue.put((data, query_id, execution_id))
                    except Exception:
                        logger.error(
                            "Could not hand query %s to the in-memory queue; "
                            "durable recovery will process it.",
                            query_id,
                            exc_info=True,
                        )
                if data is None:
                    data = [item for item in all_items if item.is_new_item()]
                    queue.put((data, query_id))
            else:
                data = []
                if execution_id is not None:
                    try:
                        query_observability.record_failure(
                            execution_id,
                            "discarded_paused",
                            http_status=200,
                            duration_ms=(time.monotonic() - request_started_monotonic)
                            * 1000,
                        )
                    except Exception:
                        logger.error(
                            "Could not finish paused-query telemetry for query %s.",
                            query_id,
                            exc_info=True,
                        )
                logger.info(
                    "Discarding results for query %s because it was paused "
                    "while the request was in progress.",
                    query_id,
                )
            logger.info(
                "Discovered %s candidate(s) from %s returned item(s) for "
                "query %s/%s: %s",
                len(data),
                len(all_items),
                position,
                query_count,
                query_url,
            )

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
        count_failed_cycle=query_ids is None,
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
    # Per-item Vinted pages are nonessential, high-risk traffic. Catalogue data
    # is the only supported description source in the notification pipeline.
    return None


def _notification_value(value, fallback="Not provided", max_length=None):
    text = str(value).strip() if value is not None else ""
    if not text:
        text = fallback

    if max_length and len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"

    return html.escape(text, quote=False)


def _ai_deal_query_ids():
    """Query IDs configured to use AI deal evaluation (CSV parameter)."""
    raw = db.get_parameter("deal_ai_query_ids") or ""
    ids = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _ai_evaluation_snapshot(item):
    """Return primitives safe to persist with a durable AI-evaluation job."""
    price = getattr(item, "price", None)
    return {
        "title": str(getattr(item, "title", "") or ""),
        "brand": str(getattr(item, "brand_title", "") or ""),
        "condition": str(getattr(item, "condition", "") or ""),
        "price": "" if price is None else str(price),
        "currency": str(getattr(item, "currency", "") or ""),
        "photo_url": (str(getattr(item, "photo", "") or "") or None),
        "item_url": str(getattr(item, "url", "") or "") or None,
    }


def _pending_snapshot_item(snapshot):
    """Rebuild the small item interface used by local filtering/delivery.

    Pending snapshots intentionally omit seller data and descriptions. The
    catalogue-provided country code is retained only when already present, so
    restart recovery never creates an additional Vinted request.
    """
    country_code = _normalise_country_code(snapshot.get("country_code"))
    raw_data = {"country_iso_code": country_code} if country_code else {}
    return SimpleNamespace(
        id=snapshot.get("item_id"),
        title=snapshot.get("title") or "",
        brand_title=snapshot.get("brand") or "",
        condition=snapshot.get("condition") or "",
        description=None,
        size_title=None,
        currency=snapshot.get("currency") or "",
        price=snapshot.get("price") or "",
        photo=snapshot.get("photo_url"),
        url=snapshot.get("item_url"),
        raw_timestamp=snapshot.get("listed_at"),
        raw_data=raw_data,
    )


def _classify_pending_safely(
    execution_id,
    query_id,
    item_id,
    disposition,
    **kwargs,
):
    try:
        return query_observability.classify_pending(
            execution_id,
            query_id,
            item_id,
            disposition,
            **kwargs,
        )
    except Exception:
        logger.error(
            "Could not classify pending catalogue item %s as %s.",
            item_id,
            disposition,
            exc_info=True,
        )
        return False


def _release_pending_items(execution_id, query_id, items, retry_delay=5):
    if execution_id is None:
        return
    for item in items:
        _classify_pending_safely(
            execution_id,
            query_id,
            item.id,
            "retry",
            retry_delay=retry_delay,
        )


def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    batch = None
    try:
        batch = items_queue.get_nowait()
    except Empty:
        pass
    except AttributeError:
        # Compatibility with simple queue fakes and legacy direct callers.
        if not items_queue.empty():
            batch = items_queue.get()
    if batch is None:
        try:
            durable_batch = query_observability.pending_batch()
        except Exception:
            # Startup creates this schema. Keep legacy/unit-test databases
            # compatible without making queue processing fail closed.
            logger.debug("Durable pending catalogue queue unavailable.", exc_info=True)
            durable_batch = None
        if durable_batch is not None:
            execution_id, query_id, snapshots = durable_batch
            batch = (
                [_pending_snapshot_item(snapshot) for snapshot in snapshots],
                query_id,
                execution_id,
            )

    if batch is not None:
        if len(batch) == 3:
            data, query_id, execution_id = batch
        else:
            data, query_id = batch
            execution_id = None
        if not db.is_query_enabled(query_id):
            _release_pending_items(execution_id, query_id, data)
            logger.info(
                "Discarding queued results for paused query %s.",
                query_id,
            )
            return
        banwords_str = db.get_parameter("banwords")
        allowlist = db.get_allowlist()
        for item in reversed(data):

            if not db.is_query_enabled(query_id):
                logger.info(
                    "Stopping queued item processing because query %s was paused.",
                    query_id,
                )
                _release_pending_items(execution_id, query_id, data)
                break

            # Legacy queue entries retain their historical timestamp guard.
            # Durable executions use per-query item IDs and progress anchors,
            # so correctness no longer depends on a 20-minute age window.
            last_query_timestamp = db.get_last_timestamp(query_id)
            if (
                execution_id is None
                and last_query_timestamp is not None
                and last_query_timestamp >= item.raw_timestamp
            ):
                pass
            # In case of multiple queries, we need to check if the item is already in the db
            elif db.is_item_in_db_by_id(item.id) is True:
                if execution_id is not None and not _classify_pending_safely(
                    execution_id,
                    query_id,
                    item.id,
                    "already_known",
                ):
                    logger.info(
                        "Stopping stale or unavailable durable batch for query %s.",
                        query_id,
                    )
                    break
                db.update_last_timestamp(query_id, item.raw_timestamp)
                pass
            # Reject local title exclusions before any optional seller lookup.
            elif banwords_str and contains_banwords(item.title, banwords_str):
                if execution_id is not None and not _classify_pending_safely(
                    execution_id,
                    query_id,
                    item.id,
                    "locally_rejected",
                ):
                    logger.info(
                        "Stopping stale or unavailable durable batch for query %s.",
                        query_id,
                    )
                    break
                db.update_last_timestamp(query_id, item.raw_timestamp)
                pass
            # If there's an allowlist and
            # If the user's country is not in the allowlist, we just update the timestamp
            elif allowlist != 0 and _resolve_item_country(item) not in (
                allowlist + ["XX"]
            ):
                if execution_id is not None and not _classify_pending_safely(
                    execution_id,
                    query_id,
                    item.id,
                    "locally_rejected",
                ):
                    logger.info(
                        "Stopping stale or unavailable durable batch for query %s.",
                        query_id,
                    )
                    break
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
                # Deal rating. AI-enabled queries persist a durable evaluation
                # snapshot in the same transaction as the item and its primary
                # alert. A separate serialized worker sends the result later,
                # so an API call can never delay the primary notification.
                # Other queries get the instant listing-price ceiling rating.
                is_ai_query = query_id in _ai_deal_query_ids()
                if not is_ai_query:
                    content, _deal_rating = deal_evaluator.prepend_deal_rating(
                        content,
                        item.price,
                        item.currency,
                        db.get_query_preferences(query_id),
                    )
                # Subscriber lookup, durable Telegram outbox insertion, item
                # insertion, and last-item advancement are one SQLite
                # transaction. If any step fails, none of them is committed and
                # the item remains eligible for discovery on the next cycle.
                persistence = db.persist_item_and_notification(
                    id=item.id,
                    timestamp=item.raw_timestamp,
                    price=item.price,
                    title=item.title,
                    photo_url=item.photo,
                    query_id=query_id,
                    currency=item.currency,
                    content=content,
                    notification_url=item.url,
                    button_text="Open Vinted",
                    ai_evaluation=(
                        _ai_evaluation_snapshot(item) if is_ai_query else None
                    ),
                    execution_id=execution_id,
                )
                if persistence is None:
                    _release_pending_items(
                        execution_id,
                        query_id,
                        [item],
                        retry_delay=5,
                    )
                    logger.error(
                        "Could not persist item %s and its notification; "
                        "stopping this query batch so it can be retried.",
                        item.id,
                    )
                    break

                persisted, subscriber_chat_ids = persistence
                if not persisted:
                    _release_pending_items(
                        execution_id,
                        query_id,
                        [item],
                        retry_delay=5,
                    )
                    logger.info(
                        "Stopping queued item processing because query %s was "
                        "paused or removed before item %s could be persisted.",
                        query_id,
                        item.id,
                    )
                    break

                if not subscriber_chat_ids:
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

    # Check if any banword matches the title (case-insensitive). A banword
    # containing '+' (e.g. "empty+box") is an AND-rule: it matches only when
    # every '+'-separated term appears somewhere in the title, in any order.
    # That catches split phrases like "Empty ... Box" without banning "empty"
    # alone (which would wrongly hit a vacuum's "self-empty station").
    title_lower = title.lower()
    for word in banwords:
        if "+" in word:
            terms = [term.strip() for term in word.split("+") if term.strip()]
            if terms and all(term in title_lower for term in terms):
                return True
        elif word in title_lower:
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
