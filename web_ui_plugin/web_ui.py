from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
import db
import core
import query_efficiency
import query_observability
import scraper_rate
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import requests
import secrets
import string
import threading
import time
from collections import deque
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
from logger import get_logger, redact_secrets
from url_normalizer import normalise_vinted_url

# Windows can map .js files to text/plain through its system MIME registry.
# Define the web-safe types explicitly so nosniff browsers execute local assets
# consistently on Windows development machines and Linux/Docker deployments.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")

# Get logger for this module
logger = get_logger(__name__)

_MAX_ACTIVE_FAST_QUERIES = db.MAX_ACTIVE_FAST_QUERIES
_QUERY_PREFERENCE_DEFAULTS = {
    "poll_mode": "normal",
    "monitor_during_quiet_hours": False,
    "deal_evaluator_enabled": False,
    "deal_excellent_max": None,
    "deal_good_max": None,
    "deal_currency": "GBP",
}

_SCRAPER_PAUSE_PRESETS = {
    "phone_blocked": {
        "duration_seconds": None,
        "reason": "phone_blocked",
        "message": (
            "Scraping paused until you resume it. Telegram, the dashboard, and "
            "queued alert delivery remain available."
        ),
    },
    "1h": {
        "duration_seconds": 60 * 60,
        "reason": "manual_1h",
        "message": "Scraping paused for one hour.",
    },
    "6h": {
        "duration_seconds": 6 * 60 * 60,
        "reason": "manual_6h",
        "message": "Scraping paused for six hours.",
    },
    "24h": {
        "duration_seconds": 24 * 60 * 60,
        "reason": "manual_24h",
        "message": "Scraping paused for 24 hours.",
    },
}

# Create Flask app
app = Flask(
    __name__,
    template_folder=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    ),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)


def _env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _is_loopback_host(host):
    """Return True only for explicit loopback bind addresses."""
    candidate = (host or "").strip().lower().strip("[]")
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


WEB_HTTPS_ENABLED = _env_flag("VN_WEB_HTTPS")
WEB_SECRET_KEY = os.environ.get("VN_SECRET_KEY", "")
AUTH_MAX_FAILURES = _positive_int_env("VN_WEB_AUTH_MAX_FAILURES", 10)
AUTH_WINDOW_SECONDS = _positive_int_env("VN_WEB_AUTH_WINDOW_SECONDS", 300)
AUTH_BLOCK_SECONDS = _positive_int_env("VN_WEB_AUTH_BLOCK_SECONDS", 900)
app.config.update(
    MAX_CONTENT_LENGTH=_positive_int_env("VN_WEB_MAX_REQUEST_BYTES", 256 * 1024),
    MAX_FORM_MEMORY_SIZE=_positive_int_env(
        "VN_WEB_MAX_FORM_MEMORY_BYTES",
        256 * 1024,
    ),
    MAX_FORM_PARTS=_positive_int_env("VN_WEB_MAX_FORM_PARTS", 200),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=WEB_HTTPS_ENABLED,
    SESSION_COOKIE_NAME="vinted_notifications_session",
)

trusted_hosts = [
    host.strip()
    for host in os.environ.get("VN_WEB_TRUSTED_HOSTS", "").split(",")
    if host.strip()
]
if trusted_hosts:
    app.config["TRUSTED_HOSTS"] = trusted_hosts

# A persistent key is mandatory in Docker. Localhost-only development can use
# an ephemeral key without placing a secret in source control.
app.secret_key = WEB_SECRET_KEY or os.urandom(32)

WEB_USERNAME = os.environ.get("VN_WEB_USERNAME", "").strip()
WEB_PASSWORD = os.environ.get("VN_WEB_PASSWORD", "")
if bool(WEB_USERNAME) != bool(WEB_PASSWORD):
    raise RuntimeError(
        "VN_WEB_USERNAME and VN_WEB_PASSWORD must either both be set or both be empty."
    )

_auth_failures = {}
_auth_blocked_until = {}
_auth_lock = threading.Lock()


def _validate_web_bind_security(host):
    """Refuse an unauthenticated or ephemeral-key network-facing Web UI."""
    if _is_loopback_host(host):
        return
    if not WEB_USERNAME:
        raise RuntimeError(
            "Refusing to expose the Web UI without authentication. Set "
            "VN_WEB_USERNAME and VN_WEB_PASSWORD, or bind VN_WEB_HOST to "
            "127.0.0.1 for local-only access."
        )
    if not WEB_SECRET_KEY:
        raise RuntimeError(
            "Refusing to expose the Web UI without a persistent VN_SECRET_KEY."
        )


def _auth_client_key():
    return request.remote_addr or "unknown"


def _clear_auth_failures(client_key):
    with _auth_lock:
        _auth_failures.pop(client_key, None)
        _auth_blocked_until.pop(client_key, None)


def _record_auth_failure(client_key, now=None):
    """Record a failed login and return the block duration, if activated."""
    now = time.monotonic() if now is None else now
    cutoff = now - AUTH_WINDOW_SECONDS
    with _auth_lock:
        blocked_until = _auth_blocked_until.get(client_key, 0)
        if blocked_until > now:
            return max(1, int(blocked_until - now))

        failures = _auth_failures.setdefault(client_key, deque())
        while failures and failures[0] < cutoff:
            failures.popleft()
        failures.append(now)
        if len(failures) >= AUTH_MAX_FAILURES:
            blocked_until = now + AUTH_BLOCK_SECONDS
            _auth_blocked_until[client_key] = blocked_until
            failures.clear()
            return AUTH_BLOCK_SECONDS
    return 0


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _basic_auth_valid():
    if not WEB_USERNAME:
        return True
    auth = request.authorization
    return bool(
        auth
        and hmac.compare_digest(auth.username or "", WEB_USERNAME)
        and hmac.compare_digest(auth.password or "", WEB_PASSWORD)
    )


@app.before_request
def protect_web_interface():
    # A fresh nonce lets the templates run their own inline scripts without
    # allowing arbitrary injected scripts under the Content Security Policy.
    g.csp_nonce = secrets.token_urlsafe(18)

    if request.path == "/healthz":
        return None

    if not _basic_auth_valid():
        retry_after = _record_auth_failure(_auth_client_key())
        if retry_after:
            return Response(
                "Too many failed authentication attempts. Try again later.",
                429,
                {"Retry-After": str(retry_after)},
            )
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Vinted Notifications"'},
        )

    if WEB_USERNAME:
        _clear_auth_failures(_auth_client_key())

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.form.get("_csrf_token") or request.headers.get(
            "X-CSRF-Token"
        )
        expected = session.get("_csrf_token")
        if (
            not supplied
            or not expected
            or not hmac.compare_digest(
                supplied,
                expected,
            )
        ):
            abort(400, description="Invalid or missing CSRF token.")

    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    nonce = getattr(g, "csp_nonce", "")
    response.headers.setdefault(
        "Content-Security-Policy",
        "; ".join(
            (
                "default-src 'self'",
                f"script-src 'self' 'nonce-{nonce}'",
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' https: data:",
                "font-src 'self' data:",
                "connect-src 'self'",
                "object-src 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
            )
        ),
    )
    if WEB_HTTPS_ENABLED:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.context_processor
