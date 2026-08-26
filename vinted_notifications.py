import multiprocessing
import time
import os
import sys
import html
import json
from datetime import datetime, timezone
from pathlib import Path


def _load_env_file():
    """Load a project-root .env into the environment before settings are read.

    This lets VN_* configuration (VN_WEB_HOST, VN_WEB_USERNAME, VN_SECRET_KEY,
    ...) come from a file instead of only the shell. Existing environment
    variables always win (override=False), so real ``-e``/compose values still
    take precedence over the file. When no .env exists — or python-dotenv is
    not installed — this is a no-op and the app behaves exactly as before.

    Called at import time (not only under ``__main__``) so that spawn-based
    child processes on Windows reload the file when they re-import this module;
    fork-based children on Linux simply inherit the already-populated
    environment. Either way the Web UI/RSS child processes see the values.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return False, None
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False, "missing-dotenv"
    load_dotenv(env_path)
    return True, str(env_path)


# Populate the environment before importing anything that reads VN_* settings.
_env_loaded, _env_detail = _load_env_file()

import db  # noqa: E402
import query_observability  # noqa: E402
import scraper_rate  # noqa: E402
from apscheduler.executors.pool import ThreadPoolExecutor  # noqa: E402
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from logger import (  # noqa: E402
    LoggedProcess,
    get_logger,
    start_logging_listener,
    stop_logging_listener,
)

# Get logger for this module
logger = get_logger(__name__)

# Log the .env outcome once, from the main process only (children stay quiet).
if __name__ == "__main__":
    if _env_loaded:
        logger.info("Loaded environment variables from %s", _env_detail)
    elif _env_detail == "missing-dotenv":
        logger.warning(
            "Found a .env file but python-dotenv is not installed; its values "
            "were not loaded. Install dependencies with "
            "'pip install -r requirements.txt'."
        )

# Global process references
telegram_process = None
rss_process = None
scrape_process = None
item_extractor_process = None
ai_process = None
current_query_refresh_delay = None
app_logging_queue = None
vinted_request_gate_lock = None
vinted_request_next_allowed = None
vinted_request_lease_until = None
vinted_request_owner_counter = None
vinted_request_current_owner = None

_SCRAPER_JOB_PREFIX = "scrape_query_"
_SCRAPER_DISPATCH_JOB_ID = "scraper_dispatch"
_SCRAPER_PLAN_ATTRIBUTE = "_vinted_scraper_plan"
_AI_JOB_FIELDS = (
    "job_id",
    "item_id",
    "query_id",
    "title",
    "brand",
    "condition",
    "price",
    "currency",
    "photo_url",
    "item_url",
    "chat_ids_json",
    "parent_notification_id",
    "attempts",
    "delivered_chat_ids_json",
    "handled_chat_ids_json",
    "result_content",
    "evaluation_started_at",
)
_AI_EVALUATION_MAX_ATTEMPTS = 4
_AI_EVALUATION_BACKOFFS = (60, 5 * 60, 15 * 60)
_AI_PARENT_POLL_SECONDS = 10
_AI_WORKER_IDLE_SECONDS = 1


def _create_process(*, target, args=()):
    """Create an application child wired to the parent-owned log listener.

    Unit-level callers that invoke process-monitor helpers without running the
    production entry point retain the previous plain-Process behaviour.
    """
    if app_logging_queue is None:
        return multiprocessing.Process(target=target, args=args)
    return LoggedProcess(app_logging_queue, target=target, args=args)


def _configure_vinted_request_gate(
    lock,
    next_allowed,
    lease_until,
    owner_counter,
    current_owner,
):
    """Connect this child to the parent-owned cross-process Vinted gate."""
    from pyVintedVN.requester import configure_shared_request_gate

    configure_shared_request_gate(
        lock,
        next_allowed,
        lease_until,
        owner_counter,
        current_owner,
    )


def _watchdog_recovery_window_seconds():
    """Require a sustained recovery before allowing another watchdog alert."""
    try:
        refresh_delay = int(db.get_parameter("query_refresh_delay") or 300)
    except (TypeError, ValueError):
        refresh_delay = 300
    return max(30 * 60, min(refresh_delay * 2, 2 * 60 * 60))


def telegram_polling_enabled():
    """Whether this instance should receive Telegram bot commands.

    Telegram permits multiple processes to send with one bot token, but only
    one process may poll ``getUpdates``. Local test runs can therefore use
    ``--telegram-send-only`` (or ``VN_TELEGRAM_POLLING=false``) while the live
    server remains the sole command-polling instance.
    """
    if "--telegram-send-only" in sys.argv:
        return False

    configured = os.getenv("VN_TELEGRAM_POLLING")
    if configured is None:
        return True

    return configured.strip().lower() not in {"0", "false", "no", "off"}


def initialise_database():
    """Create, upgrade, and configure the database once in the parent."""
    if not os.path.exists("./data/vinted_notifications.db"):
        logger.info("Database not found, creating a new one.")
        os.makedirs("./data", exist_ok=True)
        if not db.create_or_update_sqlite_db("initial_db.sql"):
            raise RuntimeError("Failed to create the application database.")
        logger.info("Database created successfully")

    if not db.configure_database_runtime():
        raise RuntimeError("Failed to configure SQLite runtime settings.")

    current_version = db.get_parameter("version")
    migration_files = os.listdir("migrations")
    visited_versions = set()
    while True:
        if current_version in visited_versions:
            raise RuntimeError(
                f"Database migration cycle detected at version {current_version}."
            )
        visited_versions.add(current_version)
        migration_file = db.next_database_migration(current_version, migration_files)
        if not migration_file:
            break
        logger.info("Running migration: %s", migration_file)
        if not db.create_or_update_sqlite_db(
            os.path.join("migrations", migration_file)
        ):
            raise RuntimeError(f"Failed to run migration {migration_file}.")
        next_version = db.get_parameter("version")
        if not next_version or next_version == current_version:
            raise RuntimeError(
                f"Migration {migration_file} did not advance the database version."
            )
        current_version = next_version

    migrations = [
        (db.migrate_message_template, "notification message template"),
        (db.migrate_remove_description_field, "remove unreliable description field"),
        (db.migrate_pending_notifications_table, "durable notification outbox"),
        (db.migrate_pending_ai_evaluations_table, "durable AI evaluation queue"),
        (db.migrate_query_enabled_column, "per-query pause/enable"),
        (db.migrate_multi_user_schema, "multi-user Telegram support"),
        (db.migrate_query_uniqueness, "query uniqueness"),
        (db.migrate_quiet_hours_schema, "quiet-hours configuration"),
        (db.migrate_query_preferences_schema, "per-query monitoring preferences"),
        (
            query_observability.migrate_schema,
            "catalogue execution telemetry and durable discovery progress",
        ),
        (
            query_observability.prune_retention,
            "catalogue execution telemetry retention",
        ),
        (db.migrate_fork_identity, "fork identity"),
    ]
    for migration, label in migrations:
        if not migration():
            raise RuntimeError(f"Failed to initialise {label}.")


def _scrape_delay(key, default, minimum=30):
    """Return a bounded positive scheduler interval from application settings."""
    try:
        value = int(db.get_parameter(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _scrape_intervals():
    """Return (normal, fast), ensuring Fast can never be the slower mode."""
    normal = _scrape_delay("query_refresh_delay", 180, minimum=60)
    configured_fast = _scrape_delay("fast_query_refresh_delay", 90, minimum=60)
    return normal, min(normal, configured_fast)


def _scraper_request_spacing_seconds():
    """Return the DB-configured hard catalogue gap within safe bounds."""
    try:
        configured = db.get_parameter("catalogue_request_spacing_seconds")
    except Exception:
        logger.warning(
            "Could not read catalogue request spacing; using the safe default."
        )
        configured = None
    return scraper_rate.bounded_request_spacing(configured)


def _query_preference(preferences, query_id):
    if not isinstance(preferences, dict):
        return {}
    preference = preferences.get(query_id)
    if preference is None:
        preference = preferences.get(str(query_id), {})
    return preference if isinstance(preference, dict) else {}


def _query_poll_mode(preference):
    mode = str(preference.get("poll_mode", "normal")).strip().lower()
    return "fast" if mode == "fast" else "normal"


def _get_query_preferences(query_ids):
    getter = getattr(db, "get_query_preferences_map", None)
    if getter is None:
        return {}
    try:
        preferences = getter(query_ids=query_ids)
    except Exception:
        logger.error("Could not load per-query monitoring preferences.", exc_info=True)
        return None
    return preferences if isinstance(preferences, dict) else None


def _job_interval_seconds(job):
    interval = getattr(getattr(job, "trigger", None), "interval", None)
    if interval is None:
        return None
    return int(interval.total_seconds())


def _timestamp(now=None):
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _empty_reconcile_result(plan=None, active=0):
    plan = plan or {}
    return {
        "active": active,
        "normal_interval": plan.get("effective_normal_seconds"),
        "fast_interval": plan.get("effective_fast_seconds"),
        "request_spacing": plan.get("request_spacing_seconds"),
        "changed": 0,
        "reconciled": False,
    }


def _plan_entry_changed(old_entry, mode, monitor, interval):
    return not old_entry or (
        old_entry.get("mode") != mode
        or old_entry.get("monitor_during_quiet_hours") != monitor
        or old_entry.get("interval") != interval
    )


def _ensure_scraper_dispatch_job(scheduler, items_queue, spacing, now=None):
    """Install the sole retrying dispatcher even before a plan read succeeds."""
    dispatch_interval = int(scraper_rate.bounded_request_spacing(spacing))
    dispatch_job = scheduler.get_job(_SCRAPER_DISPATCH_JOB_ID)
    next_run = datetime.fromtimestamp(
        _timestamp(now) + dispatch_interval,
        timezone.utc,
    )
    if dispatch_job is None:
        scheduler.add_job(
            _run_scraper_dispatch,
            "interval",
            seconds=dispatch_interval,
            args=[scheduler, items_queue],
            id=_SCRAPER_DISPATCH_JOB_ID,
            name="central Vinted query dispatcher",
            next_run_time=next_run,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=max(30, dispatch_interval * 3),
        )
        return 1
    if _job_interval_seconds(dispatch_job) != dispatch_interval:
        scheduler.reschedule_job(
            _SCRAPER_DISPATCH_JOB_ID,
            trigger="interval",
            seconds=dispatch_interval,
        )
        scheduler.modify_job(
            _SCRAPER_DISPATCH_JOB_ID,
            next_run_time=next_run,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=max(30, dispatch_interval * 3),
        )
        return 1
    return 0


def _reconcile_scraper_jobs(scheduler, items_queue, now=None):
    """Refresh one bounded central plan from current query preferences."""
    import core

    old_plan = getattr(scheduler, _SCRAPER_PLAN_ATTRIBUTE, None)
    try:
        active_queries = db.get_queries(enabled_only=True, raise_errors=True)
    except Exception:
        logger.error(
            "Could not read active queries; keeping the last good scraper plan.",
            exc_info=True,
        )
        spacing = (old_plan or {}).get(
            "request_spacing_seconds",
            _scraper_request_spacing_seconds(),
        )
        result = _empty_reconcile_result(
            old_plan,
            active=len((old_plan or {}).get("queries", {})),
        )
        result["changed"] = _ensure_scraper_dispatch_job(
            scheduler, items_queue, spacing, now=now
        )
        return result

    query_ids = [int(query[0]) for query in active_queries]
    preferences = _get_query_preferences(query_ids)
    if preferences is None:
        logger.warning(
            "Could not reconcile the scraper plan; keeping the last good plan."
        )
        spacing = (old_plan or {}).get(
            "request_spacing_seconds",
            _scraper_request_spacing_seconds(),
        )
        result = _empty_reconcile_result(old_plan, active=len(query_ids))
        result["changed"] = _ensure_scraper_dispatch_job(
            scheduler, items_queue, spacing, now=now
        )
        return result

    core.record_scraper_heartbeat()
    requested_normal, requested_fast = _scrape_intervals()
    request_spacing = _scraper_request_spacing_seconds()
    modes = {
        query_id: _query_poll_mode(_query_preference(preferences, query_id))
        for query_id in query_ids
    }
    fast_count = sum(mode == "fast" for mode in modes.values())
    cadence = scraper_rate.build_cadence_plan(
        normal_count=len(query_ids) - fast_count,
        fast_count=fast_count,
        requested_normal_seconds=requested_normal,
        requested_fast_seconds=requested_fast,
        request_spacing_seconds=request_spacing,
    )
    signature = (
        tuple(
            sorted(
                (
                    query_id,
                    modes[query_id],
                    bool(
                        _query_preference(preferences, query_id).get(
                            "monitor_during_quiet_hours", False
                        )
                    ),
                )
                for query_id in query_ids
            )
        ),
        cadence["requested_normal_seconds"],
        cadence["requested_fast_seconds"],
        cadence["effective_normal_seconds"],
        cadence["effective_fast_seconds"],
        cadence["request_spacing_seconds"],
    )
    changed = 0
    now_timestamp = _timestamp(now)

    if old_plan is None or old_plan.get("signature") != signature:
        old_entries = (old_plan or {}).get("queries", {})
        entries = {}
        for query_id in query_ids:
            mode = modes[query_id]
            monitor = bool(
                _query_preference(preferences, query_id).get(
                    "monitor_during_quiet_hours", False
                )
            )
            interval = (
                cadence["effective_fast_seconds"]
                if mode == "fast"
                else cadence["effective_normal_seconds"]
            )
            old_entry = old_entries.get(query_id)
            if _plan_entry_changed(old_entry, mode, monitor, interval):
                changed += 1

            last_started = old_entry.get("last_started") if old_entry else None
            if last_started is None:
                next_due = (
                    old_entry.get("next_due", now_timestamp)
                    if old_entry
                    else now_timestamp
                )
            elif old_entry.get("interval") != interval:
                next_due = max(now_timestamp, last_started + interval)
            else:
                next_due = old_entry.get("next_due", now_timestamp)

            entries[query_id] = {
                "mode": mode,
                "monitor_during_quiet_hours": monitor,
                "interval": interval,
                "last_started": last_started,
                "next_due": next_due,
            }

        removed = len(set(old_entries) - set(entries))
        changed += removed
        if not changed:
            changed = 1
        plan = {
            **cadence,
            "signature": signature,
            "queries": entries,
            "fast_streak": (old_plan or {}).get("fast_streak", 0),
        }
        setattr(scheduler, _SCRAPER_PLAN_ATTRIBUTE, plan)
        old_plan = plan
        logger.info(
            "Scraper rate plan: %s Fast at %ss effective (%ss requested), "
            "%s Normal at %ss effective (%ss requested); one central slot "
            "every %.0fs, at most %.1f scheduled request(s)/minute.",
            cadence["fast_count"],
            cadence["effective_fast_seconds"],
            cadence["requested_fast_seconds"],
            cadence["normal_count"],
            cadence["effective_normal_seconds"],
            cadence["requested_normal_seconds"],
            cadence["request_spacing_seconds"],
            cadence["scheduled_requests_per_minute"],
        )

    # Remove any legacy per-query jobs if a live development process reloads
    # this code. Production starts a fresh scheduler, so this is normally zero.
    for job in scheduler.get_jobs():
        if job.id.startswith(_SCRAPER_JOB_PREFIX):
            scheduler.remove_job(job.id)
            changed += 1

    changed += _ensure_scraper_dispatch_job(
        scheduler, items_queue, old_plan["request_spacing_seconds"], now=now
    )

    return {
        "active": len(query_ids),
        "normal_interval": old_plan["effective_normal_seconds"],
        "fast_interval": old_plan["effective_fast_seconds"],
        "request_spacing": old_plan["request_spacing_seconds"],
        "changed": changed,
        "reconciled": True,
    }


def _dispatch_due_query(scheduler, items_queue, now=None):
    """Run at most one due query; overdue work is one coalesced state marker."""
    import core

    plan = getattr(scheduler, _SCRAPER_PLAN_ATTRIBUTE, None)
    if not plan or not plan.get("queries"):
        return None

    now_timestamp = _timestamp(now)
    cooldown = core.get_scraper_cooldown(now=int(now_timestamp))
    if cooldown["active"]:
        return None

    quiet_active = core._quiet_hours_active()
    entries = plan["queries"]
    for entry in entries.values():
        if (
            quiet_active
            and not entry["monitor_during_quiet_hours"]
            and entry["next_due"] <= now_timestamp
        ):
            entry["next_due"] = now_timestamp + entry["interval"]

    due_fast = sorted(
        (
            (entry["next_due"], query_id, entry)
            for query_id, entry in entries.items()
            if entry["mode"] == "fast" and entry["next_due"] <= now_timestamp
        ),
        key=lambda value: (value[0], value[1]),
    )
    due_normal = sorted(
        (
            (entry["next_due"], query_id, entry)
            for query_id, entry in entries.items()
            if entry["mode"] == "normal" and entry["next_due"] <= now_timestamp
        ),
        key=lambda value: (value[0], value[1]),
    )
    if not due_fast and not due_normal:
        return None

    if due_fast and (
        not due_normal
        or plan.get("fast_streak", 0) < scraper_rate.MAX_CONSECUTIVE_FAST_DISPATCHES
    ):
        _due, query_id, entry = due_fast[0]
        plan["fast_streak"] = plan.get("fast_streak", 0) + 1
    else:
        _due, query_id, entry = due_normal[0]
        plan["fast_streak"] = 0

    started_at = now_timestamp if now is not None else time.time()
    # Prevent an accidental re-entrant dispatch from selecting this marker.
    entry["next_due"] = float("inf")
    try:
        core.process_items(
            items_queue,
            query_ids=[query_id],
            monitor_during_quiet_hours=entry["monitor_during_quiet_hours"],
        )
    finally:
        entry["last_started"] = started_at
        entry["next_due"] = started_at + entry["interval"]
    return query_id


def _run_scraper_dispatch(scheduler, items_queue):
    """Reconcile live state, then consume at most one shared request slot."""
    result = _reconcile_scraper_jobs(scheduler, items_queue)
    if not result.get("reconciled"):
        return None
    return _dispatch_due_query(scheduler, items_queue)


def scraper_process(
    items_queue,
    request_gate_lock=None,
    request_next_allowed=None,
    request_lease_until=None,
    request_owner_counter=None,
    request_current_owner=None,
):
    logger.info("Scrape process started")
    _configure_vinted_request_gate(
        request_gate_lock,
        request_next_allowed,
        request_lease_until,
        request_owner_counter,
        request_current_owner,
    )

    normal_interval, fast_interval = _scrape_intervals()
    logger.info(
        "Configured query cadence: normal=%ss, fast=%ss; aggregate load will "
        "be fitted to the shared catalogue budget.",
        normal_interval,
        fast_interval,
    )

    # pyVintedVN owns one module-global Requester/session. A single executor
    # thread plus one central job prevents per-query APScheduler backlogs.
    scraper_scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        timezone=timezone.utc,
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    _reconcile_scraper_jobs(scraper_scheduler, items_queue)
    scraper_scheduler.start()
    try:
        # Keep the process running
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scraper_scheduler.shutdown()
        logger.info("Scrape process stopped")


def item_extractor(
    items_queue,
    new_items_queue,
    request_gate_lock=None,
    request_next_allowed=None,
    request_lease_until=None,
    request_owner_counter=None,
    request_current_owner=None,
):
    import core

    logger.info("Item extractor process started")
    _configure_vinted_request_gate(
        request_gate_lock,
        request_next_allowed,
        request_lease_until,
        request_owner_counter,
        request_current_owner,
    )
    try:
        while True:
            # Check if there's an item in the queue
            core.clear_item_queue(items_queue, new_items_queue)
            time.sleep(0.1)  # Small sleep to prevent high CPU usage
    except (KeyboardInterrupt, SystemExit):
        logger.info("Consumer process stopped")


def _decode_ai_evaluation_job(row):
    """Normalise the durable DB claim into a named dictionary."""
    if isinstance(row, dict):
        values = {name: row.get(name) for name in _AI_JOB_FIELDS}
    elif hasattr(row, "keys"):
        keys = set(row.keys())
        values = {name: row[name] if name in keys else None for name in _AI_JOB_FIELDS}
    else:
        if not isinstance(row, (tuple, list)) or len(row) != len(_AI_JOB_FIELDS):
            raise ValueError("AI evaluation claim has an unsupported row shape.")
        values = dict(zip(_AI_JOB_FIELDS, row))

    for name in ("job_id", "item_id", "query_id"):
        values[name] = int(values[name])
    values["attempts"] = int(values.get("attempts") or 0)
    return values


def _decode_ai_recipients(value):
    """Return unique saved recipients without guessing on corrupt data."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError("AI evaluation has invalid saved recipients.") from error
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("AI evaluation has unsupported saved recipients.")
    return list(
        dict.fromkeys(
            str(chat_id).strip()
            for chat_id in value
            if chat_id is not None
            and not isinstance(chat_id, bool)
            and str(chat_id).strip()
        )
    )


