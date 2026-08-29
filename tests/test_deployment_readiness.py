import base64
import json
import os
import queue
import re
import sqlite3
import sys
from contextlib import closing
from datetime import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from url_normalizer import normalise_vinted_url  # noqa: E402


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


def test_database_migration_selection_matches_the_exact_source_version():
    files = [
        "1.0.5.1_1.0.5.2.sql",
        "1.0.5_1.0.5.1.sql",
        "1.0.5.2_1.0.5.3.sql",
    ]
    assert db.next_database_migration("1.0.5", files) == "1.0.5_1.0.5.1.sql"
    assert db.next_database_migration("1.0.5.1", files) == ("1.0.5.1_1.0.5.2.sql")

    with pytest.raises(RuntimeError, match="Ambiguous database migrations"):
        db.next_database_migration(
            "1.0.5",
            ["1.0.5_1.0.5.1.sql", "1.0.5_2.0.0.sql"],
        )


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


def test_quiet_hours_day_parsing():
    core = _core()
    # None (never configured) keeps the original every-day behaviour.
    assert core._parse_quiet_days(None) == {0, 1, 2, 3, 4, 5, 6}
    # An explicitly empty selection means no quiet days.
    assert core._parse_quiet_days("") == set()
    assert core._parse_quiet_days("0,4,6") == {0, 4, 6}
    assert core._parse_quiet_days("0, 1, 9, x") == {0, 1}  # ignores out-of-range/junk


def test_quiet_hours_migration_adds_default_and_preserves_saved_days(database):
    conn = db.get_db_connection()
    try:
        conn.execute("DELETE FROM parameters WHERE key='quiet_hours_days'")
        conn.commit()
    finally:
        conn.close()

    assert db.migrate_quiet_hours_schema()
    assert db.get_parameter("quiet_hours_days") == "0,1,2,3,4,5,6"

    for saved_value in ("0,4", ""):
        db.set_parameter("quiet_hours_days", saved_value)
        assert db.migrate_quiet_hours_schema()
        assert db.migrate_quiet_hours_schema()
        assert db.get_parameter("quiet_hours_days") == saved_value


def test_quiet_hours_respects_days_of_week(database, monkeypatch):
    core = _core()
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    values = {
        "quiet_hours_enabled": "True",
        "quiet_hours_start": "01:00",
        "quiet_hours_end": "06:00",
        "quiet_hours_timezone": "Europe/London",
        "quiet_hours_days": "0,1,2,3,4",  # Mon-Fri only
    }
    monkeypatch.setattr(core.db, "get_parameter", values.get)

    tz = ZoneInfo("Europe/London")
    wednesday = datetime(2026, 7, 1, 2, 0, tzinfo=tz)
    while wednesday.weekday() != 2:  # Wednesday
        wednesday += timedelta(days=1)
    saturday = wednesday + timedelta(days=3)
    assert wednesday.weekday() == 2 and saturday.weekday() == 5

    # 02:00 is inside the window: quiet on Wednesday (a selected day)...
    assert core.get_quiet_hours_status(wednesday)[0] is True
    # ...but NOT on Saturday (weekend excluded).
    assert core.get_quiet_hours_status(saturday)[0] is False
    # Outside the window on a selected day is never quiet.
    assert core.get_quiet_hours_status(wednesday.replace(hour=12))[0] is False


def test_overnight_quiet_hours_belong_to_the_day_the_window_starts(
    database, monkeypatch
):
    core = _core()
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    values = {
        "quiet_hours_enabled": "True",
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "06:00",
        "quiet_hours_timezone": "Europe/London",
        "quiet_hours_days": "0,1,2,3,4",  # nights starting Mon-Fri
    }
    monkeypatch.setattr(core.db, "get_parameter", values.get)

    tz = ZoneInfo("Europe/London")
    friday = datetime(2026, 7, 3, 23, 30, tzinfo=tz)
    while friday.weekday() != 4:
        friday += timedelta(days=1)

    assert core.get_quiet_hours_status(friday)[0] is True
    assert (
        core.get_quiet_hours_status(
            (friday + timedelta(days=1)).replace(hour=5, minute=59)
        )[0]
        is True
    )
    assert (
        core.get_quiet_hours_status(
            (friday + timedelta(days=1)).replace(hour=6, minute=0)
        )[0]
        is False
    )
    # Saturday night and the early hours of Monday belong to excluded weekend
    # start days; Monday becomes quiet only once Monday night's window begins.
    saturday_night = (friday + timedelta(days=1)).replace(hour=23, minute=30)
    monday_early = (friday + timedelta(days=3)).replace(hour=2, minute=0)
    monday_night = monday_early.replace(hour=23, minute=30)
    assert core.get_quiet_hours_status(saturday_night)[0] is False
    assert core.get_quiet_hours_status(monday_early)[0] is False
    assert core.get_quiet_hours_status(monday_night)[0] is True


def test_config_save_stores_quiet_hours_days(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    client = web.app.test_client()
    token = (
        re.search(rb'name="_csrf_token" value="([^"]+)"', client.get("/config").data)
        .group(1)
        .decode()
    )

    base = {
        "_csrf_token": token,
        "quiet_hours_enabled": "on",
        "quiet_hours_start": "01:00",
        "quiet_hours_end": "06:00",
        "quiet_hours_timezone": "Europe/London",
        "items_per_query": "20",
        "query_refresh_delay": "300",
        "rss_port": "8080",
        "rss_max_items": "100",
        "message_template": "{title}",
        "user_agents": "[]",
        "default_headers": "{}",
    }

    # Selected weekdays are stored sorted; weekends are off here.
    response = client.post(
        "/update_config",
        data={**base, "quiet_hours_days": ["4", "0", "1", "2", "3"]},
    )
    assert response.status_code == 302
    assert db.get_parameter("quiet_hours_days") == "0,1,2,3,4"

    # No days checked -> stored as an explicit empty string, not a fallback.
    response = client.post("/update_config", data=base)
    assert response.status_code == 302
    assert db.get_parameter("quiet_hours_days") == ""


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


def test_legacy_orphan_queries_are_assigned_to_admin_only_once(database):
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=legacy", "Legacy"),
        )
        query_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.get_query_subscribers(query_id) == ["111"]
    assert db.get_parameter("multi_user_orphans_migrated") == "True"

    # A later personal unsubscribe is intentional and must not be undone by
    # the idempotent startup migration.
    assert db.remove_query_subscription(query_id, "111")
    assert db.get_query_subscribers(query_id) == []
    assert db.migrate_multi_user_schema()
    assert db.get_query_subscribers(query_id) == []


def test_remove_all_subscriptions_preserves_shared_queries_and_items(database):
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    query_id, _, _ = db.add_query_to_db(
        normalise_vinted_url(
            "https://www.vinted.co.uk/catalog?search_text=shared-history"
        ),
        name="Shared history",
        chat_id="111",
    )
    conn = db.get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO items
                (item, title, price, currency, timestamp, photo_url, query_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (123, "Saved item", 10, "GBP", 1, "", query_id),
        )
        conn.commit()
    finally:
        conn.close()

    assert db.remove_all_query_subscriptions("111")
    assert db.get_query_subscribers(query_id) == []
    conn = db.get_db_connection()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE query_id=?", (query_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_shared_query_is_created_once_for_multiple_users(database):
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.migrate_query_uniqueness()
    assert db.approve_telegram_user("222", "Tester")

    url = normalise_vinted_url("https://www.vinted.co.uk/catalog?search_text=coat")
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
    conn.executescript("""
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
        """)
    conn.commit()
    conn.close()

    assert db.migrate_query_uniqueness()
    conn = db.get_db_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 1
        row = conn.execute("SELECT id,last_item,query_name FROM queries").fetchone()
        assert row == (1, 20, "Named")
        assert conn.execute("SELECT query_id FROM items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT query_id,chat_id FROM query_subscriptions"
        ).fetchone() == (1, "111")
    finally:
        conn.close()


def test_rss_dispatches_when_no_telegram_subscribers(database):
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

    assert db.migrate_pending_notifications_table()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    conn = db.get_db_connection()
    try:
        query_id = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=rss-only", "RSS only"),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)

    dispatched = destination.get_nowait()
    assert len(dispatched) == 6
    assert dispatched[5] == []
    assert db.count_pending_notifications() == 0
    conn = db.get_db_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT last_item FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            == 100
        )
    finally:
        conn.close()


def test_item_description_never_fetches_a_vinted_detail_page(database, monkeypatch):
    core = _core()

    class Item:
        id = 12345
        url = "https://www.vinted.co.uk/items/12345-example"
        description = None

    monkeypatch.setattr(
        core.requester.session,
        "get",
        lambda *args, **kwargs: pytest.fail("detail page request must not run"),
    )
    assert core._get_item_description(Item()) is None
    Item.description = " Catalogue description "
    assert core._get_item_description(Item()) == "Catalogue description"


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
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "camera=()" in response.headers["Permissions-Policy"]
    csp = response.headers["Content-Security-Policy"]
    nonce_match = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
    assert nonce_match
    assert f'<script nonce="{nonce_match.group(1)}">'.encode() in response.data
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    session_cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in session_cookie
    assert "SameSite=Strict" in session_cookie
    assert web.app.config["MAX_CONTENT_LENGTH"] <= 256 * 1024

    bootstrap_js = client.get(
        "/static/vendor/bootstrap/bootstrap.bundle.min.js",
        headers=headers,
    )
    assert bootstrap_js.status_code == 200
    assert bootstrap_js.mimetype == "application/javascript"

    assert (
        client.post("/add_country", headers=headers, data={"country": "GB"}).status_code
        == 400
    )

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


