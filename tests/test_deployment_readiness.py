import base64
import queue
import re
import sqlite3
import sys
from datetime import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db
from url_normalizer import normalise_vinted_url


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.configure_database_runtime()
    yield database_path


def _core():
    import core

    return core


def _basic_header(username="admin", password="correct-horse"):
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


def test_url_normalisation_preserves_repeated_filters():
    result = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?size_ids[]=2&size_ids[]=3&page=9&utm_source=x"
    )
    assert result.count("size_ids%5B%5D=") == 2
    assert "page=" not in result
    assert "utm_source=" not in result
    assert result.endswith("order=newest_first")


def test_quiet_hours_support_a_window_across_midnight(database, monkeypatch):
    core = _core()
    values = {
        "quiet_hours_enabled": "True",
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "06:00",
        "quiet_hours_timezone": "Europe/London",
    }
    monkeypatch.setattr(core.db, "get_parameter", values.get)

    assert core.get_quiet_hours_status(time(23, 30))[0]
    assert core.get_quiet_hours_status(time(5, 59))[0]
    assert not core.get_quiet_hours_status(time(6, 0))[0]


def test_admin_rotation_leaves_exactly_one_administrator(database):
    assert db.set_parameter("telegram_chat_id", "111") is None
    assert db.migrate_multi_user_schema()
    assert db.is_telegram_user_admin("111")

    db.set_parameter("telegram_chat_id", "333")
    assert db.migrate_multi_user_schema()

    users = db.get_telegram_users()
    admins = [row[0] for row in users if int(row[3]) == 1]
    assert admins == ["333"]
    assert db.is_telegram_user_approved("111")
    assert not db.is_telegram_user_admin("111")


def test_shared_query_is_created_once_for_multiple_users(database):
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.migrate_query_uniqueness()
    assert db.approve_telegram_user("222", "Tester")

    url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=coat"
    )
    query_id, created, subscribed = db.add_query_to_db(
        url,
        name="Coats",
        chat_id="111",
    )
    assert created and subscribed

    same_id, created, subscribed = db.add_query_to_db(url, chat_id="222")
    assert same_id == query_id
    assert not created and subscribed
    assert set(db.get_query_subscribers(query_id)) == {"111", "222"}
    assert len(db.get_queries()) == 1


def test_copy_query_subscriptions_is_idempotent(database):
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.approve_telegram_user("222", "Sister")

    first_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=coat"
    )
    second_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=boots"
    )
    first_id, _, _ = db.add_query_to_db(first_url, chat_id="111")
    second_id, _, _ = db.add_query_to_db(second_url, chat_id="111")

    assert db.copy_query_subscriptions("111", "222") == 2
    assert set(db.get_query_subscribers(first_id)) == {"111", "222"}
    assert set(db.get_query_subscribers(second_id)) == {"111", "222"}

    # Running the command again does not create duplicate rows.
    assert db.copy_query_subscriptions("111", "222") == 0
    assert len(db.get_queries()) == 2

    # Pending accounts cannot receive copied subscriptions.
    assert db.register_telegram_user("333", "Pending")
    assert db.copy_query_subscriptions("111", "333") is None