def _current_ai_recipients(job, candidates=None):
    """Intersect primary-ACKed candidates with approved subscribers.

    The query's enabled flag is intentionally ignored. Pausing affects future
    discoveries, not a verdict already started for an alert the user received.
    """
    saved = _decode_ai_recipients(job.get("chat_ids_json"))
    saved_set = set(saved)
    if candidates is None:
        candidates = saved
    else:
        candidates = [chat_id for chat_id in candidates if chat_id in saved_set]
    state = db.get_query_delivery_state(job["query_id"])
    if state is None:
        raise RuntimeError("Could not read current AI evaluation subscribers.")
    _enabled, approved_subscribers = state
    current = {str(value).strip() for value in approved_subscribers}
    return [chat_id for chat_id in candidates if chat_id in current]


def _reschedule_ai_job(job, attempts, delay, reason):
    """Persist a bounded retry without logging listing data or credentials."""
    next_attempt_at = time.time() + max(1, int(delay))
    saved = db.reschedule_ai_evaluation(
        job["job_id"],
        int(attempts),
        next_attempt_at,
        str(reason)[:300],
    )
    if not saved:
        logger.error(
            "Could not reschedule AI evaluation job %s; its claim lease must "
            "expire before it can be recovered.",
            job["job_id"],
        )
    return bool(saved)