def inject_version_info():
    is_up_to_date, current_ver, latest_version, github_url = core.check_version()
    return {
        "github_url": github_url,
        "current_version": current_ver,
        "latest_version": latest_version,
        "is_up_to_date": is_up_to_date,
    }


@app.context_processor
def inject_current_year():
    return {
        "current_year": datetime.now().year,
        "csrf_token": csrf_token,
        "csp_nonce": getattr(g, "csp_nonce", ""),
    }


@app.route("/healthz")
def healthz():
    conn = None
    try:
        conn = db.get_db_connection()
        conn.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok"})
    except Exception as error:
        logger.error("Health check failed: %s", error)
        return jsonify({"status": "error"}), 503
    finally:
        if conn:
            conn.close()


def _query_last_found_sort_key(query):
    """Sort newest successful match first, with Never rows at the end."""
    last_item = query[2]

    if last_item is None:
        return (1, 0.0, int(query[0]))

    try:
        timestamp = float(last_item)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid last_item timestamp %r for query %s; treating it as Never.",
            last_item,
            query[0],
        )
        return (1, 0.0, int(query[0]))

    return (0, -timestamp, int(query[0]))


def _queries_newest_first(queries):
    """Return query rows ordered by Last Found Item, descending."""
    return sorted(queries, key=_query_last_found_sort_key)


def _format_dashboard_timestamp(value):
    """Return display fields for one local epoch timestamp."""
    try:
        timestamp = float(value)
        moment = datetime.fromtimestamp(timestamp)
        return {
            "text": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "iso": moment.astimezone().isoformat(),
            "raw": timestamp,
        }
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _format_dashboard_queries():
    """Return saved queries ordered by the time this app last raised an alert."""
    discoveries = db.get_query_last_discovery_map()
    queries = list(db.get_queries())
    queries.sort(
        key=lambda query: (
            0 if discoveries.get(int(query[0])) is not None else 1,
            -float(discoveries.get(int(query[0])) or 0),
            int(query[0]),
        )
    )

    formatted = []
    for position, query in enumerate(queries, start=1):
        parsed_query = urlparse(query[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )
        discovery = _format_dashboard_timestamp(discoveries.get(int(query[0])))
        formatted.append(
            {
                "id": position,
                "query_id": query[0],
                "query": query[1],
                "display": query_name if query_name else query[1],
                # Keep these established template keys while changing their
                # semantics from marketplace publication time to local alert time.
                "last_found_item": discovery["text"] if discovery else "Never",
                "last_found_timestamp": discovery["raw"] if discovery else None,
            }
        )
    return formatted


def _format_dashboard_items():
    """Return the six items most recently discovered by this application."""
    formatted = []
    for item in db.get_items(limit=6, sort="discovered_desc"):
        listed = _format_dashboard_timestamp(item[4])
        discovered = _format_dashboard_timestamp(item[8])
        if listed is None or discovered is None:
            logger.warning(
                "Could not format dashboard timestamps for item %s.",
                item[0],
            )
            continue
        parsed_query = urlparse(item[5])
        formatted.append(
            {
                "title": item[1],
                "price": item[2],
                "currency": item[3],
                "timestamp": discovered["text"],
                "timestamp_iso": discovered["iso"],
                "timestamp_raw": discovered["raw"],
                "listed_timestamp": listed["text"],
                "listed_timestamp_iso": listed["iso"],
                "listed_timestamp_raw": listed["raw"],
                "query": item[5],
                "photo_url": item[6],
                "url": (
                    f"{parsed_query.scheme}://{parsed_query.netloc}/items/{item[0]}"
                ),
            }
        )
    return formatted


def _dashboard_stats(items=None):
    """Return the dashboard counters and most recently alerted item."""
    enabled_map = db.get_query_enabled_map()
    recent_items = _format_dashboard_items() if items is None else items
    return {
        "total_items": db.get_total_items_count(),
        "total_queries": len(enabled_map),
        "active_queries": sum(1 for enabled in enabled_map.values() if enabled),
        "paused_queries": sum(1 for enabled in enabled_map.values() if not enabled),
        "items_per_day": db.get_items_per_day(),
        "last_item": recent_items[0] if recent_items else None,
    }


@app.route("/")
def index():
    # Get parameters
    params = db.get_all_parameters()
    formatted_queries = _format_dashboard_queries()
    formatted_items = _format_dashboard_items()

    # Get process status from the database
    telegram_running = db.get_parameter("telegram_process_running") == "True"
    rss_running = db.get_parameter("rss_process_running") == "True"
    stats = _dashboard_stats(formatted_items)

    return render_template(
        "index.html",
        params=params,
        queries=formatted_queries,
        items=formatted_items,
        telegram_running=telegram_running,
        rss_running=rss_running,
        stats=stats,
    )


@app.route("/api/dashboard/feed")
def dashboard_feed():
    """Return escaped dashboard fragments for non-disruptive live refresh."""
    formatted_queries = _format_dashboard_queries()
    formatted_items = _format_dashboard_items()
    stats = _dashboard_stats(formatted_items)
    return _no_store_json(
        {
            "recent_item_cards_html": render_template(
                "_dashboard_recent_item_cards.html",
                items=formatted_items,
            ),
            "recent_item_rows_html": render_template(
                "_dashboard_recent_item_rows.html",
                items=formatted_items,
            ),
            "query_rows_html": render_template(
                "_dashboard_query_rows.html",
                queries=formatted_queries,
            ),
            "last_item_html": render_template(
                "_dashboard_last_item.html",
                item=stats["last_item"],
            ),
            "total_items": stats["total_items"],
            "active_queries": stats["active_queries"],
            "paused_queries": stats["paused_queries"],
            "items_per_day": stats["items_per_day"],
            "query_count": len(formatted_queries),
        }
    )


_CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}

_VINTED_COUNTRY = {
    "co.uk": "UK",
    "fr": "FR",
    "de": "DE",
    "com": "COM",
    "it": "IT",
    "es": "ES",
    "nl": "NL",
    "be": "BE",
    "pl": "PL",
    "at": "AT",
    "lt": "LT",
    "cz": "CZ",
    "pt": "PT",
    "lu": "LU",
    "sk": "SK",
    "ie": "IE",
    "se": "SE",
    "ro": "RO",
    "hu": "HU",
    "fi": "FI",
    "dk": "DK",
    "gr": "GR",
    "hr": "HR",
    "us": "US",
    "ca": "CA",
}


