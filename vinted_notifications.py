import multiprocessing
import time
import os
import sys
from datetime import datetime, timedelta, timezone
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
from apscheduler.executors.pool import ThreadPoolExecutor  # noqa: E402
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from logger import get_logger  # noqa: E402

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
current_query_refresh_delay = None

_SCRAPER_RECONCILE_SECONDS = 10
_SCRAPER_JOB_PREFIX = "scrape_query_"
_SCRAPER_RECONCILE_JOB_ID = "scraper_reconcile"


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
        (db.migrate_query_enabled_column, "per-query pause/enable"),
        (db.migrate_multi_user_schema, "multi-user Telegram support"),
        (db.migrate_query_uniqueness, "query uniqueness"),
        (db.migrate_quiet_hours_schema, "quiet-hours configuration"),
        (db.migrate_query_preferences_schema, "per-query monitoring preferences"),
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
    normal = _scrape_delay("query_refresh_delay", 300)
    configured_fast = _scrape_delay("fast_query_refresh_delay", 90, minimum=60)
    return normal, min(normal, configured_fast)


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


def _query_job_id(query_id):
    return f"{_SCRAPER_JOB_PREFIX}{int(query_id)}"


def _deterministic_next_run(query_id, interval_seconds, now=None):
    """Place a query in a stable slot instead of bursting every query at boot."""
    interval_seconds = max(1, int(interval_seconds))
    if now is None:
        now_timestamp = time.time()
    elif isinstance(now, datetime):
        now_timestamp = now.timestamp()
    else:
        now_timestamp = float(now)

    # 97 is coprime with the normal 180s and fast 90s defaults. Sequential
    # SQLite IDs are therefore distributed over those intervals rather than
    # clustering at process start. The same query keeps the same phase after a
    # restart, which also avoids cadence churn.
    phase = (int(query_id) * 97) % interval_seconds
    cycle_start = int(now_timestamp // interval_seconds) * interval_seconds
    next_timestamp = cycle_start + phase
    if next_timestamp <= now_timestamp:
        next_timestamp += interval_seconds
    return datetime.fromtimestamp(next_timestamp, timezone.utc)


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


def _run_scheduled_query(items_queue, query_id):
    """Run one query on the scheduler's sole worker thread."""
    import core

    preference = _query_preference(_get_query_preferences([query_id]), query_id)
    core.process_items(
        items_queue,
        query_ids=[query_id],
        monitor_during_quiet_hours=bool(
            preference.get("monitor_during_quiet_hours", False)
        ),
    )


def _job_interval_seconds(job):
    interval = getattr(getattr(job, "trigger", None), "interval", None)
    if interval is None:
        return None
    return int(interval.total_seconds())


def _reconcile_scraper_jobs(scheduler, items_queue, now=None):
    """Synchronise serialized per-query jobs with the latest database state."""
    import core

    # The reconciliation heartbeat proves the process is healthy even when no
    # queries are enabled or every ordinary query is intentionally quiet.
    core.record_scraper_heartbeat()

    active_queries = db.get_queries(enabled_only=True)
    query_ids = [int(query[0]) for query in active_queries]
    preferences = _get_query_preferences(query_ids)
    if preferences is None:
        # A transient preferences read must not silently downgrade Fast jobs or
        # churn every trigger. Existing jobs retain their last known schedule;
        # the next reconciliation retries the read.
        logger.warning(
            "Could not reconcile per-query schedules; keeping existing jobs."
        )
        return {
            "active": len(query_ids),
            "normal_interval": None,
            "fast_interval": None,
            "changed": 0,
        }
    normal_interval, fast_interval = _scrape_intervals()
    desired_job_ids = {_query_job_id(query_id) for query_id in query_ids}
    changes = 0

    for job in scheduler.get_jobs():
        if job.id.startswith(_SCRAPER_JOB_PREFIX) and job.id not in desired_job_ids:
            scheduler.remove_job(job.id)
            changes += 1

    for query_id in query_ids:
        preference = _query_preference(preferences, query_id)
        mode = _query_poll_mode(preference)
        interval = fast_interval if mode == "fast" else normal_interval
        job_id = _query_job_id(query_id)
        existing = scheduler.get_job(job_id)
        next_run = _deterministic_next_run(query_id, interval, now=now)

        if existing is None:
            scheduler.add_job(
                _run_scheduled_query,
                "interval",
                seconds=interval,
                args=[items_queue, query_id],
                id=job_id,
                name=f"Vinted query {query_id} ({mode})",
                next_run_time=next_run,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=max(30, interval),
            )
            changes += 1
        elif _job_interval_seconds(existing) != interval:
            scheduler.reschedule_job(
                job_id,
                trigger="interval",
                seconds=interval,
                next_run_time=next_run,
            )
            scheduler.modify_job(
                job_id,
                name=f"Vinted query {query_id} ({mode})",
                coalesce=True,
                max_instances=1,
                misfire_grace_time=max(30, interval),
            )
            changes += 1

    if changes:
        fast_count = sum(
            1
            for query_id in query_ids
            if _query_poll_mode(_query_preference(preferences, query_id)) == "fast"
        )
        logger.info(
            "Reconciled %s active query schedules (%s fast at %ss, %s normal "
            "at %ss); %s job(s) changed.",
            len(query_ids),
            fast_count,
            fast_interval,
            len(query_ids) - fast_count,
            normal_interval,
            changes,
        )

    return {
        "active": len(query_ids),
        "normal_interval": normal_interval,
        "fast_interval": fast_interval,
        "changed": changes,
    }


def scraper_process(items_queue):
    logger.info("Scrape process started")

    normal_interval, fast_interval = _scrape_intervals()
    logger.info(
        "Using serialized per-query scheduling: normal=%ss, fast=%ss.",
        normal_interval,
        fast_interval,
    )

    # pyVintedVN owns one module-global Requester/session. A single executor
    # thread is therefore intentional: Fast searches gain cadence through
    # staggered due times, never through concurrent Vinted requests.
    scraper_scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        timezone=timezone.utc,
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    _reconcile_scraper_jobs(scraper_scheduler, items_queue)
    scraper_scheduler.add_job(
        _reconcile_scraper_jobs,
        "interval",
        seconds=_SCRAPER_RECONCILE_SECONDS,
        args=[scraper_scheduler, items_queue],
        id=_SCRAPER_RECONCILE_JOB_ID,
        name="scraper schedule reconciliation",
        next_run_time=datetime.now(timezone.utc)
        + timedelta(seconds=_SCRAPER_RECONCILE_SECONDS),
        coalesce=True,
        max_instances=1,
    )
    scraper_scheduler.start()
    try:
        # Keep the process running
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scraper_scheduler.shutdown()
        logger.info("Scrape process stopped")


def item_extractor(items_queue, new_items_queue):
    import core

    logger.info("Item extractor process started")
    try:
        while True:
            # Check if there's an item in the queue
            core.clear_item_queue(items_queue, new_items_queue)
            time.sleep(0.1)  # Small sleep to prevent high CPU usage
    except (KeyboardInterrupt, SystemExit):
        logger.info("Consumer process stopped")


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
    scrape_process = multiprocessing.Process(
        target=scraper_process, args=(items_queue,)
    )
    scrape_process.start()


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


def monitor_processes(items_queue, telegram_queue, rss_queue):
    global telegram_process, rss_process

    # Restart the scrape process if it has died, then apply any delay change.
    ensure_scrape_process_alive(items_queue)
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
        telegram_process = multiprocessing.Process(
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
        rss_process = multiprocessing.Process(
            target=rss_feed_process_entry, args=(rss_queue,)
        )
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
    initialise_database()
    reset_scraper_watchdog_baseline()

    # Plugin checker
    plugin_checker()

    # Create a shared queue
    items_queue = multiprocessing.Queue()
    new_items_queue = multiprocessing.Queue()
    rss_queue = multiprocessing.Queue()
    telegram_queue = multiprocessing.Queue()

    # 1. Create and start the scrape process
    # This process will scrape items and put them in the items_queue
    current_query_refresh_delay = int(db.get_parameter("query_refresh_delay"))
    scrape_process = multiprocessing.Process(
        target=scraper_process, args=(items_queue,)
    )
    scrape_process.start()

    # 2. Create the item extractor process
    # This process will extract items from the items_queue and put them in the new_items_queue
    item_extractor_process = multiprocessing.Process(
        target=item_extractor, args=(items_queue, new_items_queue)
    )
    item_extractor_process.start()

    # 3. Create the dispatcher process
    # This process will handle the new items and send them to the enabled services
    dispatcher_process = multiprocessing.Process(
        target=dispatcher_function,
        args=(
            new_items_queue,
            rss_queue,
            telegram_queue,
        ),
    )
    dispatcher_process.start()

    # 4. Set up a scheduler to monitor processes
    # This will check the process status in the database and start/stop processes as needed
    monitor_scheduler = BackgroundScheduler()
    monitor_scheduler.add_job(
        monitor_processes,
        "interval",
        seconds=5,
        args=[items_queue, telegram_queue, rss_queue],
        name="process_monitor",
    )
    monitor_scheduler.start()

    # 5. Create and start the Web UI process
    # This process will provide a web interface to control the application
    web_ui_process_instance = multiprocessing.Process(target=web_ui_process_entry)
    web_ui_process_instance.start()

    try:
        # Wait for processes to finish (which they won't unless interrupted)
        scrape_process.join()
        item_extractor_process.join()
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
        dispatcher_process.join()
        web_ui_process_instance.join()

        # Plugins
        if telegram_process:
            telegram_process.join()
        if rss_process:
            rss_process.join()

        logger.info("All processes terminated")