def _drop_ai_job(job, reason):
    if not db.complete_ai_evaluation(job["job_id"]):
        logger.error(
            "Could not remove exhausted AI evaluation job %s.",
            job["job_id"],
        )
        return False
    logger.warning(
        "AI evaluation job %s ended without a verdict: %s",
        job["job_id"],
        str(reason)[:300],
    )
    return True


def _retry_or_drop_ai_job(job, error):
    """Apply the worker's bounded retry policy to one safe, typed failure."""
    retryable = bool(getattr(error, "retryable", True))
    next_attempt = job["attempts"] + 1
    if not retryable or next_attempt >= _AI_EVALUATION_MAX_ATTEMPTS:
        return "dropped" if _drop_ai_job(job, error) else "drop_failed"

    delay = _AI_EVALUATION_BACKOFFS[
        min(next_attempt - 1, len(_AI_EVALUATION_BACKOFFS) - 1)
    ]
    if _reschedule_ai_job(job, next_attempt, delay, error):
        logger.warning(
            "AI evaluation job %s failed (attempt %s/%s); retrying in %ss: %s",
            job["job_id"],
            next_attempt,
            _AI_EVALUATION_MAX_ATTEMPTS,
            delay,
            str(error)[:300],
        )
        return "rescheduled"
    return "reschedule_failed"