@pytest.fixture
def requester_clock(database, monkeypatch):
    """Make the process-wide requester gate instant and deterministic."""
    import importlib
    from types import SimpleNamespace

    requester_module = importlib.import_module("pyVintedVN.requester")
    clock = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    requester_module.configure_shared_request_gate(None, None)
    requester_module._reset_catalogue_request_gate()
    monkeypatch.setattr(
        requester_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep),
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)
    yield waits
    requester_module.configure_shared_request_gate(None, None)
    requester_module._reset_catalogue_request_gate()


@pytest.mark.parametrize(
    ("status_code", "expected_calls"),
    [(403, 2), (429, 1)],
)
def test_requester_uses_one_bounded_retry_for_block_responses(
    database,
    monkeypatch,
    status_code,
    expected_calls,
    requester_clock,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")

    class Response:
        def __init__(self):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    client = requester_module.Requester()
    client.session = Session()
    monkeypatch.setattr(client, "_rebuild_session", lambda: None)

    response = client.get("https://www.vinted.co.uk/api/v2/catalog/items")

    assert response.status_code == status_code
    assert client.session.calls == expected_calls
    assert requester_clock == (
        [requester_module.FORBIDDEN_RETRY_DELAY_SECONDS] + [1.0] * 57
        if status_code == 403
        else []
    )


def test_requester_recovers_from_transient_403_with_fresh_session(
    database,
    monkeypatch,
    requester_clock,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Session:
        def __init__(self, status_code):
            self.status_code = status_code
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response(self.status_code)

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    client = requester_module.Requester()
    rejected_session = Session(403)
    fresh_session = Session(200)
    client.session = rejected_session
    monkeypatch.setattr(
        client,
        "_rebuild_session",
        lambda: setattr(client, "session", fresh_session),
    )

    response = client.get("https://www.vinted.co.uk/api/v2/catalog/items")

    assert response.status_code == 200
    assert rejected_session.calls == 1
    assert fresh_session.calls == 1


@pytest.mark.parametrize("fresh_status", [200, 401])
def test_requester_rebuilds_session_once_for_401(
    database,
    monkeypatch,
    fresh_status,
    requester_clock,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Session:
        def __init__(self, status_code):
            self.status_code = status_code
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response(self.status_code)

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    client = requester_module.Requester()
    rejected_session = Session(401)
    fresh_session = Session(fresh_status)
    client.session = rejected_session
    rebuilds = []

    def rebuild():
        rebuilds.append(True)
        client.session = fresh_session

    monkeypatch.setattr(client, "_rebuild_session", rebuild)

    response = client.get("https://www.vinted.co.uk/api/v2/catalog/items")

    assert response.status_code == fresh_status
    assert rejected_session.calls == 1
    assert fresh_session.calls == 1
    assert rebuilds == [True]


def test_requester_retries_connection_reset_then_succeeds(
    database,
    monkeypatch,
    requester_clock,
):
    """A reset keep-alive socket (common on the first request of a cycle) is
    retried on a fresh connection instead of surfacing as a network error."""
    import importlib
    import requests

    requester_module = importlib.import_module("pyVintedVN.requester")

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise requests.exceptions.ConnectionError(
                    "('Connection aborted.', ConnectionResetError(10054))"
                )
            return Response(200)

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    client = requester_module.Requester()
    client.session = Session()

    response = client.get("https://www.vinted.co.uk/api/v2/catalog/items")

    assert response.status_code == 200
    assert client.session.calls == 2  # first request reset, retry succeeded


def test_requester_gives_up_after_persistent_connection_resets(
    database,
    monkeypatch,
    requester_clock,
):
    """Persistent resets are surfaced to the scraper after bounded retries."""
    import importlib
    import requests

    requester_module = importlib.import_module("pyVintedVN.requester")

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    client = requester_module.Requester()
    client.session = Session()

    with pytest.raises(requests.exceptions.ConnectionError):
        client.get("https://www.vinted.co.uk/api/v2/catalog/items")

    assert client.session.calls == requester_module.CONNECTION_RESET_MAX_RETRIES + 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "XX"),
        ({"user": {"country_iso_code": "gb"}}, "GB"),
    ],
)
def test_get_user_country_fails_closed_on_payload_errors(
    database,
    monkeypatch,
    payload,
    expected,
):
    core = _core()
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return payload

        def close(self):
            return None

    def get_once(url, **kwargs):
        calls.append((url, kwargs.get("cancel_if")))
        return Response()

    monkeypatch.setattr(core.requester, "get_once", get_once, raising=False)

    assert core.get_user_country("123") == expected
    assert len(calls) == 1
    assert callable(calls[0][1])


def test_get_user_country_fails_closed_on_network_error(database, monkeypatch):
    import requests

    core = _core()
    calls = []

    def get_once(url, **kwargs):
        calls.append((url, kwargs.get("cancel_if")))
        raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(core.requester, "get_once", get_once, raising=False)

    assert core.get_user_country("123") == "XX"
    assert len(calls) == 1
    assert callable(calls[0][1])


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_country_profile_block_opens_global_cooldown_with_missing_headers(
    database,
    monkeypatch,
    status_code,
):
    core = _core()
    core._clear_scraper_cooldown()
    calls = []

    class Response:
        def __init__(self):
            self.status_code = status_code
            self.closed = False

        def close(self):
            self.closed = True

    response = Response()

    def get_once(url, **kwargs):
        calls.append((url, kwargs.get("cancel_if")))
        return response

    monkeypatch.setattr(core.requester, "get_once", get_once, raising=False)
    try:
        assert core.get_user_country("123") == "XX"
        cooldown = core.get_scraper_cooldown()
        assert cooldown["active"] is True
        assert cooldown["status_code"] == status_code
        assert len(calls) == 1
        assert callable(calls[0][1])
        assert response.closed is True
    finally:
        core._clear_scraper_cooldown()


def test_country_lookup_cancels_if_cooldown_opens_while_waiting(
    database,
    monkeypatch,
):
    core = _core()
    core._clear_scraper_cooldown()
    calls = []

    def get_once(url, *, cancel_if=None, **kwargs):
        calls.append(url)
        assert callable(cancel_if)
        assert cancel_if() is False
        core._activate_scraper_cooldown(
            403,
            now=int(core.time.time()),
            duration_seconds=60,
        )
        assert cancel_if() is True
        return None

    monkeypatch.setattr(core.requester, "get_once", get_once, raising=False)
    try:
        assert core.get_user_country("123") == "XX"
        assert len(calls) == 1
    finally:
        core._clear_scraper_cooldown()


def test_item_country_lookup_is_suppressed_during_open_cooldown(
    database,
    monkeypatch,
):
    core = _core()
    core._USER_COUNTRY_CACHE.clear()
    core._activate_scraper_cooldown(
        403,
        now=int(core.time.time()),
        duration_seconds=60,
    )
    monkeypatch.setattr(
        core.requester,
        "get_once",
        lambda *args, **kwargs: pytest.fail("country request must not run"),
        raising=False,
    )
    try:
        assert core.get_user_country("123") == "XX"
    finally:
        core._clear_scraper_cooldown()


def test_item_country_prefers_embedded_data_and_bounds_successful_cache(
    database,
    monkeypatch,
):
    core = _core()
    core._USER_COUNTRY_CACHE.clear()
    monkeypatch.setattr(core, "_USER_COUNTRY_CACHE_MAX_ENTRIES", 2)
    calls = []

    def get_country(profile_id):
        calls.append(profile_id)
        return "XX" if profile_id == 5 else "GB"

    monkeypatch.setattr(core, "get_user_country", get_country)

    class Item:
        raw_data = {"user": {"id": 1, "country_iso_code": "fr"}}

    try:
        assert core._resolve_item_country(Item()) == "FR"
        assert calls == []

        Item.raw_data = {"user": {"id": 2}}
        assert core._resolve_item_country(Item()) == "GB"
        assert core._resolve_item_country(Item()) == "GB"
        assert calls == [2]

        for profile_id in (3, 4):
            Item.raw_data = {"user": {"id": profile_id}}
            assert core._resolve_item_country(Item()) == "GB"
        assert set(core._USER_COUNTRY_CACHE) == {"3", "4"}

        Item.raw_data = {"user": {"id": 5}}
        assert core._resolve_item_country(Item()) == "XX"
        assert core._resolve_item_country(Item()) == "XX"
        assert calls == [2, 3, 4, 5, 5]

        Item.raw_data = {}
        assert core._resolve_item_country(Item()) == "XX"
        assert calls == [2, 3, 4, 5, 5]
    finally:
        core._USER_COUNTRY_CACHE.clear()


def test_banword_filter_runs_before_country_and_reads_allowlist_once(
    database,
    monkeypatch,
):
    core = _core()
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=blocked", "Blocked"),
        )
        query_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    db.set_parameter("banwords", "blocked")
    db.add_to_allowlist("GB")

    class Item:
        id = 99
        title = "Blocked listing"
        raw_timestamp = 123
        raw_data = {"user": {"id": 1}}

    allowlist_calls = []
    original_get_allowlist = core.db.get_allowlist

    def get_allowlist():
        allowlist_calls.append(True)
        return original_get_allowlist()

    monkeypatch.setattr(core.db, "get_allowlist", get_allowlist)
    monkeypatch.setattr(
        core,
        "_resolve_item_country",
        lambda item: pytest.fail("country lookup must follow banword filtering"),
    )
    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)
    assert destination.empty()
    assert allowlist_calls == [True]


