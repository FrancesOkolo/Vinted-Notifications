import db
import random
import requests
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from email.utils import parsedate_to_datetime
from pyVintedVN import Vinted, requester
from urllib.parse import urlparse, parse_qs
from logger import get_logger
from url_normalizer import normalise_vinted_url

# Get logger for this module
logger = get_logger(__name__)


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
            ("Query removed.", True)
            if success
            else ("Failed to remove query.", False)
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
    timezone_name = str(
        db.get_parameter("quiet_hours_timezone") or "Europe/London"
    ).strip() or "Europe/London"

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
        quiet_timezone = ZoneInfo(timezone_name)

    start = _parse_quiet_time(start_text, "01:00")
    end = _parse_quiet_time(end_text, "06:00")

    if start == end:
        return False, start_text, end_text, timezone_name

    if now is None:
        current = datetime.now(quiet_timezone).time().replace(tzinfo=None)
    elif isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=quiet_timezone)
        current = now.astimezone(quiet_timezone).time().replace(tzinfo=None)
    else:
        # A time object is useful for unit tests and is interpreted directly
        # in the configured quiet-hours timezone.
        current = now.replace(tzinfo=None)

    if start < end:
        active = start <= current < end
    else:
        active = current >= start or current < end

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
        seconds = int(
            (retry_at - datetime.now(timezone.utc)).total_seconds()
        )
        return max(1, seconds)
    except (TypeError, ValueError, OverflowError):
        return fallback_seconds


def _get_query_spacing_seconds(query_count):
    if query_count <= 1:
        return 0.0

    try:
        refresh_delay = int(
            db.get_parameter("query_refresh_delay") or 600
        )
    except (TypeError, ValueError):
        refresh_delay = 600

    usable_window = max(60, refresh_delay * 0.80)
    calculated_spacing = usable_window / query_count
    return max(2.0, min(15.0, calculated_spacing))


def process_items(queue):
    """
    Scrape every unique query once, pacing requests across the configured
    interval. Shared subscriptions do not create duplicate Vinted requests.
    """
    quiet_active, quiet_start, quiet_end, quiet_timezone = (
        get_quiet_hours_status()
    )
    if quiet_active:
        logger.info(
            "Quiet hours active (%s-%s, %s); skipping this scrape cycle.",
            quiet_start,
            quiet_end,
            quiet_timezone,
        )
        return

    all_queries = db.get_queries()

    if not all_queries:
        logger.info("No Vinted queries configured.")
        return

    vinted = Vinted()

    try:
        items_per_query = int(
            db.get_parameter("items_per_query") or 20
        )
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
                break
            except requests.exceptions.HTTPError as error:
                response = error.response
                status_code = (
                    response.status_code if response is not None else None
                )

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
                fallback = 60 * (attempt + 1)
                wait_seconds = _get_retry_after_seconds(
                    response,
                    fallback_seconds=fallback,
                )
                wait_seconds = min(max(wait_seconds, 30), 300)

                logger.warning(
                    "Vinted rate-limited query %s/%s. Waiting %s seconds "
                    "before %s.",
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

        if all_items is not None:
            data = [item for item in all_items if item.is_new_item()]
            queue.put((data, query_id))
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

def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    if not items_queue.empty():
        data, query_id = items_queue.get()
        banwords_str = db.get_parameter("banwords")
        for item in reversed(data):

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
                message_template = db.get_parameter("message_template")
                content = message_template.format(
                    title=item.title,
                    price=str(item.price) + " " + item.currency,
                    brand=item.brand_title,
                    image=None if item.photo is None else item.photo,
                )
                # Route this alert only to approved subscribers of the
                # matching query. One query may notify several accounts.
                subscriber_chat_ids = db.get_query_subscribers(query_id)

                if subscriber_chat_ids:
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
                else:
                    logger.warning(
                        "No approved Telegram subscribers for query %s; "
                        "the item will be stored but not sent.",
                        query_id,
                    )
                # Add the item to the db
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


def check_version():
    """
    Check if the application is up to date
    """
    try:
        # Get URL from the database
        github_url = db.get_parameter("github_url")
        # Get version from the database
        ver = db.get_parameter("version")
        # Get latest version from the repository
        url = f"{github_url}/releases/latest"
        response = requests.get(url)

        if response.status_code == 200:
            latest_version = response.url.split("/")[-1]
            is_up_to_date = ver == latest_version
            return is_up_to_date, ver, latest_version, github_url
        else:
            # If we can't check, assume it's up to date
            return True, ver, ver, github_url
    except Exception as e:
        logger.error(f"Error checking for new version: {str(e)}", exc_info=True)
        # If we can't check, assume it's up to date
        return True, ver, ver, github_url