def process_ai_evaluation_job(row):
    """Process one claimed job; exposed separately for deterministic tests."""
    import ai_deal_evaluator

    try:
        job = _decode_ai_evaluation_job(row)
    except (TypeError, ValueError) as error:
        logger.error("Discarding an unreadable AI evaluation claim: %s", error)
        return "invalid"

    try:
        original = _decode_ai_recipients(job.get("chat_ids_json"))
        original_set = set(original)
        delivered = [
            chat_id
            for chat_id in _decode_ai_recipients(job.get("delivered_chat_ids_json"))
            if chat_id in original_set
        ]
        handled = set(_decode_ai_recipients(job.get("handled_chat_ids_json")))
        ready = [chat_id for chat_id in delivered if chat_id not in handled]

        parent_pending = db.is_notification_pending(job.get("parent_notification_id"))
        if parent_pending is None:
            return (
                "waiting"
                if _reschedule_ai_job(
                    job,
                    job["attempts"],
                    _AI_PARENT_POLL_SECONDS,
                    "Could not verify the primary notification state.",
                )
                else "reschedule_failed"
            )

        evaluation_started = bool(
            job.get("evaluation_started_at") or job.get("result_content")
        )
        if not evaluation_started:
            state = db.get_query_delivery_state(job["query_id"])
            if state is None:
                return (
                    "waiting"
                    if _reschedule_ai_job(
                        job,
                        job["attempts"],
                        _AI_PARENT_POLL_SECONDS,
                        "Could not verify whether the query is paused.",
                    )
                    else "reschedule_failed"
                )
            enabled, _subscribers = state
            if not enabled:
                return (
                    "cancelled"
                    if db.complete_ai_evaluation(job["job_id"])
                    else "complete_failed"
                )

        if not ready:
            if not parent_pending:
                # The claim snapshot can be stale: Telegram may ACK the final
                # primary recipient after the claim but before this read. Let
                # the DB transaction re-read delivered_chat_ids and retain the
                # job when a fresh, unhandled ACK exists.
                settled = db.settle_ai_evaluation_recipients(
                    job["job_id"],
                    handled_chat_ids=[],
                    eligible_chat_ids=[],
                )
                return "parent_finished" if settled else "settle_failed"
            return (
                "waiting"
                if _reschedule_ai_job(
                    job,
                    job["attempts"],
                    _AI_PARENT_POLL_SECONDS,
                    "Waiting for a primary Telegram acknowledgement.",
                )
                else "reschedule_failed"
            )

        # A delivered user may unsubscribe before the evaluator gets to this
        # job. Mark that ACK as handled without spending an API call; a sibling
        # who succeeds later can still trigger the evaluation.
        recipients = _current_ai_recipients(job, ready)
        if not recipients:
            settled = db.settle_ai_evaluation_recipients(
                job["job_id"],
                handled_chat_ids=ready,
                eligible_chat_ids=[],
            )
            return "no_recipients" if settled else "settle_failed"

        content = job.get("result_content")
        if not content:
            start_state = db.begin_ai_evaluation(job["job_id"])
            if start_state == "cancelled":
                return "cancelled"
            if start_state == "missing":
                return "missing"
            if start_state != "started":
                return (
                    "waiting"
                    if _reschedule_ai_job(
                        job,
                        job["attempts"],
                        _AI_PARENT_POLL_SECONDS,
                        "Could not establish AI evaluation start state.",
                    )
                    else "reschedule_failed"
                )

            rating = ai_deal_evaluator.evaluate(
                {
                    "url": job.get("item_url"),
                    "brand_title": job.get("brand"),
                    "title": job.get("title"),
                    "condition": job.get("condition"),
                    "price": job.get("price"),
                    "currency": job.get("currency"),
                    "photo": job.get("photo_url"),
                }
            )
            title_text = str(job.get("title") or "")
            if len(title_text) > 120:
                title_text = title_text[:119].rstrip() + "…"
            content = f"🤖 <b>AI check</b> — {html.escape(title_text)}\n{rating}"

        # Recheck after a potentially slow API call. Pausing is intentionally
        # ignored now that evaluation has started, while unsubscribe/revocation
        # still removes a recipient. The outbox repeats that subscriber check
        # immediately before Telegram delivery.
        recipients = _current_ai_recipients(job, ready)
        settled = db.settle_ai_evaluation_recipients(
            job["job_id"],
            handled_chat_ids=ready,
            eligible_chat_ids=recipients,
            result_content=content,
        )
        if not settled:
            raise RuntimeError("Could not atomically publish the AI follow-up.")
        return "completed" if recipients else "no_recipients"
    except ai_deal_evaluator.AIDealEvaluationError as error:
        return _retry_or_drop_ai_job(job, error)
    except (TypeError, ValueError) as error:
        # Corrupt persisted input will not improve on retry.
        permanent = ai_deal_evaluator.AIPermanentError(str(error))
        return _retry_or_drop_ai_job(job, permanent)
    except Exception as error:
        # Keep the worker alive and recover from transient DB/integration faults
        # without dumping item contents or API credentials into the log.
        transient = ai_deal_evaluator.AITransientError(
            f"AI worker dependency failed: {type(error).__name__}."
        )
        logger.error(
            "AI evaluation job %s hit an unexpected worker error.",
            job["job_id"],
            exc_info=True,
        )
        return _retry_or_drop_ai_job(job, transient)