def test_duplicate_migration_preserves_items_and_subscriptions(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            last_item NUMERIC,
            query_name TEXT
        );
        CREATE TABLE items (
            item NUMERIC, title TEXT, price NUMERIC, currency TEXT,
            timestamp NUMERIC, photo_url TEXT, query_id INTEGER,
            FOREIGN KEY (query_id) REFERENCES queries(id)
        );
        CREATE TABLE telegram_users (
            chat_id TEXT PRIMARY KEY, display_name TEXT,
            status TEXT NOT NULL, is_admin INTEGER NOT NULL,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE query_subscriptions (
            query_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(query_id, chat_id),
            FOREIGN KEY(query_id) REFERENCES queries(id) ON DELETE CASCADE,
            FOREIGN KEY(chat_id) REFERENCES telegram_users(chat_id) ON DELETE CASCADE
        );
        INSERT INTO queries(query,last_item,query_name) VALUES ('same',10,NULL);
        INSERT INTO queries(query,last_item,query_name) VALUES ('same',20,'Named');
        INSERT INTO telegram_users(chat_id,status,is_admin) VALUES ('111','approved',1);
        INSERT INTO query_subscriptions(query_id,chat_id) VALUES (2,'111');
        INSERT INTO items(item,title,query_id) VALUES (9,'Item',2);
        """
    )
    conn.commit()
    conn.close()

    assert db.migrate_query_uniqueness()
    conn = db.get_db_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 1
        row = conn.execute(
            "SELECT id,last_item,query_name FROM queries"
        ).fetchone()
        assert row == (1, 20, "Named")
        assert conn.execute("SELECT query_id FROM items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT query_id,chat_id FROM query_subscriptions"
        ).fetchone() == (1, "111")
    finally:
        conn.close()


def test_rss_dispatches_when_no_telegram_subscribers(database, monkeypatch):
    core = _core()

    class Item:
        id = 99
        title = "Coat"
        price = 12
        currency = "GBP"
        brand_title = "Brand"
        condition = "Very good"
        description = "Description"
        photo = None
        url = "https://www.vinted.co.uk/items/99"
        raw_timestamp = 100
        raw_data = {"user": {"id": 1}}

    parameters = {
        "banwords": "",
        "message_template": db.DEFAULT_MESSAGE_TEMPLATE,
    }
    monkeypatch.setattr(core.db, "get_parameter", parameters.get)
    monkeypatch.setattr(core.db, "get_last_timestamp", lambda query_id: None)
    monkeypatch.setattr(core.db, "is_item_in_db_by_id", lambda item_id: False)
    monkeypatch.setattr(core.db, "get_allowlist", lambda: 0)
    monkeypatch.setattr(core.db, "get_query_subscribers", lambda query_id: [])
    monkeypatch.setattr(core.db, "add_item_to_db", lambda **kwargs: None)

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], 1))
    core.clear_item_queue(source, destination)

    dispatched = destination.get_nowait()
    assert len(dispatched) == 6
    assert dispatched[5] == []


def test_item_description_uses_browser_navigation_headers(database, monkeypatch):
    core = _core()
    calls = []

    class Response:
        text = """
        <script type="application/ld+json">
        {"@type": "Product", "description": "A detailed Vinted listing"}
        </script>
        """

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    class Item:
        id = 12345
        url = "https://www.vinted.co.uk/items/12345-example"
        description = None

    monkeypatch.setattr(core.requester.session, "get", fake_get)

    assert core._get_item_description(Item()) == "A detailed Vinted listing"
    assert len(calls) == 1
    assert calls[0][0] == "https://www.vinted.co.uk/items/12345"
    assert calls[0][1]["timeout"] == (5, 10)
    assert calls[0][1]["allow_redirects"] is True
    assert calls[0][1]["headers"]["Sec-Fetch-Dest"] == "document"
    assert calls[0][1]["headers"]["Sec-Fetch-Mode"] == "navigate"
    assert calls[0][1]["headers"]["Referer"] == "https://www.vinted.co.uk/"


def test_version_check_is_cached_and_has_a_timeout(database, monkeypatch):
    core = _core()
    values = {
        "github_url": "https://github.com/FrancesOkolo/Vinted-Notifications",
        "version": "1.2.3",
    }
    monkeypatch.setattr(core.db, "get_parameter", values.get)

    calls = []

    class Response:
        status_code = 200
        url = "https://github.com/FrancesOkolo/Vinted-Notifications/releases/tag/1.2.3"

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(core.requests, "get", fake_get)
    core._VERSION_CACHE = None
    core._VERSION_CACHE_TIME = 0

    assert core.check_version()[0]
    assert core.check_version()[0]
    assert len(calls) == 1
    assert calls[0][1] == (3.05, 5)


def test_version_check_ignores_repo_without_releases(database, monkeypatch):
    core = _core()
    monkeypatch.setattr(
        core.db,
        "get_parameter",
        {"github_url": "https://github.com/x/y", "version": "1.0.5.4"}.get,
    )

    class Response:
        status_code = 200
        # A repo with no releases redirects to …/releases (no /tag/ segment).
        url = "https://github.com/x/y/releases"

    monkeypatch.setattr(core.requests, "get", lambda url, timeout: Response())
    core._VERSION_CACHE = None
    core._VERSION_CACHE_TIME = 0

    is_up_to_date, current, latest, _url = core.check_version()
    assert is_up_to_date is True
    assert current == "1.0.5.4"
    assert latest == "1.0.5.4"  # not the bogus "releases"


def test_web_auth_csrf_health_and_token_masking(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "admin")
    monkeypatch.setattr(web, "WEB_PASSWORD", "correct-horse")
    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (
            True,
            "test",
            "test",
            "https://github.com/FrancesOkolo/Vinted-Notifications",
        ),
    )
    db.set_parameter("telegram_token", "SECRET_TOKEN_MUST_NOT_APPEAR")

    client = web.app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/config").status_code == 401

    headers = _basic_header()
    response = client.get("/config", headers=headers)
    assert response.status_code == 200
    assert b"SECRET_TOKEN_MUST_NOT_APPEAR" not in response.data
    assert b"Configured" in response.data

    assert client.post("/add_country", headers=headers, data={"country": "GB"}).status_code == 400

    match = re.search(
        rb'name="_csrf_token" value="([^"]+)"',
        response.data,
    )
    assert match
    response = client.post(
        "/add_country",
        headers=headers,
        data={"country": "GB", "_csrf_token": match.group(1).decode()},
    )
    assert response.status_code == 302


def test_message_template_rejects_unknown_fields(database):
    import web_ui_plugin.web_ui as web

    web._validate_message_template("{title} {condition} {image}")
    with pytest.raises(ValueError, match="Unsupported"):
        web._validate_message_template("{title} {description}")
    with pytest.raises(ValueError, match="Unsupported"):
        web._validate_message_template("{title} {private_value}")


def test_remove_description_migration_repairs_stored_template(database):
    db.set_parameter(
        "message_template",
        "Title : {title}\nDescription : {description}\n{image}",
    )
    assert db.migrate_remove_description_field()

    migrated = db.get_parameter("message_template")
    assert "{description}" not in migrated
    assert "Description :" not in migrated
    assert "{title}" in migrated
    assert "{image}" in migrated
    # Idempotent: a second run is a no-op and still succeeds.
    assert db.migrate_remove_description_field()
    assert db.get_parameter("message_template") == migrated


def test_scraper_health_reports_stall_block_and_recovery(database):
    core = _core()
    import time as _time

    now = int(_time.time())
    db.set_parameter("query_refresh_delay", "300")

    # Fresh boot: no heartbeat recorded yet -> never reported as stalled.
    health = core.get_scraper_health(now=now)
    assert health["heartbeat_age"] is None
    assert not health["stalled"] and not health["blocked"]

    # A heartbeat plus a successful cycle -> healthy.
    core.record_scraper_heartbeat()
    core._finalize_scrape_cycle(successful_fetches=5, query_count=10)
    health = core.get_scraper_health(now=now)
    assert not health["stalled"] and not health["blocked"]
    assert health["failed_cycles"] == 0

    # Heartbeat far in the past -> stalled.
    db.set_parameter("scraper_heartbeat", str(now - 5000))
    assert core.get_scraper_health(now=now)["stalled"]

    # Three consecutive empty cycles -> blocked.
    db.set_parameter("scraper_heartbeat", str(now))
    for _ in range(3):
        core._finalize_scrape_cycle(successful_fetches=0, query_count=10)
    health = core.get_scraper_health(now=now)
    assert health["failed_cycles"] == 3 and health["blocked"]

    # A single good cycle clears the block.
    core._finalize_scrape_cycle(successful_fetches=1, query_count=10)
    assert not core.get_scraper_health(now=now)["blocked"]


def test_startup_resets_stale_watchdog_without_false_alert(database, monkeypatch):
    import time as _time
    import vinted_notifications as app

    core = _core()
    now = int(_time.time())
    db.set_parameters(
        {
            "query_refresh_delay": "300",
            "telegram_enabled": "True",
            "telegram_chat_id": "111",
            "scraper_heartbeat": str(now - 5000),
            "scraper_last_ok": str(now - 60),
            "scraper_failed_cycles": "3",
            "scraper_watchdog_alerted": "True",
        }
    )

    app.reset_scraper_watchdog_baseline(now=now)

    assert db.get_parameter("scraper_heartbeat") == str(now)
    assert db.get_parameter("scraper_failed_cycles") == "0"
    assert db.get_parameter("scraper_watchdog_alerted") == "False"
    assert db.get_parameter("scraper_last_ok") == str(now - 60)

    health = core.get_scraper_health(now=now)
    assert not health["stalled"] and not health["blocked"]

    enqueued = []
    monkeypatch.setattr(
        app.db,
        "enqueue_notification",
        lambda *args, **kwargs: enqueued.append((args, kwargs)),
    )
    app.check_scraper_watchdog()
    assert enqueued == []


def test_telegram_polling_can_be_disabled_for_local_testing(monkeypatch):
    import vinted_notifications as app

    monkeypatch.setattr(app.sys, "argv", ["vinted_notifications.py"])
    monkeypatch.delenv("VN_TELEGRAM_POLLING", raising=False)
    assert app.telegram_polling_enabled()

    monkeypatch.setenv("VN_TELEGRAM_POLLING", "false")
    assert not app.telegram_polling_enabled()

    monkeypatch.setenv("VN_TELEGRAM_POLLING", "true")
    monkeypatch.setattr(
        app.sys,
        "argv",
        ["vinted_notifications.py", "--telegram-send-only"],
    )
    assert not app.telegram_polling_enabled()


def test_telegram_send_only_mode_never_builds_a_poller(database, monkeypatch):
    import telegram
    import telegram_bot_plugin.telegram_bot as plugin

    lifecycle = []

    class FakeBot:
        def __init__(self, token):
            lifecycle.append(("created", token))

        async def initialize(self):
            lifecycle.append(("initialized", None))

        async def shutdown(self):
            lifecycle.append(("shutdown", None))

    async def finite_send_only(self):
        await self.bot.initialize()
        await self.bot.shutdown()

    monkeypatch.setattr(telegram, "Bot", FakeBot)
    monkeypatch.setattr(plugin.LeRobot, "run_send_only", finite_send_only)

    robot = plugin.LeRobot(None, polling_enabled=False)

    assert robot.app is None
    assert [event for event, _ in lifecycle] == [
        "created",
        "initialized",
        "shutdown",
    ]


def test_notification_outbox_persists_and_retries(database):
    import time as _time
    import json as _json

    assert db.migrate_pending_notifications_table()

    first = db.enqueue_notification(
        "hello",
        "http://x",
        "Open",
        ["111"],
        query_id=7,
    )
    second = db.enqueue_notification("world", None, None, ["111", "222"])
    assert db.count_pending_notifications() == 2

    due = db.get_due_notifications(limit=10)
    assert {row[0] for row in due} == {first, second}
    # Row shape:
    # (id, content, url, button_text, chat_ids_json, query_id, attempts)
    first_row = next(row for row in due if row[0] == first)
    second_row = next(row for row in due if row[0] == second)
    assert first_row[5] == 7
    assert _json.loads(second_row[4]) == ["111", "222"]

    # Delivered rows are removed.
    db.delete_notification(first)
    assert {row[0] for row in db.get_due_notifications(limit=10)} == {second}

    # A failed row is deferred and no longer due, but still pending (survives
    # a restart, which is the whole point of the outbox).
    db.reschedule_notification(second, attempts=1, next_attempt_at=_time.time() + 3600)
    assert db.get_due_notifications(limit=10) == []
    assert db.count_pending_notifications() == 1

    # Once its next-attempt time passes it becomes due again.
    db.reschedule_notification(second, attempts=1, next_attempt_at=0)
    assert {row[0] for row in db.get_due_notifications(limit=10)} == {second}


def test_outbox_migration_adds_query_id_to_existing_table(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-outbox.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE pending_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                url TEXT,
                button_text TEXT,
                chat_ids TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    assert db.migrate_pending_notifications_table()
    with sqlite3.connect(database_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pending_notifications)")
        }
    assert "query_id" in columns


def test_telegram_unsubscribe_button_removes_only_clicking_user(
    database,
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace
    from telegram_bot_plugin.telegram_bot import LeRobot

    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.approve_telegram_user("222", "Sister")

    query_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=coat"
    )
    query_id, _, _ = db.add_query_to_db(query_url, chat_id="111")
    _, _, subscribed = db.add_query_to_db(query_url, chat_id="222")
    assert subscribed

    robot = LeRobot.__new__(LeRobot)
    robot.polling_enabled = True
    sent = []

    async def fake_send(chat_id, content, markup):
        sent.append((chat_id, content, markup))
        return True

    monkeypatch.setattr(robot, "_send_message_with_retries", fake_send)

    async def exercise_button():
        delivered = await robot.send_new_post(
            "New coat",
            "https://www.vinted.co.uk/items/1",
            "Open Vinted",
            chat_ids=["111", "222"],
            query_id=query_id,
        )
        assert delivered

        sister_markup = next(
            markup for chat_id, _, markup in sent if chat_id == "222"
        )
        unsubscribe_buttons = [
            button
            for row in sister_markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        assert [button.callback_data for button in unsubscribe_buttons] == [
            f"unsubscribe:{query_id}"
        ]

        class Callback:
            data = f"unsubscribe:{query_id}"
            message = SimpleNamespace(reply_markup=sister_markup)

            def __init__(self):
                self.answers = []
                self.edited_markup = sister_markup

            async def answer(self, text, show_alert=False):
                self.answers.append((text, show_alert))

            async def edit_message_reply_markup(self, markup):
                self.edited_markup = markup

        callback = Callback()
        update = SimpleNamespace(
            callback_query=callback,
            effective_chat=SimpleNamespace(id=222),
        )
        await robot.unsubscribe_query(update, None)
        return callback

    callback = asyncio.run(exercise_button())

    assert set(db.get_query_subscribers(query_id)) == {"111"}
    assert callback.answers == [("Unsubscribed from this search.", False)]
    remaining_callbacks = [
        button.callback_data
        for row in callback.edited_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert remaining_callbacks == []


def test_send_only_notifications_do_not_offer_server_callbacks(
    database,
    monkeypatch,
):
    import asyncio
    from telegram_bot_plugin.telegram_bot import LeRobot

    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()

    robot = LeRobot.__new__(LeRobot)
    robot.polling_enabled = False
    sent = []

    async def fake_send(chat_id, content, markup):
        sent.append(markup)
        return True

    monkeypatch.setattr(robot, "_send_message_with_retries", fake_send)
    asyncio.run(
        robot.send_new_post(
            "Local test",
            "https://www.vinted.co.uk/items/1",
            "Open Vinted",
            chat_ids=["111"],
            query_id=99,
        )
    )

    assert len(sent) == 1
    assert all(
        button.callback_data is None
        for row in sent[0].inline_keyboard
        for button in row
    )


def test_subscribed_item_is_persisted_to_outbox(database, monkeypatch):
    core = _core()

    class Item:
        id = 77
        title = "Wool Coat"
        price = 30
        currency = "GBP"
        brand_title = "Brand"
        condition = "Very good"
        description = None
        photo = None
        url = "https://www.vinted.co.uk/items/77"
        raw_timestamp = 100
        raw_data = {"user": {"id": 1}}

    parameters = {
        "banwords": "",
        "message_template": db.DEFAULT_MESSAGE_TEMPLATE,
    }
    monkeypatch.setattr(core.db, "get_parameter", parameters.get)
    monkeypatch.setattr(core.db, "get_last_timestamp", lambda query_id: None)
    monkeypatch.setattr(core.db, "is_item_in_db_by_id", lambda item_id: False)
    monkeypatch.setattr(core.db, "get_allowlist", lambda: 0)
    monkeypatch.setattr(core.db, "get_query_subscribers", lambda query_id: ["123"])
    monkeypatch.setattr(core.db, "add_item_to_db", lambda **kwargs: None)

    enqueued = []
    monkeypatch.setattr(
        core.db,
        "enqueue_notification",
        lambda content, url, button_text, chat_ids, query_id=None: enqueued.append(
            (content, url, button_text, chat_ids, query_id)
        ),
    )

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], 1))
    core.clear_item_queue(source, destination)

    # The Telegram notification was persisted for the subscriber.
    assert len(enqueued) == 1
    content, url, button_text, chat_ids, query_id = enqueued[0]
    assert chat_ids == ["123"]
    assert query_id == 1
    assert url == "https://www.vinted.co.uk/items/77"
    assert "Wool Coat" in content
    # RSS still receives the item on the in-memory queue.
    rss_item = destination.get_nowait()
    assert len(rss_item) == 6


def test_retryable_telegram_error_classification():
    from telegram.error import NetworkError, TimedOut, BadRequest
    from telegram_bot_plugin.telegram_bot import is_retryable_telegram_error

    # Transient network problems are retried.
    assert is_retryable_telegram_error(NetworkError("Bad Gateway"))
    assert is_retryable_telegram_error(TimedOut())
    # Permanent client errors are not.
    assert not is_retryable_telegram_error(BadRequest("chat not found"))
    assert not is_retryable_telegram_error(ValueError("boom"))


def test_queries_page_has_search_sort_pagination_and_shared_modals(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://github.com/x/y"),
    )

    # Two queries: one with a last-found timestamp, one that never found an item.
    conn = db.get_db_connection()
    try:
        conn.execute(
            "INSERT INTO queries (query, last_item, query_name) VALUES (?, ?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=coat", 1700000000, "Coats"),
        )
        conn.execute(
            "INSERT INTO queries (query, last_item, query_name) VALUES (?, ?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=boots", None, "Boots"),
        )
        conn.commit()
    finally:
        conn.close()

    response = web.app.test_client().get("/queries")
    assert response.status_code == 200
    html = response.data.decode()

    # Search + sortable columns + pagination controls.
    assert 'id="querySearchInput"' in html
    assert 'id="queriesTable"' in html
    assert 'data-sort-key="query"' in html
    assert 'data-sort-key="lastFound"' in html
    assert 'id="queryPreviousPage"' in html
    assert 'id="queryNextPage"' in html
    assert "const pageSize = 25" in html

    # Rows carry the raw timestamp for correct client-side sorting.
    assert "data-query-row" in html
    assert "data-last-found=" in html

    # Exactly one shared Edit/Delete modal — the per-row modals are gone.
    assert 'id="editQueryModal"' in html
    assert 'id="deleteQueryModal"' in html
    assert 'data-bs-target="#editQueryModal"' in html
    assert html.count('id="editQueryModal"') == 1
    assert html.count('id="deleteQueryModal"') == 1
    assert 'id="editModal' not in html
    assert 'id="deleteModal' not in html

    # Tier 2: multi-select bulk remove, items column, pause/resume, relative time.
    assert 'id="querySelectAll"' in html
    assert 'class="form-check-input query-select"' in html
    assert 'id="bulkRemoveButton"' in html
    assert 'action="/remove_query/bulk"' in html
    assert 'data-sort-key="items"' in html
    assert "data-item-count=" in html
    assert "query-toggle" in html
    assert "data-relative-time=" in html

    # Tier 3: bulk add, Open-on-Vinted link, filter chips.
    assert 'action="/add_query/bulk"' in html
    assert 'id="bulkAddPanel"' in html
    assert 'target="_blank"' in html
    assert "query-filter-chips" in html


def test_query_pause_enable_counts_and_bulk_remove(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )

    conn = db.get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO queries (query, last_item, query_name) VALUES (?, ?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=a", 100, "A"),
        )
        qid_a = cur.lastrowid
        cur.execute(
            "INSERT INTO queries (query, last_item, query_name) VALUES (?, ?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=b", 200, "B"),
        )
        qid_b = cur.lastrowid
        cur.execute(
            "INSERT INTO queries (query, last_item, query_name) VALUES (?, ?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=c", None, "C"),
        )
        qid_c = cur.lastrowid
        cur.execute("INSERT INTO items (item, query_id) VALUES (?, ?)", (1, qid_a))
        cur.execute("INSERT INTO items (item, query_id) VALUES (?, ?)", (2, qid_a))
        conn.commit()
    finally:
        conn.close()

    # Item counts are aggregated in one query.
    counts = db.get_query_item_counts()
    assert counts.get(qid_a) == 2
    assert qid_b not in counts

    # All three enabled by default; the scraper's enabled_only view sees them.
    assert len(db.get_queries(enabled_only=True)) == 3
    assert all(db.get_query_enabled_map().values())

    client = web.app.test_client()
    token_page = client.get("/queries")
    token = re.search(
        rb'name="_csrf_token" value="([^"]+)"', token_page.data
    ).group(1).decode()

    # Pause B through the toggle route; the scraper then skips it.
    response = client.post(f"/toggle_query/{qid_b}", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.get_json()["enabled"] is False
    assert db.get_query_enabled_map()[qid_b] is False
    active = {row[0] for row in db.get_queries(enabled_only=True)}
    assert active == {qid_a, qid_c}

    # Toggling again resumes it.
    response = client.post(f"/toggle_query/{qid_b}", headers={"X-CSRF-Token": token})
    assert response.get_json()["enabled"] is True

    # Bulk-remove A and C, leaving only B.
    response = client.post(
        "/remove_query/bulk",
        data={"_csrf_token": token, "query_ids": [str(qid_a), str(qid_c)]},
    )
    assert response.status_code == 302
    assert {row[0] for row in db.get_queries()} == {qid_b}


def test_query_filter_summary_chips():
    import web_ui_plugin.web_ui as web

    chips = web._summarise_query_filters(
        "https://www.vinted.co.uk/catalog?search_text=coat&currency=GBP"
        "&price_from=10&price_to=50&brand_id[]=53&brand_id[]=88&size_ids[]=7"
        "&status[]=6&order=newest_first"
    )
    assert "UK" in chips
    assert "£10–£50" in chips
    assert "2 brands" in chips
    assert "1 size" in chips
    assert "1 condition" in chips

    # A bare text search yields just the country chip.
    assert web._summarise_query_filters(
        "https://www.vinted.fr/catalog?search_text=nike"
    ) == ["FR"]

    # Price-only-upper bound.
    assert "≤ £20" in web._summarise_query_filters(
        "https://www.vinted.co.uk/catalog?currency=GBP&price_to=20"
    )


def test_bulk_add_reports_added_and_duplicates(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()

    client = web.app.test_client()
    token = re.search(
        rb'name="_csrf_token" value="([^"]+)"', client.get("/queries").data
    ).group(1).decode()

    body = (
        "https://www.vinted.co.uk/catalog?search_text=coat\n"
        "https://www.vinted.co.uk/catalog?search_text=coat\n"  # duplicate
        "not-a-url\n"
        "https://www.vinted.co.uk/catalog?search_text=boots\n"
    )
    response = client.post(
        "/add_query/bulk",
        data={"_csrf_token": token, "queries": body},
        follow_redirects=False,
    )
    assert response.status_code == 302
    # Two unique queries actually landed in the database.
    stored = {q[1] for q in db.get_queries()}
    assert any("search_text=coat" in url for url in stored)
    assert any("search_text=boots" in url for url in stored)
    assert len(db.get_queries()) == 2


def test_config_page_health_test_and_single_quiet_hours(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    html_text = web.app.test_client().get("/config").data.decode()

    # Quiet hours now lives in the template exactly once (no string-append),
    # and the duplicate Save button that came with the injected panel is gone.
    assert html_text.count('id="quiet-hours-settings"') == 1
    assert html_text.count(">Save Configuration<") == 1

    # New Config-page features are present.
    assert 'id="testTelegramBtn"' in html_text
    assert 'id="templatePreview"' in html_text
    assert "System Health" in html_text
    assert 'id="refreshHealthBtn"' in html_text

    # Input guardrails.
    assert 'min="30"' in html_text  # query refresh delay
    assert 'min="1"' in html_text  # items per query


def test_config_health_endpoint(database):
    import web_ui_plugin.web_ui as web

    assert db.migrate_pending_notifications_table()
    data = web.app.test_client().get("/config/health").get_json()
    assert "pending_notifications" in data
    assert data["scraper"]["status"] in {"ok", "stalled", "blocked"}
    assert set(data["queries"]) == {"total", "active", "paused"}


def test_test_telegram_route(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    client = web.app.test_client()
    token = re.search(
        rb'name="_csrf_token" value="([^"]+)"', client.get("/config").data
    ).group(1).decode()

    # Unconfigured -> 400 with guidance, no network call.
    response = client.post("/test_telegram", headers={"X-CSRF-Token": token})
    assert response.status_code == 400
    assert "Save a bot token" in response.get_json()["message"]

    db.set_parameter("telegram_token", "123:abc")
    db.set_parameter("telegram_chat_id", "111")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {}}

    monkeypatch.setattr(web.requests, "post", lambda *a, **k: FakeResponse())
    response = client.post("/test_telegram", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.get_json()["status"] == "success"

    class RejectResponse:
        status_code = 400

        def json(self):
            return {"ok": False, "description": "chat not found"}

    monkeypatch.setattr(web.requests, "post", lambda *a, **k: RejectResponse())
    response = client.post("/test_telegram", headers={"X-CSRF-Token": token})
    assert response.status_code == 400
    assert "chat not found" in response.get_json()["message"]


def test_logs_viewer_renders_messages_as_text_not_html():
    template = (ROOT / "web_ui_plugin" / "templates" / "logs.html").read_text(
        encoding="utf-8"
    )
    # Log fields must be inserted as text (textContent), never interpolated into
    # innerHTML — otherwise scraped/user-influenced log lines could inject HTML
    # or scripts (DOM XSS).
    assert "${log.message}" not in template
    assert "${log.module}" not in template
    assert "messageCell.textContent = log.message" in template


def test_dashboard_query_table_has_accessible_sort_controls():
    template = (ROOT / "web_ui_plugin" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="dashboardQueriesTable"' in template
    assert 'data-sort-key="query"' in template
    assert 'data-sort-key="lastFound"' in template
    assert 'aria-sort="descending"' in template
    assert 'id="querySortStatus" aria-live="polite"' in template
    assert "localeCompare" in template
    assert "queryRows.slice().sort" in template
    assert 'shown as "Never"' in template


def test_dashboard_has_search_pagination_relative_times_and_collapsible_sections():
    template = (ROOT / "web_ui_plugin" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    web_source = (ROOT / "web_ui_plugin" / "web_ui.py").read_text(encoding="utf-8")

    assert 'id="querySearchInput"' in template
    assert 'id="queryPreviousPage"' in template
    assert 'id="queryNextPage"' in template
    assert "const pageSize = 10" in template
    assert 'class="btn btn-sm btn-outline-secondary dashboard-collapse-toggle"' in template
    assert "vintedDashboardSection:" in template
    assert "data-relative-time" in template
    assert "Intl.RelativeTimeFormat" in template
    assert "items = db.get_items(limit=5)" in web_source
