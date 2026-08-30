import sqlite3
from pathlib import Path

import pytest

import db

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.configure_database_runtime()
    yield database_path


@pytest.fixture
def web_client(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "")
    monkeypatch.setattr(web, "WEB_PASSWORD", "")
    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://example.invalid"),
    )
    return web.app.test_client()


def _add_query(url, name):
    conn = db.get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            (url, name),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def test_item_discovery_migration_is_idempotent_and_backfills_legacy_rows(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("""
            CREATE TABLE items (
                item NUMERIC,
                title TEXT,
                price NUMERIC,
                currency TEXT,
                timestamp NUMERIC,
                photo_url TEXT,
                query_id INTEGER
            )
            """)
        conn.execute(
            "INSERT INTO items (item, title, timestamp) VALUES (?, ?, ?)",
            (101, "Legacy lamp", 1700000000.5),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.migrate_item_discovery_schema()
    assert db.migrate_item_discovery_schema()

    conn = sqlite3.connect(database_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(items)")}
        discovered_at = conn.execute(
            "SELECT discovered_at FROM items WHERE item=101"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "discovered_at" in columns
    assert "idx_items_discovered_at" in indexes
    assert discovered_at == pytest.approx(1700000000.5)


def test_items_and_queries_can_be_ordered_by_local_discovery_time(database):
    query_id = _add_query(
        "https://www.vinted.co.uk/catalog?search_text=lamps",
        "Lamps",
    )
    db.add_item_to_db(
        101,
        "Older listing, newest alert",
        query_id,
        20,
        1700000000,
        "",
        "GBP",
        discovered_at=1780000000,
    )
    db.add_item_to_db(
        102,
        "Newer listing, older alert",
        query_id,
        30,
        1700001000,
        "",
        "GBP",
        discovered_at=1779999000,
    )

    assert [row[0] for row in db.get_items(sort="date_desc")] == [102, 101]
    discovered = db.get_items(sort="discovered_desc")
    assert [row[0] for row in discovered] == [101, 102]
    assert discovered[0][8] == pytest.approx(1780000000)
    assert db.get_query_last_discovery_map()[query_id] == pytest.approx(1780000000)


def test_dashboard_feed_uses_alert_order_and_refreshes_live(web_client):
    recent_query_id = _add_query(
        "https://www.vinted.co.uk/catalog?search_text=recent-alert",
        "Recent alert query",
    )
    older_query_id = _add_query(
        "https://www.vinted.co.uk/catalog?search_text=older-alert",
        "Older alert query",
    )
    db.add_item_to_db(
        201,
        "Old listing that just alerted",
        recent_query_id,
        25,
        1700000000,
        "",
        "GBP",
        discovered_at=1780000000,
    )
    db.add_item_to_db(
        202,
        "New listing that alerted earlier",
        older_query_id,
        35,
        1770000000,
        "",
        "GBP",
        discovered_at=1779999000,
    )

    page = web_client.get("/")
    assert page.status_code == 200
    page_html = page.get_data(as_text=True)
    assert "Last Alerted Item" in page_html
    assert "Last Alert" in page_html
    assert "fetch('/api/dashboard/feed'" in page_html
    assert "window.setInterval(loadDashboardFeed, 20000)" in page_html

    response = web_client.get("/api/dashboard/feed")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()

    cards = payload["recent_item_cards_html"]
    assert cards.index("Old listing that just alerted") < cards.index(
        "New listing that alerted earlier"
    )
    assert "Found:" in cards
    assert "Listed:" in cards
    assert "Old listing that just alerted" in payload["last_item_html"]

    queries = payload["query_rows_html"]
    assert queries.index("Recent alert query") < queries.index("Older alert query")
    assert payload["total_items"] == 2
    assert payload["active_queries"] == 2

    items_page = web_client.get("/items")
    assert items_page.status_code == 200
    items_html = items_page.get_data(as_text=True)
    assert items_html.index("Old listing that just alerted") < items_html.index(
        "New listing that alerted earlier"
    )
    assert 'value="discovered_desc" selected' in items_html
    assert "Found:" in items_html
    assert "Listed:" in items_html

    listed_page = web_client.get("/items?sort=date_desc")
    listed_html = listed_page.get_data(as_text=True)
    assert listed_html.index("New listing that alerted earlier") < listed_html.index(
        "Old listing that just alerted"
    )
    assert 'value="date_desc" selected' in listed_html