def ai_evaluator_process():
    """Serialized durable worker for OpenAI deal-evaluation follow-ups."""
    logger.info("AI deal evaluator process started")
    try:
        while True:
            try:
                row = db.claim_due_ai_evaluation()
            except Exception:
                logger.error("Could not claim an AI evaluation job.", exc_info=True)
                time.sleep(_AI_WORKER_IDLE_SECONDS)
                continue
            if row is None:
                time.sleep(_AI_WORKER_IDLE_SECONDS)
                continue
            process_ai_evaluation_job(row)
    except (KeyboardInterrupt, SystemExit):
        logger.info("AI deal evaluator process stopped")


def dispatcher_function(input_queue, rss_queue, telegram_queue):
    # Telegram delivery now goes through the durable outbox (see
    # core.clear_item_queue -> db.enqueue_notification), so this dispatcher only
    # feeds the ephemeral RSS feed. telegram_queue is retained for signature
    # compatibility but is no longer used for item delivery.
    logger.info("Dispatcher process started")
    try:
        while True:
            # Get from input queue
            item = input_queue.get()
            # Telegram items may include a sixth field containing the
            # destination chat IDs. RSS still expects the original five fields.
            if isinstance(item, tuple) and len(item) == 6:
                rss_queue.put(item[:5])
            else:
                rss_queue.put(item)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Dispatcher process stopped")
    except Exception as e:
        logger.error(f"Error in dispatcher process: {e}", exc_info=True)