def test_unknown_country_keeps_existing_allowlist_fail_open_semantics(
    database,
    monkeypatch,
):
    core = _core()
    assert db.migrate_pending_notifications_table()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    db.add_to_allowlist("GB")
    conn = db.get_db_connection()
    try:
        query_id = conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=unknown", "Unknown"),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    class Item:
        id = 199
        title = "Unknown-country listing"
        price = 12
        currency = "GBP"
        brand_title = "Brand"
        condition = "Good"
        description = None
        photo = None
        url = "https://www.vinted.co.uk/items/199"
        raw_timestamp = 200
        raw_data = {"user": {}}

    monkeypatch.setattr(core, "_resolve_item_country", lambda item: "XX")
    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)
    assert not destination.empty()


def test_ai_deal_evaluator_formats_verdicts():
    import ai_deal_evaluator as ai

    assert "AI: GREAT DEAL" in ai.format_verdict(
        '{"verdict":"excellent","reason":"well under value"}'
    )
    assert "AI: GOOD DEAL" in ai.format_verdict('{"verdict":"good","reason":"fair"}')
    assert "DON'T BUY" in ai.format_verdict(
        '{"verdict":"dont_buy","reason":"overpriced"}'
    )
    # HTML in the reason is escaped so it can't break the Telegram message.
    assert "&lt;b&gt;" in ai.format_verdict('{"verdict":"good","reason":"<b>x"}')
    # Unknown verdict or malformed input yields no rating (best-effort).
    assert ai.format_verdict('{"verdict":"maybe"}') is None
    assert ai.format_verdict("not json") is None
    assert ai.format_verdict("[1,2]") is None


def test_contains_banwords_supports_and_syntax(database):
    core = _core()
    banwords = "empty+box|||empty+bottle|||box only"

    # AND-rule ("a+b"): every term must be present, in any order/position, so a
    # split phrase like "Empty ... Box" is caught.
    assert (
        core.contains_banwords("Empty Penhaligons The Favourite Box", banwords) is True
    )
    assert core.contains_banwords("Jo Malone empty box", banwords) is True
    assert core.contains_banwords("Empty fragrance bottle", banwords) is True

    # A plain (non-'+') banword still matches as a substring.
    assert core.contains_banwords("iPhone box only, no phone", banwords) is True

    # The AND-rule must NOT over-match: "empty" without "box"/"bottle" is kept,
    # so legitimate robot-vacuum listings survive.
    assert (
        core.contains_banwords("Eufy Robot Vacuum with Self-Empty Station", banwords)
        is False
    )
    assert core.contains_banwords("robot with auto empty bin", banwords) is False

    # Unrelated titles are untouched.
    assert core.contains_banwords("Karen Millen dress size 12", banwords) is False


def test_query_spacing_leaves_idle_time_between_scrape_cycles(
    database,
):
    core = _core()
    db.set_parameter("query_refresh_delay", "300")

    spacing = core._get_query_spacing_seconds(133)
    active_window = spacing * 132

    assert 1 <= spacing <= 5
    assert 2 * 60 <= active_window <= 3 * 60
    assert active_window < 300

    db.set_parameter("query_refresh_delay", "600")
    smaller_query_set_window = core._get_query_spacing_seconds(41) * 40
    assert smaller_query_set_window <= 600 * 0.5


def test_403_circuit_breaker_stops_cycle_and_escalates_cooldown(
    database,
    monkeypatch,
):
    import requests

    core = _core()
    core._clear_scraper_cooldown()
    db.set_parameter("quiet_hours_enabled", "False")
    db.set_parameter("query_refresh_delay", "300")

    conn = db.get_db_connection()
    try:
        conn.executemany(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            [
                (
                    f"https://www.vinted.co.uk/catalog?search_text=item-{index}",
                    f"Item {index}",
                )
                for index in range(1, 6)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    search_calls = []

    class Items:
        def search(self, url, nbr_items):
            search_calls.append(url)
            response = type(
                "Response",
                (),
                {"status_code": 403, "headers": {}},
            )()
            raise requests.exceptions.HTTPError(
                "403 Client Error",
                response=response,
            )

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    clock = [1_000_000]
    monkeypatch.setattr(core, "Vinted", FakeVinted)
    monkeypatch.setattr(core.time, "time", lambda: clock[0])
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(core.random, "uniform", lambda low, high: 1.0)

    # Requester has already retried with a fresh session, so the first confirmed
    # rejected query opens protection immediately. No other query is contacted.
    core.process_items(queue.Queue())
    assert len(search_calls) == 1
    cooldown = core.get_scraper_cooldown(now=clock[0])
    assert cooldown["active"]
    assert cooldown["remaining"] == 10 * 60
    assert cooldown["level"] == 1
    assert cooldown["status_code"] == 403

    # Scheduled runs during the cooldown make no Vinted requests.
    core.process_items(queue.Queue())
    assert len(search_calls) == 1

    # An expired cooldown is a recovery probe. Its first confirmed 403
    # immediately reopens the breaker.
    clock[0] += 10 * 60 + 1
    core.process_items(queue.Queue())
    assert len(search_calls) == 2
    cooldown = core.get_scraper_cooldown(now=clock[0])
    assert cooldown["remaining"] == 30 * 60
    assert cooldown["level"] == 2

    # Other already-queued jobs see the reopened global cooldown and make no
    # Vinted request.
    core.process_items(queue.Queue())
    assert len(search_calls) == 2

    # Further failed recovery probes back off substantially for an IP-wide
    # block, while still making only one confirmed query each time.
    clock[0] += 30 * 60 + 1
    core.process_items(queue.Queue())
    assert len(search_calls) == 3
    cooldown = core.get_scraper_cooldown(now=clock[0])
    assert cooldown["remaining"] == 2 * 60 * 60
    assert cooldown["level"] == 3

    clock[0] += 2 * 60 * 60 + 1
    core.process_items(queue.Queue())
    assert len(search_calls) == 4
    cooldown = core.get_scraper_cooldown(now=clock[0])
    assert cooldown["remaining"] == 8 * 60 * 60
    assert cooldown["level"] == 4


def test_active_403_cooldown_warns_once_for_queued_jobs(
    database,
    monkeypatch,
    caplog,
):
    import logging

    core = _core()
    core._clear_scraper_cooldown()
    db.set_parameter("quiet_hours_enabled", "False")
    monkeypatch.setattr(core.time, "time", lambda: 1_000)
    core._activate_scraper_cooldown(403, now=1_000)
    caplog.set_level(logging.DEBUG, logger=core.logger.name)

    for _ in range(3):
        core.process_items(queue.Queue(), query_ids=[1])

    skips = [
        record
        for record in caplog.records
        if "circuit breaker is open" in record.getMessage()
        and "skipping this cycle" in record.getMessage()
    ]
    assert [record.levelno for record in skips] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]


def test_429_opens_global_cooldown_without_sleeping_in_scheduled_job(
    database,
    monkeypatch,
):
    import requests

    core = _core()
    core._clear_scraper_cooldown()
    db.set_parameter("quiet_hours_enabled", "False")

    conn = db.get_db_connection()
    try:
        conn.executemany(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            [
                (
                    f"https://www.vinted.co.uk/catalog?search_text=rate-{index}",
                    f"Rate {index}",
                )
                for index in range(1, 6)
            ],
        )
        conn.commit()
        query_id = conn.execute("SELECT MIN(id) FROM queries").fetchone()[0]
    finally:
        conn.close()

    search_calls = []
    waits = []

    class Items:
        def search(self, url, nbr_items):
            search_calls.append(url)
            response = type(
                "Response",
                (),
                {"status_code": 429, "headers": {"Retry-After": "120"}},
            )()
            raise requests.exceptions.HTTPError(
                "429 Client Error",
                response=response,
            )

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    monkeypatch.setattr(core.time, "time", lambda: 1_000)
    monkeypatch.setattr(core.time, "sleep", waits.append)
    monkeypatch.setattr(core.random, "uniform", lambda low, high: 1.0)

    core.process_items(queue.Queue(), query_ids=[query_id])

    assert len(search_calls) == 1
    assert waits == []
    cooldown = core.get_scraper_cooldown(now=1_000)
    assert cooldown["active"]
    assert cooldown["remaining"] == 120
    assert cooldown["status_code"] == 429

    # Other already-queued per-query jobs return before touching Vinted.
    core.process_items(queue.Queue(), query_ids=[query_id])
    assert len(search_calls) == 1


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("1", 30), ("9999", 5 * 60), ("invalid", 60)],
)
def test_429_retry_after_is_bounded(database, retry_after, expected):
    core = _core()
    response = type("Response", (), {"headers": {"Retry-After": retry_after}})()
    assert core._get_bounded_retry_after_seconds(response) == expected


def test_confirmed_401_opens_global_cooldown_after_one_scheduled_query(
    database,
    monkeypatch,
):
    import requests

    core = _core()
    core._clear_scraper_cooldown()
    db.set_parameter("quiet_hours_enabled", "False")
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=auth", "Auth"),
        )
        query_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    search_calls = []

    class Items:
        def search(self, url, nbr_items):
            search_calls.append(url)
            response = type("Response", (), {"status_code": 401, "headers": {}})()
            raise requests.exceptions.HTTPError(
                "401 Client Error",
                response=response,
            )

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    monkeypatch.setattr(core.time, "time", lambda: 1_000)
    waits = []
    monkeypatch.setattr(core.time, "sleep", waits.append)

    core.process_items(queue.Queue(), query_ids=[query_id])

    assert len(search_calls) == 1
    assert waits == []
    cooldown = core.get_scraper_cooldown(now=1_000)
    assert cooldown["active"]
    assert cooldown["remaining"] == 5 * 60
    assert cooldown["status_code"] == 401
    assert db.get_parameter("scraper_failed_cycles") in (None, "0")

    core.process_items(queue.Queue(), query_ids=[query_id])
    assert len(search_calls) == 1