def _summarise_query_filters(url):
    """Return short, human-readable chips describing a query's filters.

    Numeric IDs (brands, sizes, categories) can't be resolved to names without
    a lookup Vinted blocks, so those are summarised as counts. Price range and
    the Vinted country are shown directly.
    """
    chips = []
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
    except (ValueError, TypeError):
        return chips

    host = (parsed.netloc or "").lower()
    marker = "vinted."
    if marker in host:
        suffix = host.split(marker, 1)[1]
        chips.append(_VINTED_COUNTRY.get(suffix, suffix.upper()))

    currency = (params.get("currency", [""])[0] or "").upper()
    symbol = _CURRENCY_SYMBOLS.get(currency, (currency + " ") if currency else "")
    price_from = params.get("price_from", [None])[0]
    price_to = params.get("price_to", [None])[0]
    if price_from and price_to:
        chips.append(f"{symbol}{price_from}–{symbol}{price_to}")
    elif price_to:
        chips.append(f"≤ {symbol}{price_to}")
    elif price_from:
        chips.append(f"≥ {symbol}{price_from}")

    def _count(*keys):
        return sum(len(params.get(key, [])) for key in keys)

    def _add_count(count, singular, plural):
        if count == 1:
            chips.append(f"1 {singular}")
        elif count > 1:
            chips.append(f"{count} {plural}")

    _add_count(_count("brand_id[]", "brand_ids[]"), "brand", "brands")
    _add_count(_count("catalog[]", "catalog_ids[]"), "category", "categories")
    _add_count(_count("size_id[]", "size_ids[]"), "size", "sizes")
    _add_count(_count("color_id[]", "color_ids[]"), "colour", "colours")
    _add_count(
        _count("status[]", "status_ids[]", "status_id[]"),
        "condition",
        "conditions",
    )

    if (params.get("is_for_swap", ["0"])[0] or "0") in ("1", "true"):
        chips.append("swap")

    return chips