def telegram_bot_process(queue, polling_enabled=True):
    mode = "polling" if polling_enabled else "send-only"
    logger.info("Telegram bot process started in %s mode", mode)
    try:
        # Import LeRobot
        from telegram_bot_plugin.telegram_bot import LeRobot

        LeRobot(queue, polling_enabled=polling_enabled)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Telegram bot process stopped")
    except Exception as e:
        logger.error(f"Error in telegram bot process: {e}", exc_info=True)


def rss_feed_process_entry(queue):
    from rss_feed_plugin.rss_feed import rss_feed_process

    rss_feed_process(queue)


def web_ui_process_entry():
    from web_ui_plugin.web_ui import web_ui_process

    web_ui_process()


def check_refresh_delay(items_queue):
    """Observe delay changes; the scraper reconciles jobs without a restart."""
    global current_query_refresh_delay

    # Check if the scheduler is running

    if scrape_process is None or not scrape_process.is_alive():
        return

    # Get the current value from the database
    try:
        new_delay = int(db.get_parameter("query_refresh_delay"))

        # The scraper child rereads both Normal and Fast intervals during its
        # ten-second reconciliation. Do not terminate it here: doing so used to
        # create a restart burst and could interrupt an in-flight query.
        if new_delay != current_query_refresh_delay:
            logger.info(
                "Query refresh delay changed from %s to %s seconds; the "
                "per-query scheduler will reconcile it without restarting.",
                current_query_refresh_delay,
                new_delay,
            )
            current_query_refresh_delay = new_delay
    except Exception as e:
        logger.error(f"Error updating refresh delay: {e}", exc_info=True)


def ensure_scrape_process_alive(items_queue):
    """Restart the scrape process if it has died, so scraping self-heals."""
    global scrape_process

    if scrape_process is not None and scrape_process.is_alive():
        return

    logger.error("Scrape process is not running; restarting it.")
    scrape_process = _create_process(
        target=scraper_process,
        args=(
            items_queue,
            vinted_request_gate_lock,
            vinted_request_next_allowed,
            vinted_request_lease_until,
            vinted_request_owner_counter,
            vinted_request_current_owner,
        ),
    )
    scrape_process.start()


def ensure_item_extractor_process_alive(items_queue, new_items_queue):
    """Restart the item extractor if it has died, preserving Vinted pacing."""
    global item_extractor_process

    if item_extractor_process is not None and item_extractor_process.is_alive():
        return

    logger.error("Item extractor process is not running; restarting it.")
    item_extractor_process = _create_process(
        target=item_extractor,
        args=(
            items_queue,
            new_items_queue,
            vinted_request_gate_lock,
            vinted_request_next_allowed,
            vinted_request_lease_until,
            vinted_request_owner_counter,
            vinted_request_current_owner,
        ),
    )
    item_extractor_process.start()