def test_local_cooldown_skips_queued_job_when_persistence_fails(
    database,
    monkeypatch,
):
    import requests

    core = _core()
    core._clear_scraper_cooldown()
    db.set_parameter("quiet_hours_enabled", "False")
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=limited", "Limited"),
        )
        query_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    search_calls = []

    class Items:
        def search(self, url, nbr_items):
            search_calls.append(url)
            response = type(
                "Response",
                (),
                {"status_code": 429, "headers": {"Retry-After": "75"}},
            )()
            raise requests.exceptions.HTTPError(
                "429 Client Error",
                response=response,
            )

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    monkeypatch.setattr(core.time, "time", lambda: 1_000)
    monkeypatch.setattr(core.db, "set_parameters", lambda values: False)

    core.process_items(queue.Queue(), query_ids=[query_id])
    assert len(search_calls) == 1
    assert core.get_scraper_cooldown(now=1_000)["remaining"] == 75

    core.process_items(queue.Queue(), query_ids=[query_id])
    assert len(search_calls) == 1
    core._clear_scraper_cooldown()


def test_scheduled_query_failure_is_not_counted_as_a_failed_full_cycle(
    database,
    monkeypatch,
):
    import requests

    core = _core()
    db.set_parameter("quiet_hours_enabled", "False")
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=broken", "Broken"),
        )
        query_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    class Items:
        def search(self, url, nbr_items):
            response = type("Response", (), {"status_code": 500, "headers": {}})()
            raise requests.exceptions.HTTPError(
                "500 Server Error",
                response=response,
            )

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)

    core.process_items(queue.Queue(), query_ids=[query_id])
    assert db.get_parameter("scraper_failed_cycles") in (None, "0")

    # The legacy all-query entry point still represents one complete sweep.
    core.process_items(queue.Queue())
    assert db.get_parameter("scraper_failed_cycles") == "1"


def test_successful_scrape_clears_persisted_cooldown(database, monkeypatch):
    core = _core()
    core._clear_scraper_cooldown()
    db.set_parameter("quiet_hours_enabled", "False")
    db.set_parameters(
        {
            "scraper_cooldown_until": "999",
            "scraper_cooldown_level": "3",
            "scraper_last_block_status": "403",
            "scraper_failed_cycles": "2",
        }
    )

    conn = db.get_db_connection()
    try:
        conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            (
                "https://www.vinted.co.uk/catalog?search_text=recovery",
                "Recovery",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class Items:
        def search(self, url, nbr_items):
            return []

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    monkeypatch.setattr(core.time, "time", lambda: 1_000)

    core.process_items(queue.Queue())

    cooldown = core.get_scraper_cooldown(now=1_000)
    assert cooldown["level"] == 0
    assert cooldown["status_code"] is None
    assert not cooldown["active"]
    assert not core.get_scraper_health(now=1_000)["blocked"]


def test_scraper_health_reports_stall_block_and_recovery(database):
    core = _core()
    core._clear_scraper_cooldown()
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
    # Preserve an existing alert across restart; stable-recovery logic clears
    # it later without sending a false duplicate warning.
    assert db.get_parameter("scraper_watchdog_alerted") == "True"
    assert db.get_parameter("scraper_watchdog_recovery_started") == "0"
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


def test_watchdog_requires_stable_recovery_before_rearming(database, monkeypatch):
    import vinted_notifications as app

    core = _core()
    db.set_parameters(
        {
            "query_refresh_delay": "300",
            "telegram_enabled": "True",
            "telegram_chat_id": "111",
            "scraper_watchdog_alerted": "False",
            "scraper_watchdog_recovery_started": "0",
        }
    )
    clock = [1_000]
    monkeypatch.setattr(app.time, "time", lambda: clock[0])
    enqueued = []
    monkeypatch.setattr(
        app.db,
        "enqueue_notification",
        lambda *args, **kwargs: enqueued.append((args, kwargs)),
    )

    blocked = {
        "stalled": False,
        "blocked": True,
        "cooldown_active": True,
        "cooldown_remaining": 300,
        "cooldown_level": 1,
        "last_block_status": 403,
        "failed_cycles": 1,
    }
    healthy = {"stalled": False, "blocked": False}
    state = [blocked]
    monkeypatch.setattr(core, "get_scraper_health", lambda: state[0])

    app.check_scraper_watchdog()
    assert len(enqueued) == 1
    assert db.get_parameter("scraper_watchdog_alerted") == "True"

    # A brief recovery followed by another block does not emit a recovery or
    # a second warning.
    state[0] = healthy
    clock[0] += 60
    app.check_scraper_watchdog()
    state[0] = blocked
    clock[0] += 60
    app.check_scraper_watchdog()
    assert len(enqueued) == 1

    # Only a sustained healthy period clears/rearms the alert.
    state[0] = healthy
    clock[0] += 60
    app.check_scraper_watchdog()
    clock[0] += app._watchdog_recovery_window_seconds()
    app.check_scraper_watchdog()
    assert len(enqueued) == 2
    assert db.get_parameter("scraper_watchdog_alerted") == "False"


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
    # (id, content, url, button_text, chat_ids_json, query_id, attempts,
    #  ignore_query_pause)
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


def test_notification_outbox_acknowledges_each_recipient(database):
    assert db.migrate_pending_notifications_table()
    notification_id = db.enqueue_notification(
        "hello",
        "http://x",
        "Open",
        ["111", "222", "111"],
    )

    assert db.ack_notification_recipient(notification_id, "111") == 1
    row = db.get_due_notifications(limit=10)[0]
    assert json.loads(row[4]) == ["222"]
    assert db.count_pending_notifications() == 1

    assert db.ack_notification_recipient(notification_id, "222") == 0
    assert db.count_pending_notifications() == 0


def test_strict_telegram_approval_state_distinguishes_database_errors(
    database, monkeypatch
):
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.get_telegram_user_approval_state("111") is True

    assert db.register_telegram_user("222", "Pending")
    assert db.get_telegram_user_approval_state("222") is False
    assert db.get_telegram_user_approval_state("missing") is False

    def unavailable_database():
        raise sqlite3.OperationalError("temporarily unavailable")

    monkeypatch.setattr(db, "get_db_connection", unavailable_database)
    assert db.get_telegram_user_approval_state("111") is None


def test_send_new_post_fails_closed_when_approval_cannot_be_read(database, monkeypatch):
    import asyncio
    from telegram_bot_plugin.telegram_bot import LeRobot, _DELIVERY_INELIGIBLE

    robot = LeRobot.__new__(LeRobot)
    robot.polling_enabled = False
    sent = []

    async def fake_send(chat_id, content, markup):
        sent.append(chat_id)
        return True

    monkeypatch.setattr(robot, "_send_message_with_retries", fake_send)

    async def send_once():
        return await robot.send_new_post(
            "hello", None, None, chat_ids=["111"], query_id=None
        )

    monkeypatch.setattr(db, "get_telegram_user_approval_state", lambda chat_id: None)
    assert asyncio.run(send_once()) is None
    assert sent == []

    monkeypatch.setattr(db, "get_telegram_user_approval_state", lambda chat_id: False)
    assert asyncio.run(send_once()) is _DELIVERY_INELIGIBLE
    assert sent == []

    monkeypatch.setattr(db, "get_telegram_user_approval_state", lambda chat_id: True)
    assert asyncio.run(send_once()) is True
    assert sent == ["111"]


def test_outbox_retains_recipient_when_approval_read_fails(database, monkeypatch):
    import asyncio
    from telegram_bot_plugin.telegram_bot import LeRobot

    assert db.migrate_pending_notifications_table()
    notification_id = db.enqueue_notification("hello", None, None, ["111"])
    robot = LeRobot.__new__(LeRobot)
    robot.polling_enabled = False
    sent = []

    async def fake_send(chat_id, content, markup):
        sent.append(chat_id)
        return True

    monkeypatch.setattr(robot, "_send_message_with_retries", fake_send)
    approval = [None]
    monkeypatch.setattr(
        db,
        "get_telegram_user_approval_state",
        lambda chat_id: approval[0],
    )

    asyncio.run(robot.drain_outbox(None))
    row = db.get_due_notifications(limit=10)[0]
    assert row[0] == notification_id
    assert json.loads(row[4]) == ["111"]
    assert row[6] == 0
    assert sent == []

    # A definitive revocation is intentionally acknowledged and removed.
    approval[0] = False
    asyncio.run(robot.drain_outbox(None))
    assert db.count_pending_notifications() == 0
    assert sent == []


def test_outbox_retries_only_the_recipient_that_failed(database, monkeypatch):
    import asyncio
    from telegram_bot_plugin.telegram_bot import LeRobot

    assert db.migrate_pending_notifications_table()
    notification_id = db.enqueue_notification(
        "hello",
        "http://x",
        "Open",
        ["111", "222"],
    )
    robot = LeRobot.__new__(LeRobot)
    calls = []
    second_recipient_succeeds = False

    async def fake_send(content, url, button_text, chat_ids=None, query_id=None):
        chat_id = chat_ids[0]
        calls.append(chat_id)
        return chat_id == "111" or second_recipient_succeeds

    monkeypatch.setattr(robot, "send_new_post", fake_send)
    asyncio.run(robot.drain_outbox(None))

    conn = db.get_db_connection()
    try:
        row = conn.execute(
            "SELECT chat_ids, attempts FROM pending_notifications WHERE id=?",
            (notification_id,),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0]) == ["222"]
    assert row[1] == 1
    assert calls == ["111", "222"]

    # Make the failed recipient due immediately and let the retry succeed.
    db.reschedule_notification(notification_id, attempts=1, next_attempt_at=0)
    second_recipient_succeeds = True
    asyncio.run(robot.drain_outbox(None))
    assert calls == ["111", "222", "222"]
    assert db.count_pending_notifications() == 0