def _canonical_nonnegative_decimal(raw_value, label):
    """Validate a money form field and return a stable decimal string."""
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return None

    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a valid number.") from None

    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a non-negative finite number.")

    canonical = format(value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def _query_preferences_from_form():
    """Parse and validate the shared per-query alert preferences."""
    poll_mode = request.form.get("poll_mode", "normal").strip().lower()
    if poll_mode not in {"normal", "fast"}:
        raise ValueError("Polling speed must be Normal or Fast.")

    currency = request.form.get("deal_currency", "GBP").strip().upper() or "GBP"
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ValueError("Deal evaluator currency must be a three-letter code.")

    excellent_max = _canonical_nonnegative_decimal(
        request.form.get("deal_excellent_max"),
        "Excellent price",
    )
    good_max = _canonical_nonnegative_decimal(
        request.form.get("deal_good_max"),
        "Good price",
    )
    deal_mode = request.form.get("deal_mode", "off").strip().lower()
    evaluator_enabled = deal_mode == "ceiling"

    if evaluator_enabled and (excellent_max is None or good_max is None):
        raise ValueError(
            "Enter both Excellent and Good maximum prices to enable the deal evaluator."
        )
    if (excellent_max is None) != (good_max is None):
        raise ValueError("Enter both deal-price thresholds, or leave both blank.")
    if excellent_max is not None and Decimal(excellent_max) > Decimal(good_max):
        raise ValueError("Excellent price must be less than or equal to Good price.")

    return {
        "poll_mode": poll_mode,
        "monitor_during_quiet_hours": ("monitor_during_quiet_hours" in request.form),
        "deal_evaluator_enabled": evaluator_enabled,
        "deal_excellent_max": excellent_max,
        "deal_good_max": good_max,
        "deal_currency": currency,
    }


def _query_preferences_with_defaults(preferences):
    merged = dict(_QUERY_PREFERENCE_DEFAULTS)
    if preferences:
        merged.update(preferences)
    return merged


def _current_query_edit_state(query_id):
    """Load current raw editable values and their deterministic revision."""
    state = db.get_query_edit_state(query_id)
    if state is None:
        return None
    state = dict(state)
    # Hash the raw database state (where an absent name is NULL). The atomic
    # writer compares against that same representation inside its transaction.
    revision = db.query_edit_revision(state)
    if not revision:
        raise RuntimeError("Could not create an edit revision.")
    # The database intentionally preserves a NULL name. Keep it blank in the
    # form instead of substituting the display label derived from search_text.
    state["query_name"] = state.get("query_name") or ""
    ai_enabled = bool(state.get("deal_ai_enabled"))
    state["deal_mode"] = (
        "ai"
        if ai_enabled
        else "ceiling" if state.get("deal_evaluator_enabled") else "off"
    )
    state["revision"] = revision
    return state


def _no_store_json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _active_fast_query_ids(preferences_map=None, enabled_map=None):
    preferences_map = (
        preferences_map
        if preferences_map is not None
        else db.get_query_preferences_map()
    )
    if preferences_map is None:
        raise ValueError("Could not verify the active Fast-query limit.")
    enabled_map = enabled_map if enabled_map is not None else db.get_query_enabled_map()
    return {
        query_id
        for query_id, enabled in enabled_map.items()
        if enabled
        and _query_preferences_with_defaults(preferences_map.get(query_id)).get(
            "poll_mode"
        )
        == "fast"
    }


def _fast_query_limit_exceeded(
    query_id,
    poll_mode,
    will_be_enabled=True,
    preferences_map=None,
    enabled_map=None,
):
    """Return True if this change would create a sixth active Fast query."""
    if poll_mode != "fast" or not will_be_enabled:
        return False
    active_fast = _active_fast_query_ids(
        preferences_map=preferences_map,
        enabled_map=enabled_map,
    )
    if query_id is not None:
        active_fast.discard(query_id)
    return len(active_fast) >= _MAX_ACTIVE_FAST_QUERIES


def _save_query_preferences(query_id, preferences):
    return db.set_query_preferences(query_id, **preferences)


def _ai_deal_query_ids():
    """Query IDs configured to use AI deal evaluation (CSV parameter)."""
    ids = set()
    for part in str(db.get_parameter("deal_ai_query_ids") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


@app.route("/queries")
def queries():
    # Get queries, newest Last Found Item first; Never entries last.
    all_queries = _queries_newest_first(db.get_queries())
    # Fetch pause-state and item counts once each to avoid per-row queries.
    enabled_map = db.get_query_enabled_map()
    item_counts = db.get_query_item_counts()
    preferences_map = db.get_query_preferences_map([query[0] for query in all_queries])
    if preferences_map is None:
        logger.error("Could not load query preferences for the Queries page.")
        preferences_map = {}
    formatted_queries = []
    ai_ids = _ai_deal_query_ids()
    for i, query in enumerate(all_queries):
        parsed_query = urlparse(query[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        # last_item is already included in db.get_queries().
        last_timestamp = query[2]
        # last_found_timestamp is the raw epoch used for client-side sorting;
        # None (rendered as "Never") always sorts to the bottom.
        last_found_timestamp = None
        if last_timestamp is None:
            last_found_item = "Never"
        else:
            try:
                last_found_timestamp = float(last_timestamp)
                last_found_item = datetime.fromtimestamp(last_found_timestamp).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (TypeError, ValueError, OSError, OverflowError) as error:
                logger.warning(
                    "Could not format last_item %r for query %s: %s",
                    last_timestamp,
                    query[0],
                    error,
                )
                last_found_item = "Never"
                last_found_timestamp = None

        preferences = _query_preferences_with_defaults(preferences_map.get(query[0]))
        currency = str(preferences["deal_currency"] or "GBP").upper()
        currency_prefix = "£" if currency == "GBP" else f"{currency} "

        formatted_queries.append(
            {
                "id": i + 1,
                "query_id": query[0],
                "query": query[1],
                "display": query_name if query_name else query[1],
                "last_found_item": last_found_item,
                "last_found_timestamp": last_found_timestamp,
                "enabled": enabled_map.get(query[0], True),
                "item_count": item_counts.get(query[0], 0),
                "filters": _summarise_query_filters(query[1]),
                **preferences,
                "deal_ai_enabled": query[0] in ai_ids,
                "deal_currency_prefix": currency_prefix,
            }
        )

    return render_template(
        "queries.html",
        queries=formatted_queries,
        max_active_fast_queries=_MAX_ACTIVE_FAST_QUERIES,
    )


@app.route("/api/queries/<int:query_id>/edit-state")
def query_edit_state(query_id):
    """Return fresh editable values so a stale page cannot seed the modal."""
    try:
        state = _current_query_edit_state(query_id)
    except Exception:
        logger.exception("Could not load edit state for query %s", query_id)
        return _no_store_json(
            {"status": "error", "message": "Could not load this query."},
            500,
        )

    if state is None:
        return _no_store_json(
            {"status": "error", "message": "Query not found."},
            404,
        )
    return _no_store_json(state)


@app.route("/add_query", methods=["POST"])
def add_query():
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()

    if query:
        try:
            preferences = _query_preferences_from_form()
            if _fast_query_limit_exceeded(None, preferences["poll_mode"]):
                raise ValueError(
                    f"At most {_MAX_ACTIVE_FAST_QUERIES} active queries can use Fast polling."
                )
            query = normalise_vinted_url(query)
            message, is_new_query = core.process_query(
                query, name=query_name if query_name != "" else None
            )
            if is_new_query:
                saved_query = db.get_query_by_url(query)
                if saved_query and _save_query_preferences(saved_query[0], preferences):
                    flash(f"Query added: {query}", "success")
                else:
                    flash(
                        "Query added, but its alert preferences could not be saved. "
                        "Open Edit and try again.",
                        "error",
                    )
            else:
                flash(message, "warning")
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
    else:
        flash("No query provided", "error")

    return redirect(url_for("queries"))


@app.route("/add_query/bulk", methods=["POST"])
def add_query_bulk():
    raw = request.form.get("queries", "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    added = duplicates = failed = 0
    for line in lines:
        try:
            normalised = normalise_vinted_url(line)
            message, _success = core.process_query(normalised, name=None)
            if message == "Query added.":
                added += 1
            elif "already" in message.lower():
                duplicates += 1
            else:
                failed += 1
        except (ValueError, TypeError):
            failed += 1

    if not lines:
        flash("No URLs provided.", "warning")
    else:
        parts = [f"{added} added"]
        if duplicates:
            parts.append(f"{duplicates} already existed")
        if failed:
            parts.append(f"{failed} invalid")
        flash("Bulk add: " + ", ".join(parts) + ".", "success" if added else "warning")

    return redirect(url_for("queries"))


@app.route("/remove_query/<int:query_id>", methods=["POST"])
def remove_query(query_id):
    message, success = core.process_remove_query(str(query_id))
    if success:
        flash("Query removed", "success")
    else:
        flash(message, "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/all", methods=["POST"])
def remove_all_queries():
    message, success = core.process_remove_query("all")
    if success:
        flash("All queries removed", "success")
    else:
        flash(message, "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/bulk", methods=["POST"])
def remove_query_bulk():
    ids = request.form.getlist("query_ids")
    removed = 0
    for raw_id in ids:
        if str(raw_id).isdigit():
            _message, success = core.process_remove_query(str(raw_id))
            if success:
                removed += 1

    if removed:
        flash(
            f"Removed {removed} quer{'y' if removed == 1 else 'ies'}.",
            "success",
        )
    else:
        flash("No queries were removed.", "warning")

    return redirect(url_for("queries"))


@app.route("/pause_query/bulk", methods=["POST"])
def pause_query_bulk():
    selected_ids = _selected_query_ids()

    if not selected_ids:
        flash("Select at least one query to pause.", "warning")
        return redirect(url_for("queries"))

    paused = db.set_queries_enabled(selected_ids, False)
    if paused is None:
        flash("Could not pause the selected queries.", "error")
    elif paused:
        flash(
            f"Paused {paused} quer{'y' if paused == 1 else 'ies'}.",
            "success",
        )
    else:
        flash("The selected queries were already paused.", "info")

    return redirect(url_for("queries"))


def _selected_query_ids():
    """Return unique, positive query IDs submitted by a bulk-action form."""
    ids = request.form.getlist("query_ids")
    selected_ids = []
    for raw_id in ids:
        if str(raw_id).isdigit():
            query_id = int(raw_id)
            if query_id > 0 and query_id not in selected_ids:
                selected_ids.append(query_id)
    return selected_ids


@app.route("/resume_query/bulk", methods=["POST"])
def resume_query_bulk():
    selected_ids = _selected_query_ids()
    if not selected_ids:
        flash("Select at least one query to resume.", "warning")
        return redirect(url_for("queries"))

    enabled_map = db.get_query_enabled_map()
    preferences_map = db.get_query_preferences_map(selected_ids)
    if preferences_map is None:
        flash("Could not read query preferences. No queries were resumed.", "error")
        return redirect(url_for("queries"))
    all_preferences = db.get_query_preferences_map()
    if all_preferences is None:
        flash("Could not verify the active Fast-query limit.", "error")
        return redirect(url_for("queries"))
    active_fast = _active_fast_query_ids(
        preferences_map=all_preferences,
        enabled_map=enabled_map,
    )
    fast_to_resume = {
        query_id
        for query_id in selected_ids
        if not enabled_map.get(query_id, False)
        and _query_preferences_with_defaults(preferences_map.get(query_id)).get(
            "poll_mode"
        )
        == "fast"
    }
    if len(active_fast | fast_to_resume) > _MAX_ACTIVE_FAST_QUERIES:
        flash(
            f"Cannot resume these queries: at most {_MAX_ACTIVE_FAST_QUERIES} "
            "active queries can use Fast polling.",
            "error",
        )
        return redirect(url_for("queries"))

    resume_status, resumed = db.set_queries_enabled_with_fast_limit(
        selected_ids,
        True,
    )
    if resume_status == "fast_limit":
        flash(
            f"Cannot resume these queries: at most {_MAX_ACTIVE_FAST_QUERIES} "
            "active queries can use Fast polling.",
            "error",
        )
    elif resume_status == "error":
        flash("Could not resume the selected queries.", "error")
    elif resumed:
        flash(
            f"Resumed {resumed} quer{'y' if resumed == 1 else 'ies'}.",
            "success",
        )
    else:
        flash("The selected queries were already active.", "info")

    return redirect(url_for("queries"))


@app.route("/toggle_query/<int:query_id>", methods=["POST"])
def toggle_query(query_id):
    enabled_map = db.get_query_enabled_map()
    if query_id not in enabled_map:
        return jsonify({"status": "error", "message": "Query not found."}), 404

    new_state = not enabled_map[query_id]
    stored_preferences = db.get_query_preferences(query_id)
    if stored_preferences is None:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Could not read this query's preferences.",
                }
            ),
            500,
        )
    preferences = _query_preferences_with_defaults(stored_preferences)
    try:
        fast_limit_exceeded = new_state and _fast_query_limit_exceeded(
            query_id,
            preferences["poll_mode"],
            enabled_map=enabled_map,
        )
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 500
    if fast_limit_exceeded:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": (
                        f"At most {_MAX_ACTIVE_FAST_QUERIES} active queries "
                        "can use Fast polling."
                    ),
                }
            ),
            409,
        )
    toggle_status = db.set_query_enabled_with_fast_limit(query_id, new_state)
    if toggle_status == "fast_limit":
        return (
            jsonify(
                {
                    "status": "error",
                    "message": (
                        f"At most {_MAX_ACTIVE_FAST_QUERIES} active queries "
                        "can use Fast polling."
                    ),
                }
            ),
            409,
        )
    if toggle_status == "not_found":
        return jsonify({"status": "error", "message": "Query not found."}), 404
    if toggle_status in {"updated", "unchanged"}:
        return jsonify({"status": "success", "enabled": new_state})

    return (
        jsonify({"status": "error", "message": "Could not update the query."}),
        500,
    )