def ensure_ai_evaluator_process_alive():
    """Restart the durable AI worker if its child process has died."""
    global ai_process

    if ai_process is not None and ai_process.is_alive():
        return

    logger.error("AI deal evaluator process is not running; restarting it.")
    ai_process = _create_process(target=ai_evaluator_process)
    ai_process.start()


def check_scraper_watchdog():
    """Alert the admin once when the scraper stalls or is blocked, and once
    again when it recovers.

    The alert state is persisted in the database, so a sustained problem
    produces a single notification rather than one on every monitor tick.
    """
    try:
        import core

        health = core.get_scraper_health()
        problem = bool(health["stalled"] or health["blocked"])
        already_alerted = db.get_parameter("scraper_watchdog_alerted") == "True"

        now = int(time.time())
        try:
            recovery_started = int(
                db.get_parameter("scraper_watchdog_recovery_started") or 0
            )
        except (TypeError, ValueError):
            recovery_started = 0

        admin_chat_id = db.get_parameter("telegram_chat_id")
        telegram_enabled = db.get_parameter("telegram_enabled") == "True"

        if problem:
            if recovery_started:
                db.set_parameter("scraper_watchdog_recovery_started", "0")
            if already_alerted:
                return
            if health["stalled"]:
                reason = "has stalled with no recent scrape activity"
            elif health["cooldown_active"]:
                minutes = max(
                    1,
                    (health["cooldown_remaining"] + 59) // 60,
                )
                reason = (
                    "paused itself after Vinted returned HTTP "
                    f"{health['last_block_status'] or 403}; retrying in "
                    f"about {minutes} minute(s)"
                )
            elif health["cooldown_level"] > 0:
                reason = (
                    "is waiting for a successful Vinted scrape after HTTP "
                    f"{health['last_block_status'] or 403}"
                )
            else:
                reason = (
                    "appears to be blocked by Vinted "
                    f"({health['failed_cycles']} consecutive cycles found nothing)"
                )
            logger.error("Scraper watchdog: the scraper %s.", reason)
            content = (
                "⚠️ Vinted Notifications: the scraper "
                f"{reason}. It will keep retrying automatically."
            )
            db.set_parameter("scraper_watchdog_alerted", "True")
        else:
            if not already_alerted:
                if recovery_started:
                    db.set_parameter("scraper_watchdog_recovery_started", "0")
                return

            # A single successful request between two blocks used to emit a
            # recovery followed by another warning. Keep the existing alert
            # latched until the scraper has stayed healthy for a meaningful
            # period, which suppresses that noisy alert flapping.
            if not recovery_started:
                db.set_parameter(
                    "scraper_watchdog_recovery_started",
                    str(now),
                )
                logger.info(
                    "Scraper watchdog: recovery detected; waiting for stable "
                    "operation before clearing the alert."
                )
                return

            if now - recovery_started < _watchdog_recovery_window_seconds():
                return

            logger.info("Scraper watchdog: the scraper has recovered.")
            content = (
                "✅ Vinted Notifications: the scraper has recovered "
                "and is running normally again."
            )
            db.set_parameter("scraper_watchdog_alerted", "False")
            db.set_parameter("scraper_watchdog_recovery_started", "0")

        if telegram_enabled and admin_chat_id:
            db.enqueue_notification(content, None, None, [admin_chat_id])
    except Exception:
        logger.error("Error in scraper watchdog.", exc_info=True)


def monitor_processes(items_queue, new_items_queue, telegram_queue, rss_queue):
    global telegram_process, rss_process

    # Restart the scrape process if it has died, then apply any delay change.
    ensure_scrape_process_alive(items_queue)
    ensure_item_extractor_process_alive(items_queue, new_items_queue)
    ensure_ai_evaluator_process_alive()
    check_refresh_delay(items_queue)

    ### TELEGRAM ###
    # Check telegram process status
    telegram_should_run = db.get_parameter("telegram_process_running") == "True"
    # Check if the telegram token and chat ID are set
    telegram_token = db.get_parameter("telegram_token")
    telegram_chat_id = db.get_parameter("telegram_chat_id")
    if not telegram_token or not telegram_chat_id:
        telegram_should_run = False
    telegram_is_running = telegram_process is not None and telegram_process.is_alive()

    if telegram_should_run and not telegram_is_running:
        # Start telegram process
        polling_enabled = telegram_polling_enabled()
        mode = "polling" if polling_enabled else "send-only"
        logger.info("Starting Telegram process in %s mode.", mode)
        telegram_process = _create_process(
            target=telegram_bot_process,
            args=(telegram_queue, polling_enabled),
        )
        telegram_process.start()
    elif not telegram_should_run and telegram_is_running:
        # Stop telegram process
        logger.info("Stopping telegram bot process.")
        telegram_process.terminate()
        telegram_process.join()
        telegram_process = None

    ### RSS ###
    # Check RSS process status
    rss_should_run = db.get_parameter("rss_process_running") == "True"
    rss_is_running = rss_process is not None and rss_process.is_alive()

    if rss_should_run and not rss_is_running:
        # Start RSS process
        logger.info("Starting RSS process based on database status")
        rss_process = _create_process(target=rss_feed_process_entry, args=(rss_queue,))
        rss_process.start()
    elif not rss_should_run and rss_is_running:
        # Stop RSS process
        logger.info("Stopping RSS process based on database status")
        rss_process.terminate()
        rss_process.join()
        rss_process = None

    ### SCRAPER WATCHDOG ###
    check_scraper_watchdog()