def test_outbox_keeps_notification_when_eligibility_read_fails(database, monkeypatch):
    import asyncio
    import telegram_bot_plugin.telegram_bot as plugin

    assert db.migrate_pending_notifications_table()
    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    query_id, _, _ = db.add_query_to_db(
        normalise_vinted_url(
            "https://www.vinted.co.uk/catalog?search_text=eligibility-error"
        ),
        chat_id="111",
    )
    notification_id = db.enqueue_notification(
        "hello", "http://x", "Open", ["111"], query_id=query_id
    )
    robot = plugin.LeRobot.__new__(plugin.LeRobot)
    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(kwargs.get("chat_ids"))
        return True

    monkeypatch.setattr(robot, "send_new_post", fake_send)
    monkeypatch.setattr(db, "get_query_delivery_state", lambda query_id: None)
    asyncio.run(robot.drain_outbox(None))

    assert calls == []
    assert db.count_pending_notifications() == 1
    row = db.get_due_notifications(limit=10)[0]
    assert row[0] == notification_id
    assert row[6] == 0


def test_outbox_never_guesses_a_recipient_for_corrupt_json(database, monkeypatch):
    import asyncio
    import telegram_bot_plugin.telegram_bot as plugin

    assert db.migrate_pending_notifications_table()
    db.set_parameter("telegram_chat_id", "admin-chat")
    notification_id = db.enqueue_notification(
        "hello", "http://x", "Open", ["intended-chat"]
    )
    conn = db.get_db_connection()
    try:
        conn.execute(
            "UPDATE pending_notifications SET chat_ids=? WHERE id=?",
            ("{not-json", notification_id),
        )
        conn.commit()
    finally:
        conn.close()

    robot = plugin.LeRobot.__new__(plugin.LeRobot)
    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(kwargs.get("chat_ids"))
        return True

    monkeypatch.setattr(robot, "send_new_post", fake_send)
    asyncio.run(robot.drain_outbox(None))

    assert calls == []
    conn = db.get_db_connection()
    try:
        row = conn.execute(
            "SELECT chat_ids, attempts FROM pending_notifications WHERE id=?",
            (notification_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("{not-json", 1)


@pytest.mark.parametrize("failed_write", ["reschedule", "delete"])
def test_outbox_stops_when_retry_state_cannot_be_saved(
    database, monkeypatch, failed_write
):
    import asyncio
    import telegram_bot_plugin.telegram_bot as plugin

    assert db.migrate_pending_notifications_table()
    notification_id = db.enqueue_notification("hello", "http://x", "Open", ["111"])
    if failed_write == "delete":
        db.reschedule_notification(notification_id, attempts=9, next_attempt_at=0)
        monkeypatch.setattr(db, "delete_notification", lambda notification_id: False)
    else:
        monkeypatch.setattr(
            db,
            "reschedule_notification",
            lambda notification_id, attempts, next_attempt_at: False,
        )

    robot = plugin.LeRobot.__new__(plugin.LeRobot)
    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(kwargs.get("chat_ids"))
        return False

    monkeypatch.setattr(robot, "send_new_post", fake_send)
    asyncio.run(robot.drain_outbox(None))

    # A failed state write returns from the pass instead of immediately
    # re-fetching and re-sending the same due row forever.
    assert calls == [["111"]]
    assert db.count_pending_notifications() == 1


def test_outbox_cancellation_keeps_only_unacknowledged_recipients(
    database, monkeypatch
):
    import asyncio
    import telegram_bot_plugin.telegram_bot as plugin

    assert db.migrate_pending_notifications_table()
    notification_id = db.enqueue_notification(
        "hello", "http://x", "Open", ["111", "222"]
    )
    robot = plugin.LeRobot.__new__(plugin.LeRobot)
    calls = []

    async def fake_send(*args, **kwargs):
        chat_id = kwargs["chat_ids"][0]
        calls.append(chat_id)
        if chat_id == "222":
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(robot, "send_new_post", fake_send)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(robot.drain_outbox(None))

    assert calls == ["111", "222"]
    conn = db.get_db_connection()
    try:
        row = conn.execute(
            "SELECT chat_ids, attempts FROM pending_notifications WHERE id=?",
            (notification_id,),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0]) == ["222"]
    assert row[1] == 0


def test_outbox_migration_adds_query_id_to_existing_table(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-outbox.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("""
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
            """)
        conn.commit()

    assert db.migrate_pending_notifications_table()
    with closing(sqlite3.connect(database_path)) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(pending_notifications)")
        }
    assert "query_id" in columns


def test_telegram_subscription_button_toggles_only_clicking_user(
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

        sister_markup = next(markup for chat_id, _, markup in sent if chat_id == "222")
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
            def __init__(self, data, markup):
                self.data = data
                self.message = SimpleNamespace(reply_markup=markup)
                self.answers = []
                self.edited_markup = markup

            async def answer(self, text, show_alert=False):
                self.answers.append((text, show_alert))

            async def edit_message_reply_markup(self, markup):
                self.edited_markup = markup

        callback = Callback(f"unsubscribe:{query_id}", sister_markup)
        update = SimpleNamespace(
            callback_query=callback,
            effective_chat=SimpleNamespace(id=222),
        )
        await robot.unsubscribe_query(update, None)
        assert set(db.get_query_subscribers(query_id)) == {"111"}
        assert callback.answers == [("Unsubscribed from this search.", True)]
        action_buttons = [
            button
            for row in callback.edited_markup.inline_keyboard
            for button in row
            if button.callback_data
            and button.callback_data.startswith(("unsubscribe:", "resubscribe:"))
        ]
        assert [button.callback_data for button in action_buttons] == [
            f"resubscribe:{query_id}"
        ]
        assert [button.text for button in action_buttons] == [
            "Resubscribe to this search"
        ]

        callback.message.reply_markup = callback.edited_markup
        callback.data = f"resubscribe:{query_id}"
        callback.answers = []
        await robot.resubscribe_query(update, None)
        return callback

    callback = asyncio.run(exercise_button())

    assert set(db.get_query_subscribers(query_id)) == {"111", "222"}
    assert callback.answers == [("Resubscribed to this search.", True)]
    remaining_callbacks = [
        button.callback_data
        for row in callback.edited_markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert remaining_callbacks == [f"unsubscribe:{query_id}"]
    subscription_labels = [
        button.text
        for row in callback.edited_markup.inline_keyboard
        for button in row
        if button.callback_data
        and button.callback_data.startswith(("unsubscribe:", "resubscribe:"))
    ]
    assert subscription_labels == ["Unsubscribe from this search"]


def test_last_subscriber_can_unsubscribe_and_resubscribe(database, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from telegram_bot_plugin.telegram_bot import LeRobot

    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    query_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=last-subscriber"
    )
    query_id, _, subscribed = db.add_query_to_db(query_url, chat_id="111")
    assert subscribed

    conn = db.get_db_connection()
    try:
        conn.execute(
            "INSERT INTO items "
            "(item, title, price, currency, timestamp, query_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (99, "Saved item", 10, "GBP", 100, query_id),
        )
        conn.commit()
    finally:
        conn.close()

    robot = LeRobot.__new__(LeRobot)
    robot.polling_enabled = True
    markups = []

    async def fake_send(chat_id, content, markup):
        markups.append(markup)
        return True

    monkeypatch.setattr(robot, "_send_message_with_retries", fake_send)

    class Callback:
        def __init__(self, data, markup):
            self.data = data
            self.message = SimpleNamespace(reply_markup=markup)
            self.answers = []
            self.edited_markup = markup

        async def answer(self, text, show_alert=False):
            self.answers.append((text, show_alert))

        async def edit_message_reply_markup(self, markup):
            self.edited_markup = markup

    async def exercise():
        await robot.send_new_post(
            "New item",
            "https://www.vinted.co.uk/items/99",
            "Open Vinted",
            chat_ids=["111"],
            query_id=query_id,
        )
        callback = Callback(f"unsubscribe:{query_id}", markups[0])
        update = SimpleNamespace(
            callback_query=callback,
            effective_chat=SimpleNamespace(id=111),
        )
        await robot.unsubscribe_query(update, None)
        assert callback.answers == [("Unsubscribed from this search.", True)]

        callback.message.reply_markup = callback.edited_markup
        callback.data = f"resubscribe:{query_id}"
        callback.answers = []
        await robot.resubscribe_query(update, None)
        return callback

    callback = asyncio.run(exercise())
    assert callback.answers == [("Resubscribed to this search.", True)]
    assert db.get_query_subscribers(query_id) == ["111"]
    # Personal subscription changes preserve the shared query and item history.
    conn = db.get_db_connection()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE query_id=?", (query_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_subscription_button_does_not_change_when_database_update_fails(
    database, monkeypatch
):
    import asyncio
    from types import SimpleNamespace
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram_bot_plugin.telegram_bot import LeRobot

    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    robot = LeRobot.__new__(LeRobot)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Unsubscribe", callback_data="unsubscribe:7")]]
    )

    class Callback:
        def __init__(self, data):
            self.data = data
            self.message = SimpleNamespace(reply_markup=markup)
            self.answers = []
            self.edits = []

        async def answer(self, text, show_alert=False):
            self.answers.append((text, show_alert))

        async def edit_message_reply_markup(self, new_markup):
            self.edits.append(new_markup)

    async def exercise():
        unsubscribe = Callback("unsubscribe:7")
        update = SimpleNamespace(
            callback_query=unsubscribe,
            effective_chat=SimpleNamespace(id=111),
        )
        monkeypatch.setattr(db, "remove_query_subscription", lambda *args: None)
        await robot.unsubscribe_query(update, None)

        resubscribe = Callback("resubscribe:7")
        update.callback_query = resubscribe
        monkeypatch.setattr(db, "add_query_subscription", lambda *args: None)
        await robot.resubscribe_query(update, None)
        return unsubscribe, resubscribe

    unsubscribe, resubscribe = asyncio.run(exercise())
    assert "try again" in unsubscribe.answers[0][0].lower()
    assert "try again" in resubscribe.answers[0][0].lower()
    assert unsubscribe.edits == []
    assert resubscribe.edits == []


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


def test_subscribed_item_is_persisted_to_outbox_atomically(database):
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

    assert db.migrate_pending_notifications_table()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    db.set_parameter("telegram_chat_id", "123")
    assert db.migrate_multi_user_schema()
    query_id, _, subscribed = db.add_query_to_db(
        normalise_vinted_url(
            "https://www.vinted.co.uk/catalog?search_text=atomic-coat"
        ),
        chat_id="123",
    )
    assert subscribed

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)

    due = db.get_due_notifications(limit=10)
    assert len(due) == 1
    (
        _,
        content,
        url,
        button_text,
        chat_ids_json,
        queued_query_id,
        attempts,
        ignore_query_pause,
    ) = due[0]
    assert json.loads(chat_ids_json) == ["123"]
    assert queued_query_id == query_id
    assert url == "https://www.vinted.co.uk/items/77"
    assert button_text == "Open Vinted"
    assert attempts == 0
    assert ignore_query_pause == 0
    assert "Wool Coat" in content
    conn = db.get_db_connection()
    try:
        item = conn.execute("SELECT item, query_id FROM items WHERE item=77").fetchone()
        last_item = conn.execute(
            "SELECT last_item FROM queries WHERE id=?", (query_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert item == (77, query_id)
    assert last_item == 100
    # RSS still receives the item on the in-memory queue.
    rss_item = destination.get_nowait()
    assert len(rss_item) == 6


def test_outbox_insert_failure_rolls_back_item_and_timestamp(database):
    core = _core()

    class Item:
        id = 78
        title = "Rollback Coat"
        price = 31
        currency = "GBP"
        brand_title = "Brand"
        condition = "Very good"
        description = None
        photo = None
        url = "https://www.vinted.co.uk/items/78"
        raw_timestamp = 101
        raw_data = {"user": {"id": 1}}

    assert db.migrate_pending_notifications_table()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    db.set_parameter("telegram_chat_id", "123")
    assert db.migrate_multi_user_schema()
    query_id, _, subscribed = db.add_query_to_db(
        normalise_vinted_url(
            "https://www.vinted.co.uk/catalog?search_text=rollback-coat"
        ),
        chat_id="123",
    )
    assert subscribed
    conn = db.get_db_connection()
    try:
        conn.execute("""
            CREATE TRIGGER fail_pending_notification
            BEFORE INSERT ON pending_notifications
            BEGIN
                SELECT RAISE(ABORT, 'forced outbox failure');
            END;
            """)
        conn.commit()
    finally:
        conn.close()

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)

    assert destination.empty()
    assert db.count_pending_notifications() == 0
    conn = db.get_db_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT last_item FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_persistence_failure_stops_the_remaining_item_batch(database, monkeypatch):
    core = _core()

    class Item:
        title = "Retry me"
        price = 10
        currency = "GBP"
        brand_title = "Brand"
        condition = "Good"
        description = None
        photo = None
        raw_data = {"user": {"id": 1}}

        def __init__(self, item_id, timestamp):
            self.id = item_id
            self.raw_timestamp = timestamp
            self.url = f"https://www.vinted.co.uk/items/{item_id}"

    conn = db.get_db_connection()
    try:
        query_id = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=retry", "Retry"),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    attempts = []

    def fail_persistence(**kwargs):
        attempts.append(kwargs["id"])
        return None

    monkeypatch.setattr(core.db, "persist_item_and_notification", fail_persistence)
    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item(2, 200), Item(1, 100)], query_id))
    core.clear_item_queue(source, destination)

    assert attempts == [1]
    assert destination.empty()


