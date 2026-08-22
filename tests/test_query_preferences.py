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
    yield database_path


def test_query_preferences_keep_legacy_query_shape_and_canonicalise_values(database):
    query_id, created, _subscribed = db.add_query_to_db(
        "https://www.vinted.co.uk/catalog?search_text=pooky"
    )
    assert created
    assert len(db.get_queries()[0]) == 4
    assert db.get_query_preferences(query_id) == db.QUERY_PREFERENCE_DEFAULTS
    assert db.get_parameter("fast_query_refresh_delay") == "90"

    assert db.set_query_preferences(
        query_id,
        poll_mode="FAST",
        monitor_during_quiet_hours="on",
        deal_evaluator_enabled=True,
        deal_excellent_max="010.5000",
        deal_good_max="20.00",
        deal_currency="gbp",
    )
    assert db.get_query_preferences(query_id) == {
        "poll_mode": "fast",
        "monitor_during_quiet_hours": True,
        "deal_evaluator_enabled": True,
        "deal_excellent_max": "10.5",
        "deal_good_max": "20",
        "deal_currency": "GBP",
    }

    # Invalid input is rejected without replacing the last valid values.
    assert not db.set_query_preferences(
        query_id,
        deal_evaluator_enabled=True,
        deal_excellent_max="30",
        deal_good_max="20",
    )
    assert db.get_query_preferences(query_id)["poll_mode"] == "fast"


def test_query_preferences_migration_backfills_triggers_and_cascades(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    with sqlite3.connect(database_path) as conn:
        conn.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE queries
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL UNIQUE,
                last_item NUMERIC,
                query_name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE parameters (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO parameters (key, value) VALUES ('version', '1.2.1');
            INSERT INTO queries (query) VALUES ('https://www.vinted.co.uk/catalog');
            """)

    # Simulate a live installation that already applied the 1.2.1 migration
    # before the request-spacing key was added to that migration file.
    assert db.get_parameter("version") == "1.2.1"
    assert db.get_parameter("catalogue_request_spacing_seconds") is None

    assert db.migrate_query_preferences_schema()
    assert db.migrate_query_preferences_schema()
    assert db.get_query_preferences(1) == db.QUERY_PREFERENCE_DEFAULTS
    assert db.get_parameter("fast_query_refresh_delay") == "90"
    assert db.get_parameter("catalogue_request_spacing_seconds") == "12"

    db.set_parameter("catalogue_request_spacing_seconds", "30")
    assert db.migrate_query_preferences_schema()
    assert db.get_parameter("catalogue_request_spacing_seconds") == "30"

    with db.get_db_connection() as conn:
        second_id = conn.execute(
            "INSERT INTO queries (query) VALUES (?)",
            ("https://www.vinted.co.uk/catalog?search_text=raffield",),
        ).lastrowid
        conn.commit()
    assert db.get_query_preferences(second_id) == db.QUERY_PREFERENCE_DEFAULTS

    with db.get_db_connection() as conn:
        conn.execute("DELETE FROM queries WHERE id=?", (second_id,))
        conn.commit()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM query_preferences WHERE query_id=?",
                (second_id,),
            ).fetchone()[0]
            == 0
        )