def reset_scraper_watchdog_baseline(now=None):
    """Start each process lifetime with a fresh watchdog baseline.

    The heartbeat and failure counter live in SQLite, so without this reset a
    clean restart can inherit a stale heartbeat or failures from the previous
    process and immediately emit a false stalled/blocked alert before the first
    scheduled scrape. The last successful-cycle timestamp is intentionally
    preserved for health reporting.
    """
    baseline = int(now if now is not None else time.time())
    if not db.set_parameters(
        {
            "scraper_heartbeat": str(baseline),
            "scraper_failed_cycles": "0",
            "scraper_watchdog_recovery_started": "0",
        }
    ):
        raise RuntimeError("Failed to reset the scraper watchdog baseline.")
    logger.info("Scraper watchdog baseline reset at %s.", baseline)


def plugin_checker():
    # Get telegram and rss enable status
    telegram_enabled = db.get_parameter("telegram_enabled")
    logger.info("Telegram enabled: {}".format(telegram_enabled))
    rss_enabled = db.get_parameter("rss_enabled")
    logger.info("RSS enabled: {}".format(rss_enabled))

    # Reset process status at startup
    db.set_parameter("telegram_process_running", telegram_enabled)
    db.set_parameter("rss_process_running", rss_enabled)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app_logging_queue = start_logging_listener()
    initialise_database()
    reset_scraper_watchdog_baseline()

    # Plugin checker
    plugin_checker()

    # All application processes that contact Vinted share this conservative
    # start/completion gate. The value uses the system-wide monotonic clock.
    vinted_request_gate_lock = multiprocessing.Lock()
    vinted_request_next_allowed = multiprocessing.Value("d", 0.0, lock=False)
    vinted_request_lease_until = multiprocessing.Value("d", 0.0, lock=False)
    vinted_request_owner_counter = multiprocessing.Value("Q", 0, lock=False)
    vinted_request_current_owner = multiprocessing.Value("Q", 0, lock=False)

    # Create a shared queue
    items_queue = multiprocessing.Queue()
    new_items_queue = multiprocessing.Queue()
    rss_queue = multiprocessing.Queue()
    telegram_queue = multiprocessing.Queue()

    # 1. Create and start the scrape process
    # This process will scrape items and put them in the items_queue
    current_query_refresh_delay = int(db.get_parameter("query_refresh_delay"))
    scrape_process = _create_process(
        target=scraper_process,
        args=(
            items_queue,
            vinted_request_gate_lock,
            vinted_request_next_allowed,
            vinted_request_lease_until,
            vinted_request_owner_counter,
            vinted_request_current_owner,
        ),
    )
    scrape_process.start()

    # 2. Create the item extractor process
    # This process will extract items from the items_queue and put them in the new_items_queue
    item_extractor_process = _create_process(
        target=item_extractor,
        args=(
            items_queue,
            new_items_queue,
            vinted_request_gate_lock,
            vinted_request_next_allowed,
            vinted_request_lease_until,
            vinted_request_owner_counter,
            vinted_request_current_owner,
        ),
    )
    item_extractor_process.start()

    # 3. Create the durable, serialized AI evaluator process. It is always
    # available, but remains idle unless an AI-enabled query produces a job.
    ai_process = _create_process(target=ai_evaluator_process)
    ai_process.start()

    # 4. Create the dispatcher process
    # This process will handle the new items and send them to the enabled services
    dispatcher_process = _create_process(
        target=dispatcher_function,
        args=(
            new_items_queue,
            rss_queue,
            telegram_queue,
        ),
    )
    dispatcher_process.start()

    # 5. Set up a scheduler to monitor processes
    # This will check the process status in the database and start/stop processes as needed
    monitor_scheduler = BackgroundScheduler()
    monitor_scheduler.add_job(
        monitor_processes,
        "interval",
        seconds=5,
        args=[items_queue, new_items_queue, telegram_queue, rss_queue],
        name="process_monitor",
    )
    monitor_scheduler.start()

    # 6. Create and start the Web UI process
    # This process will provide a web interface to control the application
    web_ui_process_instance = _create_process(target=web_ui_process_entry)
    web_ui_process_instance.start()

    try:
        # Wait for processes to finish (which they won't unless interrupted)
        scrape_process.join()
        item_extractor_process.join()
        ai_process.join()
        dispatcher_process.join()
        web_ui_process_instance.join()

        # plugins
        if telegram_process:
            telegram_process.join()
        if rss_process:
            rss_process.join()
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        logger.info("Main process interrupted")

        # Shutdown the monitor scheduler
        monitor_scheduler.shutdown()

        # Terminate all processes
        scrape_process.terminate()
        item_extractor_process.terminate()
        if ai_process and ai_process.is_alive():
            ai_process.terminate()
        dispatcher_process.terminate()
        # Terminate web UI process
        web_ui_process_instance.terminate()

        # Plugins

        if telegram_process and telegram_process.is_alive():
            telegram_process.terminate()
            # Set the process status in the database
            db.set_parameter("telegram_process_running", "False")
        if rss_process and rss_process.is_alive():
            rss_process.terminate()
            # Set the process status in the database
            db.set_parameter("rss_process_running", "False")

        # Wait for all processes to terminate
        scrape_process.join()
        item_extractor_process.join()
        if ai_process:
            ai_process.join()
        dispatcher_process.join()
        web_ui_process_instance.join()

        # Plugins
        if telegram_process:
            telegram_process.join()
        if rss_process:
            rss_process.join()

        logger.info("All processes terminated")
        stop_logging_listener()