def test_subscriber_read_failure_does_not_mark_item_seen(database):
    core = _core()

    class Item:
        id = 79
        title = "Database retry coat"
        price = 15
        currency = "GBP"
        brand_title = "Brand"
        condition = "Good"
        description = None
        photo = None
        url = "https://www.vinted.co.uk/items/79"
        raw_timestamp = 102
        raw_data = {"user": {"id": 1}}

    assert db.migrate_pending_notifications_table()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    conn = db.get_db_connection()
    try:
        query_id = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            (
                "https://www.vinted.co.uk/catalog?search_text=subscriber-read",
                "Subscriber read",
            ),
        ).lastrowid
        conn.execute("DROP TABLE query_subscriptions")
        conn.commit()
    finally:
        conn.close()

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)

    assert destination.empty()
    assert db.count_pending_notifications() == 0
    conn = db.get_db_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT last_item FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_atomic_item_writer_rechecks_a_paused_query(database, monkeypatch):
    core = _core()

    class Item:
        id = 80
        title = "Paused race coat"
        price = 20
        currency = "GBP"
        brand_title = "Brand"
        condition = "Good"
        description = None
        photo = None
        url = "https://www.vinted.co.uk/items/80"
        raw_timestamp = 103
        raw_data = {"user": {"id": 1}}

    assert db.migrate_pending_notifications_table()
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    conn = db.get_db_connection()
    try:
        query_id = conn.execute(
            "INSERT INTO queries (query, query_name, enabled) VALUES (?, ?, 0)",
            (
                "https://www.vinted.co.uk/catalog?search_text=paused-race",
                "Paused race",
            ),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    # Simulate the query being paused after the outer queue checks but before
    # the transactional writer acquires its lock and rechecks the row.
    monkeypatch.setattr(core.db, "is_query_enabled", lambda query_id: True)
    source = queue.Queue()
    destination = queue.Queue()
    source.put(([Item()], query_id))
    core.clear_item_queue(source, destination)

    assert destination.empty()
    assert db.count_pending_notifications() == 0
    conn = db.get_db_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT last_item FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_paused_query_discards_results_already_waiting_in_item_queue(database):
    core = _core()
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries (query, query_name, enabled) VALUES (?, ?, 0)",
            ("https://www.vinted.co.uk/catalog?search_text=paused", "Paused"),
        )
        query_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([object()], query_id))

    core.clear_item_queue(source, destination)

    assert destination.empty()
    conn = db.get_db_connection()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_outbox_rechecks_pause_and_current_subscribers(database):
    from telegram_bot_plugin.telegram_bot import _eligible_outbox_chat_ids

    db.set_parameter("telegram_chat_id", "111")
    assert db.migrate_multi_user_schema()
    assert db.approve_telegram_user("222", "Sister")
    query_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=queue-state"
    )
    query_id, _, _ = db.add_query_to_db(query_url, chat_id="111")
    db.add_query_to_db(query_url, chat_id="222")

    assert _eligible_outbox_chat_ids(query_id, ["111", "222"]) == ["111", "222"]

    # Unsubscribing after the item was queued removes only that recipient.
    assert db.remove_query_subscription(query_id, "222")
    assert _eligible_outbox_chat_ids(query_id, ["111", "222"]) == ["111"]

    # Pausing the shared query suppresses the queued alert for everyone.
    assert db.set_query_enabled(query_id, False)
    assert _eligible_outbox_chat_ids(query_id, ["111"]) == []


def test_retryable_telegram_error_classification():
    from telegram.error import NetworkError, TimedOut, BadRequest
    from telegram_bot_plugin.telegram_bot import is_retryable_telegram_error

    # Transient network problems are retried.
    assert is_retryable_telegram_error(NetworkError("Bad Gateway"))
    assert is_retryable_telegram_error(TimedOut())
    # Permanent client errors are not.
    assert not is_retryable_telegram_error(BadRequest("chat not found"))
    assert not is_retryable_telegram_error(ValueError("boom"))


def test_queries_page_has_search_sort_pagination_and_shared_modals(
    database, monkeypatch
):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://github.com/x/y"),
    )
    assert db.migrate_pending_notifications_table()

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
    assert 'id="bulkPauseButton"' in html
    assert 'formaction="/pause_query/bulk"' in html
    assert 'id="bulkResumeButton"' in html
    assert 'formaction="/resume_query/bulk"' in html
    assert 'id="bulkRemoveButton"' in html
    assert 'action="/remove_query/bulk"' in html
    assert 'data-sort-key="items"' in html
    assert "data-item-count=" in html
    assert "query-toggle" in html
    assert "data-relative-time=" in html
    assert 'id="queryStatusFilter"' in html
    assert 'id="queryMobileSort"' in html
    assert 'id="querySelectAllMobile"' in html
    assert "data-enabled=" in html
    assert 'id="toggleAddQuery"' in html

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
    token = (
        re.search(rb'name="_csrf_token" value="([^"]+)"', token_page.data)
        .group(1)
        .decode()
    )

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

    # Bulk-pause A and C in one request; B remains active.
    response = client.post(
        "/pause_query/bulk",
        data={"_csrf_token": token, "query_ids": [str(qid_a), str(qid_c)]},
    )
    assert response.status_code == 302
    enabled = db.get_query_enabled_map()
    assert enabled == {qid_a: False, qid_b: True, qid_c: False}
    assert {row[0] for row in db.get_queries(enabled_only=True)} == {qid_b}

    # Repeating the action is harmless and keeps both queries paused.
    response = client.post(
        "/pause_query/bulk",
        data={"_csrf_token": token, "query_ids": [str(qid_a), str(qid_c)]},
    )
    assert response.status_code == 302
    assert db.get_query_enabled_map() == enabled

    # The matching bulk action resumes both paused queries without touching B.
    response = client.post(
        "/resume_query/bulk",
        data={"_csrf_token": token, "query_ids": [str(qid_a), str(qid_c)]},
    )
    assert response.status_code == 302
    assert all(db.get_query_enabled_map().values())

    # Pause A and C again for the existing bulk-removal assertion below.
    db.set_queries_enabled([qid_a, qid_c], False)

    # Bulk-remove A and C, leaving only B.
    response = client.post(
        "/remove_query/bulk",
        data={"_csrf_token": token, "query_ids": [str(qid_a), str(qid_c)]},
    )
    assert response.status_code == 302
    assert {row[0] for row in db.get_queries()} == {qid_b}