@app.route("/update_query/<int:query_id>", methods=["POST"])
def update_query(query_id):
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()

    submitted_revision = request.form.get("edit_revision", "").strip()
    if not submitted_revision:
        flash(
            "This edit form is incomplete. Nothing was saved; please reopen it "
            "and try again.",
            "warning",
        )
        return redirect(url_for("queries"))

    if query:
        try:
            preferences = _query_preferences_from_form()
            enabled = db.get_query_enabled_map().get(query_id, False)
            if _fast_query_limit_exceeded(
                query_id,
                preferences["poll_mode"],
                will_be_enabled=enabled,
            ):
                raise ValueError(
                    f"At most {_MAX_ACTIVE_FAST_QUERIES} active queries can use Fast polling."
                )
            query = normalise_vinted_url(query)
            ai_enabled = request.form.get("deal_mode", "off").strip().lower() == "ai"
            result = db.update_query_configuration_atomic(
                query_id,
                query,
                query_name if query_name != "" else None,
                preferences,
                ai_enabled,
                expected_revision=submitted_revision,
            )
            if result == "updated":
                flash("Query updated", "success")
            elif result == "fast_limit":
                flash(
                    f"At most {_MAX_ACTIVE_FAST_QUERIES} active queries can "
                    "use Fast polling. Nothing was saved.",
                    "warning",
                )
            elif result == "stale":
                flash(
                    "This query changed after the edit form was opened. Nothing "
                    "was saved; please reopen it and try again.",
                    "warning",
                )
            elif result == "not_found":
                flash("That query no longer exists. Nothing was changed.", "warning")
            else:
                flash("The query could not be updated. Nothing was changed.", "error")
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
        except Exception:
            logger.exception("Could not atomically update query %s", query_id)
            flash("The query could not be updated. Nothing was changed.", "error")
    else:
        flash("No query provided", "error")

    return redirect(url_for("queries"))


_VALID_ITEM_SORTS = {
    "discovered_desc",
    "date_desc",
    "date_asc",
    "price_asc",
    "price_desc",
    "title_asc",
}
_ITEMS_PAGE_SIZE = 24


@app.route("/items")
def items():
    query_id = request.args.get("query", "").strip()
    search = request.args.get("search", "").strip()
    sort = request.args.get("sort", "discovered_desc")
    if sort not in _VALID_ITEM_SORTS:
        sort = "discovered_desc"

    def _parse_price(name):
        raw = request.args.get(name, "").strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    price_min = _parse_price("price_min")
    price_max = _parse_price("price_max")

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    # Resolve the selected query id to its stored URL for filtering.
    query_string = None
    if query_id:
        for q in db.get_queries():
            if str(q[0]) == query_id:
                query_string = q[1]
                break

    total = db.count_items(
        query=query_string,
        search=search or None,
        price_min=price_min,
        price_max=price_max,
    )
    total_pages = max(1, (total + _ITEMS_PAGE_SIZE - 1) // _ITEMS_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * _ITEMS_PAGE_SIZE

    items_data = db.get_items(
        limit=_ITEMS_PAGE_SIZE,
        offset=offset,
        query=query_string,
        search=search or None,
        price_min=price_min,
        price_max=price_max,
        sort=sort,
    )

    formatted_items = []
    for item in items_data:
        search_text = parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
        listed_timestamp = datetime.fromtimestamp(item[4]).strftime("%Y-%m-%d %H:%M:%S")
        discovered_timestamp = datetime.fromtimestamp(item[8]).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        formatted_items.append(
            {
                "title": item[1],
                "price": item[2],
                "currency": item[3],
                "timestamp": discovered_timestamp,
                "timestamp_raw": item[8],
                "listed_timestamp": listed_timestamp,
                "listed_timestamp_raw": item[4],
                "query": item[7] or search_text or item[5],
                "url": (
                    f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}"
                    f"/items/{item[0]}"
                ),
                "photo_url": item[6],
            }
        )

    # Queries for the filter dropdown.
    formatted_queries = []
    selected_query_display = None
    for i, q in enumerate(db.get_queries()):
        query_params = parse_qs(urlparse(q[1]).query)
        query_name = (
            q[3] if q[3] is not None else query_params.get("search_text", [None])[0]
        )
        display_name = query_name if query_name else q[0]
        if query_id == str(q[0]):
            selected_query_display = display_name
        formatted_queries.append(
            {"id": i + 1, "query_id": q[0], "query": q[1], "display": display_name}
        )

    # Pagination links that preserve the active filters.
    filter_args = {}
    if query_id:
        filter_args["query"] = query_id
    if search:
        filter_args["search"] = search
    if request.args.get("price_min", "").strip():
        filter_args["price_min"] = request.args.get("price_min").strip()
    if request.args.get("price_max", "").strip():
        filter_args["price_max"] = request.args.get("price_max").strip()
    if sort != "discovered_desc":
        filter_args["sort"] = sort
    prev_url = url_for("items", page=page - 1, **filter_args) if page > 1 else None
    next_url = (
        url_for("items", page=page + 1, **filter_args) if page < total_pages else None
    )
    first_url = url_for("items", page=1, **filter_args) if page > 1 else None
    last_url = (
        url_for("items", page=total_pages, **filter_args)
        if page < total_pages
        else None
    )

    return render_template(
        "items.html",
        items=formatted_items,
        queries=formatted_queries,
        selected_query=query_id,
        selected_query_display=selected_query_display,
        search=search,
        price_min=request.args.get("price_min", "").strip(),
        price_max=request.args.get("price_max", "").strip(),
        sort=sort,
        page=page,
        total_pages=total_pages,
        total_items=total,
        prev_url=prev_url,
        next_url=next_url,
        first_url=first_url,
        last_url=last_url,
    )


def _validated_int(name, default, minimum, maximum):
    raw_value = request.form.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(
            f"{name.replace('_', ' ').title()} must be a number."
        ) from error
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name.replace('_', ' ').title()} must be between "
            f"{minimum} and {maximum}."
        )
    return value


