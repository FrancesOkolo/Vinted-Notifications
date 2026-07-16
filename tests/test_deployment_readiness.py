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

    web._validate_message_template("{title} {description}")
    with pytest.raises(ValueError, match="Unsupported"):
        web._validate_message_template("{title} {private_value}")