def test_scrape_cycle_honours_query_paused_after_cycle_started(database, monkeypatch):
    core = _core()
    db.set_parameter("quiet_hours_enabled", "False")
    db.set_parameter("query_refresh_delay", "300")

    conn = db.get_db_connection()
    try:
        first = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=first", "First"),
        ).lastrowid
        second = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=second", "Second"),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    search_calls = []

    class Items:
        def search(self, url, nbr_items):
            search_calls.append(url)
            db.set_query_enabled(second, False)
            return []

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(core.random, "uniform", lambda low, high: 1.0)

    core.process_items(queue.Queue())

    assert len(search_calls) == 1
    assert "search_text=first" in search_calls[0]
    assert db.is_query_enabled(first)
    assert not db.is_query_enabled(second)


def test_network_web_bind_requires_auth_and_persistent_secret(monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "")
    monkeypatch.setattr(web, "WEB_PASSWORD", "")
    monkeypatch.setattr(web, "WEB_SECRET_KEY", "")
    web._validate_web_bind_security("127.0.0.1")
    web._validate_web_bind_security("::1")
    with pytest.raises(RuntimeError, match="without authentication"):
        web._validate_web_bind_security("0.0.0.0")

    monkeypatch.setattr(web, "WEB_USERNAME", "admin")
    monkeypatch.setattr(web, "WEB_PASSWORD", "correct-horse")
    with pytest.raises(RuntimeError, match="persistent VN_SECRET_KEY"):
        web._validate_web_bind_security("0.0.0.0")

    monkeypatch.setattr(web, "WEB_SECRET_KEY", "persistent-test-secret")
    web._validate_web_bind_security("0.0.0.0")