def _validate_message_template(template):
    if not template.strip():
        raise ValueError("The notification message template cannot be empty.")

    allowed = {"title", "price", "brand", "condition", "image"}
    fields = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }
    unknown = fields - allowed
    if unknown:
        raise ValueError(
            "Unsupported notification template variable(s): "
            + ", ".join(sorted(unknown))
        )

    template.format(**{field: "test" for field in allowed})


def _query_efficiency_schedule():
    """Return the current clean-run cadence model for the read-only report."""

    params = db.get_all_parameters()
    enabled_map = db.get_query_enabled_map()
    active_ids = [query_id for query_id, enabled in enabled_map.items() if enabled]

    try:
        refresh_delay = int(params.get("query_refresh_delay") or 180)
    except (TypeError, ValueError):
        refresh_delay = 180
    try:
        fast_delay = int(params.get("fast_query_refresh_delay") or 90)
    except (TypeError, ValueError):
        fast_delay = 90

    request_spacing = scraper_rate.bounded_request_spacing(
        params.get("catalogue_request_spacing_seconds")
    )
    try:
        preferences = db.get_query_preferences_map(query_ids=active_ids) or {}
    except Exception:
        logger.exception("Could not load preferences for the efficiency report.")
        preferences = {}

    fast_count = sum(
        str(preferences.get(query_id, {}).get("poll_mode", "normal")).lower() == "fast"
        for query_id in active_ids
    )
    normal_count = len(active_ids) - fast_count
    cadence = scraper_rate.build_cadence_plan(
        normal_count=normal_count,
        fast_count=fast_count,
        requested_normal_seconds=refresh_delay,
        requested_fast_seconds=fast_delay,
        request_spacing_seconds=request_spacing,
    )
    volume = query_efficiency.request_volume_scenario(
        normal_count=normal_count,
        fast_count=fast_count,
        effective_normal_seconds=cadence["effective_normal_seconds"],
        effective_fast_seconds=cadence["effective_fast_seconds"],
    )
    return {
        "cadence": cadence,
        "volume": volume,
        "normal_latency": query_efficiency.periodic_poll_latency(
            cadence["effective_normal_seconds"]
        ),
        "fast_latency": query_efficiency.periodic_poll_latency(
            cadence["effective_fast_seconds"]
        ),
    }


@app.route("/query-efficiency")
def query_efficiency_report():
    """Render privacy-safe, read-only catalogue-query evidence."""

    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        abort(400, description="days must be one of 7, 30, or 90")
    if days not in {7, 30, 90}:
        abort(400, description="days must be one of 7, 30, or 90")

    report = query_observability.get_efficiency_report(days=days)
    schedule = _query_efficiency_schedule()
    return render_template(
        "query_efficiency.html",
        report=report,
        schedule=schedule,
        selected_days=days,
        allowed_days=(7, 30, 90),
    )


@app.route("/config")
def config():
    params = db.get_all_parameters()
    telegram_token_configured = bool(params.get("telegram_token", "").strip())
    params = dict(params)
    params["telegram_token"] = ""

    enabled_map = db.get_query_enabled_map()
    active_ids = [query_id for query_id, on in enabled_map.items() if on]
    active_count = len(active_ids)
    try:
        refresh_delay = int(params.get("query_refresh_delay") or 180)
    except (TypeError, ValueError):
        refresh_delay = 180
    try:
        fast_delay = int(params.get("fast_query_refresh_delay") or 90)
    except (TypeError, ValueError):
        fast_delay = 90
    request_spacing = scraper_rate.bounded_request_spacing(
        params.get("catalogue_request_spacing_seconds")
    )
    try:
        preferences = db.get_query_preferences_map(query_ids=active_ids) or {}
    except Exception:
        preferences = {}
    fast_count = sum(
        str(preferences.get(query_id, {}).get("poll_mode", "normal")).lower() == "fast"
        for query_id in active_ids
    )
    cadence = scraper_rate.build_cadence_plan(
        normal_count=active_count - fast_count,
        fast_count=fast_count,
        requested_normal_seconds=refresh_delay,
        requested_fast_seconds=fast_delay,
        request_spacing_seconds=request_spacing,
    )
    query_health = {
        "total": len(enabled_map),
        "active": active_count,
        "paused": sum(1 for on in enabled_map.values() if not on),
        "normal_count": cadence["normal_count"],
        "fast_count": cadence["fast_count"],
        "requested_normal_seconds": cadence["requested_normal_seconds"],
        "requested_fast_seconds": cadence["requested_fast_seconds"],
        "effective_normal_seconds": cadence["effective_normal_seconds"],
        "effective_fast_seconds": cadence["effective_fast_seconds"],
        "request_spacing_seconds": cadence["request_spacing_seconds"],
        "scheduled_requests_per_minute": round(
            cadence["scheduled_requests_per_minute"], 1
        ),
    }

    return render_template(
        "config.html",
        params=params,
        telegram_token_configured=telegram_token_configured,
        query_health=query_health,
        timezones=sorted(available_timezones()),
    )


@app.route("/test_telegram", methods=["POST"])
def test_telegram():
    """Send a one-off test message using the saved bot token and Chat ID."""
    token = (db.get_parameter("telegram_token") or "").strip()
    chat_id = (db.get_parameter("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Save a bot token and Chat ID first, then try again.",
                }
            ),
            400,
        )

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    "✅ Vinted Notifications test message — "
                    "your Telegram alerts are configured correctly."
                ),
            },
            timeout=(3.05, 10),
        )
        payload = response.json()
        if not isinstance(payload, dict):
            logger.warning("Telegram test message returned an invalid response shape.")
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Telegram returned an invalid response. Try again.",
                    }
                ),
                502,
            )
        if response.status_code == 200 and payload.get("ok"):
            return jsonify(
                {"status": "success", "message": "Test message sent — check Telegram."}
            )
        description = str(payload.get("description", f"HTTP {response.status_code}"))
        # Telegram usually returns harmless guidance, but never trust a remote
        # error body with a credential that was present in the request URL.
        description = redact_secrets(description.replace(token, "[REDACTED]"))[:500]
        return (
            jsonify(
                {"status": "error", "message": f"Telegram rejected it: {description}"}
            ),
            400,
        )
    except (requests.RequestException, ValueError) as error:
        # Requests exceptions can contain the full Telegram URL, including the
        # bot token. Record the error type without copying credentials to logs.
        logger.warning("Telegram test message failed (%s)", type(error).__name__)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": (
                        "Could not reach Telegram. Check the connection and try again."
                    ),
                }
            ),
            502,
        )


@app.route("/config/health", methods=["GET"])
def config_health():
    health = core.get_scraper_health()
    pause_available = health.get("pause_available") is True
    manual_pause = {
        "active": bool(health.get("pause_active", not pause_available)),
        "available": pause_available,
        "until": health.get("pause_until", 0),
        "remaining": health.get("pause_remaining"),
        "reason": str(health.get("pause_reason") or ""),
        # Keep the response shape stable if the core health snapshot grows this
        # field later. The controls do not depend on the timestamp.
        "started_at": health.get("pause_started_at", 0),
    }
    if not pause_available:
        status = "safety_stop"
    elif manual_pause["active"]:
        status = "paused"
    elif health["stalled"]:
        status = "stalled"
    elif health["blocked"]:
        status = "blocked"
    else:
        status = "ok"

    enabled_map = db.get_query_enabled_map()

    quiet_active, quiet_start, quiet_end, quiet_timezone = core.get_quiet_hours_status()
    quiet_enabled = (
        str(db.get_parameter("quiet_hours_enabled") or "False").strip().lower()
        == "true"
    )

    return jsonify(
        {
            "pending_notifications": db.count_pending_notifications(),
            "scraper": {
                "status": status,
                "heartbeat_age": health["heartbeat_age"],
                "last_ok_age": health["last_ok_age"],
                "failed_cycles": health["failed_cycles"],
                "cooldown_active": health["cooldown_active"],
                "cooldown_remaining": health["cooldown_remaining"],
                "cooldown_level": health["cooldown_level"],
                "last_block_status": health["last_block_status"],
                "manual_pause": manual_pause,
            },
            "queries": {
                "total": len(enabled_map),
                "active": sum(1 for on in enabled_map.values() if on),
                "paused": sum(1 for on in enabled_map.values() if not on),
            },
            "quiet_hours": {
                "active": quiet_active,
                "enabled": quiet_enabled,
                "start": quiet_start,
                "end": quiet_end,
                "timezone": quiet_timezone,
            },
        }
    )


@app.route("/scraper/pause", methods=["POST"])
def pause_scraper():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    preset_name = str(payload.get("preset") or request.form.get("preset") or "").strip()
    preset = _SCRAPER_PAUSE_PRESETS.get(preset_name)
    if preset is None:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Choose Phone blocked, 1 hour, 6 hours, or 24 hours.",
                }
            ),
            400,
        )

    try:
        pause_state = core.pause_scraper(
            duration_seconds=preset["duration_seconds"],
            reason=preset["reason"],
        )
    except Exception:
        logger.exception("Could not persist scraper pause requested from Web UI")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "The scraper could not be paused safely. Try again.",
                }
            ),
            503,
        )

    if (
        not pause_state
        or pause_state.get("available") is not True
        or not pause_state.get("active")
    ):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "The scraper pause state could not be saved. Try again.",
                }
            ),
            503,
        )

    logger.warning("Scraper paused from Web UI (%s)", preset["reason"])
    return jsonify(
        {
            "status": "success",
            "message": preset["message"],
            "pause": pause_state,
        }
    )


@app.route("/scraper/resume", methods=["POST"])
def resume_scraper():
    try:
        resumed = core.resume_scraper()
    except Exception:
        logger.exception("Could not clear scraper pause requested from Web UI")
        resumed = False

    if not resumed:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "The scraper could not be resumed. Try again.",
                }
            ),
            503,
        )

    logger.info("Scraper resumed from Web UI")
    try:
        pause_state = core.get_scraper_pause()
    except Exception:
        logger.exception("Could not verify scraper pause state after Web UI resume")
        pause_state = None

    if not pause_state or pause_state.get("available") is not True:
        pause_state = {
            "active": True,
            "available": False,
            "until": 0,
            "remaining": None,
            "reason": "Scraper safety state unavailable",
            "started_at": 0,
        }
        message = (
            "Manual pause cleared, but the safety state could not be verified. "
            "Vinted requests remain suppressed until it is readable."
        )
        response_status = "warning"
    else:
        message = "Scraping resumed. The safety request limit still applies."
        response_status = "success"

    return jsonify(
        {
            "status": response_status,
            "message": message,
            "pause": pause_state,
        }
    )


@app.route("/update_config", methods=["POST"])
def update_config():
    quiet_start = request.form.get("quiet_hours_start", "01:00").strip()
    quiet_end = request.form.get("quiet_hours_end", "06:00").strip()
    quiet_timezone = (
        request.form.get("quiet_hours_timezone", "Europe/London").strip()
        or "Europe/London"
    )

    try:
        datetime.strptime(quiet_start, "%H:%M")
        datetime.strptime(quiet_end, "%H:%M")
    except ValueError:
        flash("Quiet-hours times must be valid 24-hour times.", "error")
        return redirect(url_for("config"))

    if quiet_start == quiet_end:
        flash("Quiet-hours start and end times must be different.", "error")
        return redirect(url_for("config"))

    try:
        ZoneInfo(quiet_timezone)
    except ZoneInfoNotFoundError:
        flash(
            "Quiet-hours timezone is not recognised. Use an IANA name such as "
            "Europe/London.",
            "error",
        )
        return redirect(url_for("config"))

    # Days quiet hours apply on (Mon=0 .. Sun=6). Always written, so an empty
    # selection is stored as "" (no quiet days) rather than falling back to all.
    quiet_days = ",".join(
        sorted(
            {
                day
                for day in request.form.getlist("quiet_hours_days")
                if day.isdigit() and 0 <= int(day) <= 6
            },
            key=int,
        )
    )

    try:
        items_per_query = _validated_int("items_per_query", 20, 1, 100)
        query_refresh_delay = _validated_int(
            "query_refresh_delay",
            180,
            60,
            86400,
        )
        catalogue_request_spacing = _validated_int(
            "catalogue_request_spacing_seconds",
            60,
            60,
            120,
        )
        rss_port = _validated_int("rss_port", 8080, 1024, 65535)
        rss_max_items = _validated_int("rss_max_items", 100, 1, 1000)

        telegram_chat_id = request.form.get("telegram_chat_id", "").strip()
        if telegram_chat_id and not telegram_chat_id.lstrip("-").isdigit():
            raise ValueError("Telegram Chat ID must be numeric.")

        user_agents = request.form.get("user_agents", "[]").strip() or "[]"
        parsed_user_agents = json.loads(user_agents)
        if not isinstance(parsed_user_agents, list) or not all(
            isinstance(value, str) for value in parsed_user_agents
        ):
            raise ValueError("User Agents must be a JSON list of strings.")

        default_headers = request.form.get("default_headers", "{}").strip() or "{}"
        parsed_headers = json.loads(default_headers)
        if not isinstance(parsed_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in parsed_headers.items()
        ):
            raise ValueError("Default Headers must be a JSON object of strings.")

        message_template = request.form.get("message_template", "")
        _validate_message_template(message_template)
    except (
        ValueError,
        TypeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
    ) as error:
        flash(str(error), "error")
        return redirect(url_for("config"))

    existing_token = db.get_parameter("telegram_token") or ""
    submitted_token = request.form.get("telegram_token", "").strip()
    telegram_token = submitted_token or existing_token
    if "clear_telegram_token" in request.form:
        telegram_token = ""

    telegram_enabled = "telegram_enabled" in request.form
    if telegram_enabled and (not telegram_token or not telegram_chat_id):
        flash(
            "Telegram cannot be enabled without a bot token and Chat ID.",
            "error",
        )
        return redirect(url_for("config"))

    settings = {
        "telegram_enabled": str(telegram_enabled),
        "telegram_token": telegram_token,
        "telegram_chat_id": telegram_chat_id,
        "rss_enabled": str("rss_enabled" in request.form),
        "rss_port": rss_port,
        "rss_max_items": rss_max_items,
        "items_per_query": items_per_query,
        "query_refresh_delay": query_refresh_delay,
        "catalogue_request_spacing_seconds": catalogue_request_spacing,
        "banwords": request.form.get("banwords", ""),
        "quiet_hours_enabled": str("quiet_hours_enabled" in request.form),
        "quiet_hours_start": quiet_start,
        "quiet_hours_end": quiet_end,
        "quiet_hours_timezone": quiet_timezone,
        "quiet_hours_days": quiet_days,
        "check_proxies": str("check_proxies" in request.form),
        "proxy_list": request.form.get("proxy_list", ""),
        "proxy_list_link": request.form.get("proxy_list_link", ""),
        "message_template": message_template,
        "user_agents": user_agents,
        "default_headers": default_headers,
        "last_proxy_check_time": "1",
    }

    if not db.set_parameters(settings):
        flash("Configuration could not be saved.", "error")
        return redirect(url_for("config"))

    if not db.migrate_multi_user_schema():
        flash("Configuration saved, but administrator rotation failed.", "error")
        return redirect(url_for("config"))

    logger.info("Configuration updated; proxy cache reset")

    flash("Configuration updated", "success")
    return redirect(url_for("config"))