def test_repeated_web_auth_failures_are_throttled(monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "admin")
    monkeypatch.setattr(web, "WEB_PASSWORD", "correct-horse")
    monkeypatch.setattr(web, "AUTH_MAX_FAILURES", 3)
    monkeypatch.setattr(web, "AUTH_BLOCK_SECONDS", 60)
    with web._auth_lock:
        web._auth_failures.clear()
        web._auth_blocked_until.clear()

    client = web.app.test_client()
    asset = "/static/vendor/bootstrap/bootstrap.bundle.min.js"
    wrong_headers = _basic_header(password="wrong-password")
    assert client.get(asset, headers=wrong_headers).status_code == 401
    assert client.get(asset, headers=wrong_headers).status_code == 401
    blocked = client.get(asset, headers=wrong_headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    # Correct credentials always recover the legitimate administrator.
    assert client.get(asset, headers=_basic_header()).status_code == 200
    assert client.get(asset, headers=wrong_headers).status_code == 401


def test_https_mode_adds_hsts(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_HTTPS_ENABLED", True)
    response = web.app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_dynamic_configuration_content_is_rendered_as_text():
    config = (ROOT / "web_ui_plugin/templates/config.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "web_ui_plugin/templates/index.html").read_text(
        encoding="utf-8"
    )

    assert "templatePreview.textContent = rendered" in config
    assert "templatePreview.innerHTML" not in config
    assert "tag.innerHTML" not in config
    assert "document.createTextNode(String(message))" in config
    assert "${data.message}" not in config
    assert "${data.message}" not in dashboard
    assert "document.createTextNode(String(data.message))" in dashboard


def test_secret_redaction_covers_telegram_tokens_and_proxy_passwords():
    from logger import redact_secrets

    # Build the token shape at runtime so secret scanners do not mistake this
    # regression fixture for a committed live credential.
    token = "123456789:" + ("A" * 35)
    message = (
        f"POST https://api.telegram.org/bot{token}/sendMessage via "
        "http://proxy-user:proxy-password@proxy.example:8080"
    )
    redacted = redact_secrets(message)
    assert token not in redacted
    assert "proxy-user" not in redacted
    assert "proxy-password" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_rss_token_protects_feed(database, monkeypatch):
    from rss_feed_plugin.rss_feed import RSSFeed

    monkeypatch.delenv("VN_RSS_TOKEN", raising=False)
    unconfigured_feed = RSSFeed(queue.Queue())
    assert unconfigured_feed.app.test_client().get("/").status_code == 503

    monkeypatch.setenv("VN_RSS_TOKEN", "long-private-rss-token")
    feed = RSSFeed(queue.Queue())
    client = feed.app.test_client()

    denied = client.get("/")
    assert denied.status_code == 401
    assert denied.headers["WWW-Authenticate"].startswith("Bearer")

    response = client.get("/?token=long-private-rss-token")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    response = client.get(
        "/",
        headers={"Authorization": "Bearer long-private-rss-token"},
    )
    assert response.status_code == 200


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
    token = (
        re.search(rb'name="_csrf_token" value="([^"]+)"', client.get("/queries").data)
        .group(1)
        .decode()
    )

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
    assert 'min="60"' in html_text  # query refresh delay
    assert 'min="1"' in html_text  # items per query


def test_config_health_endpoint(database):
    import web_ui_plugin.web_ui as web

    assert db.migrate_pending_notifications_table()
    data = web.app.test_client().get("/config/health").get_json()
    assert "pending_notifications" in data
    assert data["scraper"]["status"] in {"ok", "stalled", "blocked"}
    assert {
        "cooldown_active",
        "cooldown_remaining",
        "cooldown_level",
        "last_block_status",
    }.issubset(data["scraper"])
    assert set(data["queries"]) == {"total", "active", "paused"}


def test_test_telegram_route(database, monkeypatch, caplog):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    client = web.app.test_client()
    token = (
        re.search(rb'name="_csrf_token" value="([^"]+)"', client.get("/config").data)
        .group(1)
        .decode()
    )

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

    # Neither transport exceptions nor Telegram's response body may echo the
    # token-bearing API URL back into the browser.
    import requests as requests_module

    configured_token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
    db.set_parameter("telegram_token", configured_token)

    def connection_failure(*args, **kwargs):
        raise requests_module.ConnectionError(
            f"failed https://api.telegram.org/bot{configured_token}/sendMessage"
        )

    monkeypatch.setattr(web.requests, "post", connection_failure)
    response = client.post("/test_telegram", headers={"X-CSRF-Token": token})
    assert response.status_code == 502
    assert configured_token not in response.get_json()["message"]
    assert "api.telegram.org" not in response.get_json()["message"]
    assert configured_token not in caplog.text

    class LeakyRejectResponse:
        status_code = 400

        def json(self):
            return {
                "ok": False,
                "description": f"invalid token {configured_token}",
            }

    monkeypatch.setattr(web.requests, "post", lambda *a, **k: LeakyRejectResponse())
    response = client.post("/test_telegram", headers={"X-CSRF-Token": token})
    assert response.status_code == 400
    assert configured_token not in response.get_json()["message"]
    assert "[REDACTED]" in response.get_json()["message"]

    class InvalidShapeResponse:
        status_code = 200

        def json(self):
            return ["not", "a", "Telegram", "object"]

    monkeypatch.setattr(web.requests, "post", lambda *a, **k: InvalidShapeResponse())
    response = client.post("/test_telegram", headers={"X-CSRF-Token": token})
    assert response.status_code == 502
    assert response.get_json()["status"] == "error"
    assert configured_token not in response.get_json()["message"]


def test_items_filter_sort_and_pagination(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )

    conn = db.get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO queries (query, last_item, query_name) VALUES (?, ?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=nike", 100, "Nike"),
        )
        qid = cur.lastrowid
        cur.executemany(
            "INSERT INTO items (item, title, price, currency, timestamp, photo_url, query_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Nike Air", 25.0, "GBP", 1000, None, qid),
                (2, "Nike Boots", 10.0, "GBP", 2000, None, qid),
                (3, "Adidas Cap", 5.0, "GBP", 3000, None, qid),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    # Title search + price filters.
    assert db.count_items(search="nike") == 2
    assert db.count_items(price_min=10) == 2
    assert db.count_items(price_max=10) == 2
    assert db.count_items(price_min=10, price_max=25) == 2

    # Sort ascending by price.
    prices = [row[2] for row in db.get_items(sort="price_asc")]
    assert prices == sorted(prices)

    # Pagination via limit/offset.
    page1 = db.get_items(limit=2, offset=0, sort="price_asc")
    page2 = db.get_items(limit=2, offset=2, sort="price_asc")
    assert len(page1) == 2 and len(page2) == 1

    # The route renders the new controls and preserves the sort selection.
    html_text = (
        web.app.test_client().get("/items?search=nike&sort=price_asc").data.decode()
    )
    assert 'name="search"' in html_text
    assert 'name="price_min"' in html_text
    assert 'name="sort"' in html_text
    assert "data-relative-time" in html_text
    assert "Page 1 of" in html_text
    assert 'value="price_asc" selected' in html_text
    assert "item-result-card" in html_text
    assert "item-query-meta" in html_text
    assert "data-image-fallback" in html_text


def test_dark_mode_toggle_is_wired():
    template = (ROOT / "web_ui_plugin" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    assert 'id="themeToggle"' in template  # header toggle button
    assert "vintedTheme" in template  # preference persisted to localStorage
    assert "prefers-color-scheme" in template  # OS default applied before paint
    assert '[data-bs-theme="dark"]' in template  # dark overrides for custom elements


def test_frontend_assets_are_self_hosted_and_image_fallback_is_wired():
    template = (ROOT / "web_ui_plugin" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    assert "cdn.jsdelivr.net" not in template
    assert "vendor/bootstrap/bootstrap.min.css" in template
    assert "vendor/bootstrap/bootstrap.bundle.min.js" in template
    assert "vendor/bootstrap-icons/bootstrap-icons.css" in template
    assert "data-image-fallback" in template
    assert "Image unavailable" in template

    required_assets = [
        ROOT / "web_ui_plugin/static/vendor/bootstrap/bootstrap.min.css",
        ROOT / "web_ui_plugin/static/vendor/bootstrap/bootstrap.bundle.min.js",
        ROOT / "web_ui_plugin/static/vendor/bootstrap-icons/bootstrap-icons.css",
        ROOT
        / "web_ui_plugin/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
        ROOT / "web_ui_plugin/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff",
    ]
    assert all(
        path.is_file() and path.stat().st_size > 1000 for path in required_assets
    )


def test_base_template_declares_local_favicon():
    template = (ROOT / "web_ui_plugin" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    favicon = ROOT / "web_ui_plugin" / "static" / "favicon.svg"

    assert "filename='favicon.svg'" in template
    assert favicon.is_file()
    assert "#11a8b5" in favicon.read_text(encoding="utf-8").lower()


def test_responsive_layout_hooks_are_present():
    base = (ROOT / "web_ui_plugin" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    items = (ROOT / "web_ui_plugin" / "templates" / "items.html").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "web_ui_plugin" / "static" / "css" / "custom.css").read_text(
        encoding="utf-8"
    )

    assert "mobile-nav-toggle" in base
    assert "brand-short" in base
    assert "col-md-10" in base
    assert "items-results-header" in items
    assert "items-pagination" in items
    assert "item-result-card" in items
    assert "@media (max-width: 768px)" in css
    assert "min-height: 44px" in css
    assert "#queriesTable tr[data-query-row]" in css
    assert "grid-template-columns: 7.25rem" in css
    assert "prefers-reduced-motion: reduce" in css


def test_frontend_and_dependency_audit_use_supported_release_paths():
    bootstrap_css = (
        ROOT / "web_ui_plugin" / "static" / "vendor" / "bootstrap" / "bootstrap.min.css"
    ).read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/linter.yml").read_text(encoding="utf-8")

    assert re.search(r"Bootstrap\s+v5\.3\.8", bootstrap_css)
    assert "5.3.0-alpha1" not in bootstrap_css
    assert "pypa/gh-action-pip-audit@v1.1.0" in workflow
    assert "frances/current-working-version" in workflow
    assert 'cron: "17 4 * * 1"' in workflow


def test_container_and_manual_deployment_are_private_by_default():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert '"127.0.0.1:${VN_WEB_PORT:-8000}:8000"' in compose
    assert '"127.0.0.1:${VN_RSS_PORT:-8080}:8080"' in compose
    assert "read_only: true" in compose
    assert "tmpfs:" in compose
    assert "umask 0077" in entrypoint
    assert 'find "$directory" -type d -exec chmod 700 {} +' in entrypoint
    assert 'find "$directory" -type f -exec chmod 600 {} +' in entrypoint
    assert "-p 127.0.0.1:8000:8000" in deployment
    assert "--read-only" in deployment
    assert "--tmpfs /tmp:rw,noexec,nosuid,size=64m" in deployment


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
    assert "message.textContent = log.message" in template
    assert "? 25 : 50" in template
    assert 'id="logSearchInput"' in template
    assert 'id="logModuleInput"' in template
    assert 'id="logCards"' in template


def test_logs_api_search_module_and_routine_request_filters(
    database, monkeypatch, tmp_path
):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "vinted.log").write_text(
        '2026-07-21 10:00:00,000 - werkzeug - INFO - 127.0.0.1 - "\x1b[36mGET /api/logs HTTP/1.1\x1b[0m" 200 -\n'
        "2026-07-21 10:00:01,000 - core - ERROR - Catalogue request failed\n"
        "2026-07-21 10:00:02,000 - core - INFO - Scrape completed\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    client = web.app.test_client()

    default_data = client.get("/api/logs").get_json()
    assert default_data["total"] == 2
    assert all(entry["module"] == "core" for entry in default_data["logs"])

    all_data = client.get("/api/logs?hide_http=0").get_json()
    assert all_data["total"] == 3
    assert all("\x1b" not in entry["message"] for entry in all_data["logs"])

    error_data = client.get(
        "/api/logs", query_string={"search": "failed", "module": "core"}
    ).get_json()
    assert error_data["total"] == 1
    assert error_data["logs"][0]["level"] == "ERROR"


def test_allowlist_country_picker_uppercases_and_validates(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core, "check_version", lambda: (True, "t", "t", "https://x/y")
    )
    client = web.app.test_client()
    page = client.get("/allowlist")
    token = (
        re.search(rb'name="_csrf_token" value="([^"]+)"', page.data).group(1).decode()
    )
    html = page.data.decode()
    assert 'list="countryOptions"' in html
    assert 'value="GB">United Kingdom' in html
    assert "toUpperCase()" in html

    client.post("/add_country", data={"_csrf_token": token, "country": "1!"})
    assert db.get_allowlist() == 0

    client.post("/add_country", data={"_csrf_token": token, "country": "gb"})
    assert db.get_allowlist() == ["GB"]


def test_configuration_collapses_and_warns_about_unsaved_changes():
    template = (ROOT / "web_ui_plugin/templates/config.html").read_text(
        encoding="utf-8"
    )
    assert 'id="configForm"' in template
    assert template.count("config-section-toggle") >= 6
    assert 'id="configSaveBar"' in template
    assert "Unsaved changes" in template
    assert "beforeunload" in template
    assert "data-collapsed-mobile" in template


def test_dashboard_stat_cards_are_links_and_images_have_fallbacks():
    template = (ROOT / "web_ui_plugin/templates/index.html").read_text(encoding="utf-8")
    partials = "".join(
        (ROOT / "web_ui_plugin/templates" / name).read_text(encoding="utf-8")
        for name in (
            "_dashboard_last_item.html",
            "_dashboard_recent_item_cards.html",
            "_dashboard_recent_item_rows.html",
        )
    )
    assert 'href="/items"' in template
    assert 'href="/queries?status=active"' in template
    assert template.count("dashboard-stat-link") == 3
    assert partials.count("data-image-fallback") >= 3
    assert "stats.active_queries" in template
    assert "stats.paused_queries" in template


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
    assert (
        'class="btn btn-sm btn-outline-secondary dashboard-collapse-toggle"' in template
    )
    assert "vintedDashboardSection:" in template
    assert "data-relative-time" in template
    assert "Intl.RelativeTimeFormat" in template
    assert 'db.get_items(limit=6, sort="discovered_desc")' in web_source
    assert "fetch('/api/dashboard/feed'" in template


def test_dashboard_has_live_system_health_summary():
    template = (ROOT / "web_ui_plugin" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    config = (ROOT / "web_ui_plugin" / "templates" / "config.html").read_text(
        encoding="utf-8"
    )

    assert 'id="dashboardHealthCard"' in template
    assert 'id="dashboardHealthScraper"' in template
    assert 'id="dashboardHealthLastOk"' in template
    assert 'id="dashboardHealthPending"' in template
    assert 'id="dashboardHealthProtection"' in template
    assert "fetch('/config/health'" in template
    assert "cooldown_remaining" in template
    assert 'href="/config#system-health"' in template
    assert 'id="system-health"' in config


def test_real_browser_smoke_csp_xss_and_responsive_layout(database, monkeypatch):
    if os.environ.get("VN_RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set VN_RUN_BROWSER_TESTS=1 to run the browser smoke test")

    playwright_api = pytest.importorskip("playwright.sync_api")
    import threading
    from werkzeug.serving import make_server
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "")
    monkeypatch.setattr(web, "WEB_PASSWORD", "")
    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://github.com/x/y"),
    )

    server = make_server("127.0.0.1", 0, web.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        with playwright_api.sync_playwright() as playwright:
            launch_options = {"headless": True}
            browser_channel = os.environ.get("VN_E2E_BROWSER_CHANNEL", "").strip()
            if browser_channel:
                launch_options["channel"] = browser_channel
            browser = playwright.chromium.launch(**launch_options)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            response = page.goto(
                f"http://127.0.0.1:{server.server_port}/config",
                wait_until="networkidle",
            )
            assert response.status == 200
            assert (
                "script-src 'self' 'nonce-"
                in response.headers["content-security-policy"]
            )
            assert page.evaluate("typeof window.bootstrap !== 'undefined'")
            assert page.locator("h1", has_text="Configuration").count() == 1
            assert page.evaluate(
                "document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth"
            )

            probe = '<img src=x onerror="window.__securityProbe=1">'
            page.locator('[data-config-target="configAdvancedBody"]').click()
            page.locator("#message_template").fill(probe)
            page.wait_for_timeout(100)
            preview = page.locator("#templatePreview")
            assert preview.text_content() == probe
            assert preview.locator("img").count() == 0
            assert not page.evaluate("window.__securityProbe === 1")
            assert page_errors == []
            browser.close()
            browser = None
    finally:
        if browser:
            browser.close()
        server.shutdown()
        thread.join(timeout=5)