@app.route("/control/<process_name>/<action>", methods=["POST"])
def control_process(process_name, action):
    if process_name not in ["telegram", "rss"]:
        return jsonify({"status": "error", "message": "Invalid process name"})

    if action == "start":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": "Telegram bot already running"}
                )

            # Check if telegram_token and telegram_chat_id are set
            telegram_token = db.get_parameter("telegram_token")
            telegram_chat_id = db.get_parameter("telegram_chat_id")
            if not telegram_token or not telegram_chat_id:
                return jsonify(
                    {
                        "status": "error",
                        "message": "Please set Telegram token and chat ID in the configuration panel before starting the Telegram process",
                    }
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("telegram_process_running", "True")
            logger.info("Telegram bot process start requested")
            return jsonify(
                {"status": "success", "message": "Telegram bot start requested"}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": "RSS feed already running"}
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("rss_process_running", "True")
            logger.info("RSS feed process start requested")
            return jsonify({"status": "success", "message": "RSS feed start requested"})

    elif action == "stop":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") != "True":
                return jsonify(
                    {"status": "warning", "message": "Telegram bot not running"}
                )

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("telegram_process_running", "False")
            logger.info("Telegram bot process stop requested")
            return jsonify(
                {"status": "success", "message": "Telegram bot stop requested"}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") != "True":
                return jsonify({"status": "warning", "message": "RSS feed not running"})

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("rss_process_running", "False")
            logger.info("RSS feed process stop requested")
            return jsonify({"status": "success", "message": "RSS feed stop requested"})

    return jsonify({"status": "error", "message": "Invalid action"})


@app.route("/control/status", methods=["GET"])
def process_status():
    # Get process status from the database
    telegram_running = db.get_parameter("telegram_process_running") == "True"
    rss_running = db.get_parameter("rss_process_running") == "True"

    return jsonify({"telegram": telegram_running, "rss": rss_running})


@app.route("/allowlist")
def allowlist():
    countries = db.get_allowlist()
    if countries == 0:
        countries = []

    return render_template("allowlist.html", countries=countries)


@app.route("/add_country", methods=["POST"])
def add_country():
    country = request.form.get("country", "").strip().upper()
    if country:
        if len(country) != 2 or not country.isalpha():
            flash("Enter a valid two-letter country code.", "warning")
            return redirect(url_for("allowlist"))
        message, country_list = core.process_add_country(country)
        flash(message, "success" if "added" in message else "warning")
    else:
        flash("No country provided", "error")

    return redirect(url_for("allowlist"))


@app.route("/remove_country/<country>", methods=["POST"])
def remove_country(country):
    message, country_list = core.process_remove_country(country)
    flash(message, "success")

    return redirect(url_for("allowlist"))


@app.route("/clear_allowlist", methods=["POST"])
def clear_allowlist():
    db.clear_allowlist()
    flash("Allowlist cleared", "success")

    return redirect(url_for("allowlist"))


@app.route("/logs")
def logs():
    return render_template("logs.html")


@app.route("/api/logs")
def api_logs():
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
    except ValueError:
        return jsonify({"error": "offset and limit must be integers"}), 400
    level_filter = request.args.get("level", "all")
    search_filter = request.args.get("search", "").strip().casefold()
    module_filter = request.args.get("module", "").strip().casefold()
    hide_routine_http = request.args.get("hide_http", "1") not in {
        "0",
        "false",
        "no",
    }

    log_file_path = os.path.join("logs", "vinted.log")

    if not os.path.exists(log_file_path):
        return jsonify({"logs": [], "total": 0})

    # Parse log file
    log_entries = []
    total_matching_entries = 0

    try:
        with open(log_file_path, "r", encoding="utf-8") as file:
            # Read all lines from the file
            all_lines = file.readlines()

            # Process lines in reverse order (newest first)
            all_lines.reverse()

            # Regular expression to parse log lines
            # Format: 2023-09-15 12:34:56,789 - module_name - LEVEL - Message
            log_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ([^-]+) - ([A-Z]+) - (.+)"

            for line in all_lines:
                match = re.match(log_pattern, line.strip())
                if match:
                    timestamp, module, level, message = match.groups()
                    module = module.strip()
                    # Werkzeug may colourise access-log messages even when
                    # they are written to a file. Strip terminal escape codes
                    # so the web viewer stays readable and filtering works.
                    message = re.sub(r"\x1b\[[0-9;]*m", "", message)

                    # Apply level filter if specified
                    if level_filter != "all" and level != level_filter:
                        continue

                    if module_filter and module_filter not in module.casefold():
                        continue

                    searchable = f"{timestamp} {module} {level} {message}".casefold()
                    if search_filter and search_filter not in searchable:
                        continue

                    if (
                        hide_routine_http
                        and level == "INFO"
                        and module == "werkzeug"
                        and re.search(
                            r'"(?:GET|HEAD) /.* HTTP/\d(?:\.\d)?" (?:2\d\d|304) ',
                            message,
                        )
                    ):
                        continue

                    total_matching_entries += 1

                    result_index = total_matching_entries - 1
                    if result_index < offset or result_index >= offset + limit:
                        continue

                    log_entries.append(
                        {
                            "timestamp": timestamp,
                            "module": module,
                            "level": level,
                            "message": message,
                        }
                    )
    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        return jsonify({"logs": [], "total": 0, "error": str(e)})

    return jsonify({"logs": log_entries, "total": total_matching_entries})


def web_ui_process():
    logger.info("Web UI process started")
    try:
        from waitress import serve

        # Network-facing binds must have credentials and a persistent session
        # key. Localhost-only development may omit both.
        host = os.environ.get("VN_WEB_HOST", "0.0.0.0")
        port = int(os.environ.get("VN_WEB_PORT", "8000"))
        _validate_web_bind_security(host)
        logger.info("Serving Web UI on %s:%s", host, port)
        serve(app, host=host, port=port, threads=4)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Web UI process stopped")
    except Exception as e:
        logger.error(f"Error in web UI process: {e}", exc_info=True)
