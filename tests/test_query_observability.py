import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import db
import query_observability as observation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    yield database_path


def _add_query(url):
    with closing(db.get_db_connection()) as connection:
        query_id = connection.execute(
            "INSERT INTO queries (query, query_name) VALUES (?, ?)",
            (url, "Test query"),
        ).lastrowid
        connection.commit()
        return query_id


def _snapshot(item_id, listed_at, *, title=None):
    return {
        "item_id": item_id,
        "title": title or f"Listing {item_id}",
        "brand": "Brand",
        "condition": "Very good",
        "price": "12.50",
        "currency": "GBP",
        "photo_url": "https://images.example.test/photo.jpg?size=large",
        "item_url": f"https://www.vinted.co.uk/items/{item_id}?referrer=catalog",
        "listed_at": listed_at,
        "country_code": "GB",
        "description": "must not be persisted",
        "seller_id": 999,
    }


def test_migration_is_idempotent_seeds_history_and_deduplicates_items(database):
    query_id = _add_query("https://www.vinted.co.uk/catalog?search_text=lamp")
    with closing(db.get_db_connection()) as connection:
        connection.executemany(
            """
            INSERT INTO items
                (item, title, price, currency, timestamp, photo_url, query_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (101, "Lamp", 10, "GBP", 1000, None, query_id),
                (101, "Lamp duplicate", 10, "GBP", 1000, None, query_id),
                (102, "Shade", 20, "GBP", 2000, None, query_id),
            ],
        )
        connection.commit()

    assert observation.migrate_schema()
    assert observation.migrate_schema()

    with closing(db.get_db_connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM query_item_observations WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 2
        )
        progress = connection.execute(
            """
            SELECT anchor_item_key, successful_observations
            FROM query_progress WHERE query_id=?
            """,
            (query_id,),
        ).fetchone()
        assert progress == (observation.item_key(102), 1)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO items (item, query_id) VALUES (?, ?)",
                (101, query_id),
            )


def test_failure_never_advances_progress_but_empty_success_does(database):
    url = "https://www.vinted.co.uk/catalog?search_text=pooky"
    query_id = _add_query(url)
    observation.migrate_schema()

    failed_id = observation.start_execution(query_id, url, 20, started_at=1000)
    assert observation.record_failure(
        failed_id,
        "http_403 customer@example.test",
        http_status=403,
        duration_ms=321,
        finished_at=1001,
    )
    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                """
            SELECT successful_observations, last_execution_id
            FROM query_progress WHERE query_id=?
            """,
                (query_id,),
            ).fetchone()
            == (0, None)
        )
        outcome = connection.execute(
            "SELECT outcome FROM catalogue_query_executions WHERE id=?",
            (failed_id,),
        ).fetchone()[0]
        assert outcome == "http_403_customer_example.test"

    success_id = observation.start_execution(query_id, url, 20, started_at=1010)
    result = observation.record_success(
        success_id,
        query_id,
        url,
        [],
        duration_ms=50,
        finished_at=1011,
    )
    assert result.candidate_ids == frozenset()
    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                """
            SELECT anchor_item_key, successful_observations, last_execution_id
            FROM query_progress WHERE query_id=?
            """,
                (query_id,),
            ).fetchone()
            == (None, 1, success_id)
        )


def test_bootstrap_uses_twenty_minutes_then_anchor_accepts_older_listing(database):
    url = "https://www.vinted.co.uk/catalog?search_text=raffield"
    query_id = _add_query(url)
    observation.migrate_schema()

    first_id = observation.start_execution(query_id, url, 20, started_at=10_000)
    first = observation.record_success(
        first_id,
        query_id,
        url,
        [
            _snapshot(2, 9_900),
            _snapshot(1, 1_000),
        ],
        duration_ms=80,
        finished_at=10_000,
    )
    assert first.candidate_ids == frozenset({2})
    assert first.metrics["fresh_count"] == 1
    assert first.metrics["bootstrapped"] == 1

    # Listing 3 is older than twenty minutes, but it first appears ahead of
    # the previous successful anchor and must no longer be discarded by age.
    second_id = observation.start_execution(query_id, url, 20, started_at=20_000)
    second = observation.record_success(
        second_id,
        query_id,
        url,
        [
            _snapshot(3, 2_000),
            _snapshot(2, 9_900),
            _snapshot(1, 1_000),
        ],
        duration_ms=70,
        finished_at=20_000,
    )
    assert second.candidate_ids == frozenset({3})
    assert second.metrics["fresh_count"] == 1

    # Replaying the same successful window after a restart is idempotent.
    third_id = observation.start_execution(query_id, url, 20, started_at=21_000)
    third = observation.record_success(
        third_id,
        query_id,
        url,
        [_snapshot(3, 2_000), _snapshot(2, 9_900), _snapshot(1, 1_000)],
        duration_ms=60,
        finished_at=21_000,
    )
    assert third.candidate_ids == frozenset()
    assert third.metrics["fresh_count"] == 0


def test_pending_snapshots_are_sanitized_leased_and_finalized_once(database):
    url = "https://www.vinted.co.uk/catalog?search_text=shade"
    query_id = _add_query(url)
    observation.migrate_schema()
    execution_id = observation.start_execution(query_id, url, 20, started_at=5000)
    result = observation.record_success(
        execution_id,
        query_id,
        url,
        [_snapshot(42, 4990)],
        duration_ms=100,
        finished_at=5000,
    )
    assert result.candidate_ids == frozenset({42})

    batch = observation.pending_batch(limit=100, now=5001, lease_seconds=30)
    assert batch is not None
    assert batch[0:2] == (execution_id, query_id)
    pending = batch[2][0]
    assert pending["item_id"] == 42
    assert pending["item_url"] == "https://www.vinted.co.uk/items/42"
    assert "description" not in pending
    assert "seller_id" not in pending
    assert observation.pending_batch(now=5002) is None

    assert observation.classify_pending(
        execution_id,
        query_id,
        42,
        "accepted",
        notification_generated=2,
        now=5003,
    )
    assert observation.classify_pending(
        execution_id,
        query_id,
        42,
        "accepted",
        notification_generated=2,
        now=5004,
    )
    with closing(db.get_db_connection()) as connection:
        row = connection.execute(
            """
            SELECT accepted_count, notifications_generated,
                   processing_finished_at
            FROM catalogue_query_executions WHERE id=?
            """,
            (execution_id,),
        ).fetchone()
        assert row == (1, 2, 5003)
        status, payload = connection.execute(
            "SELECT status, snapshot_json FROM pending_query_items"
        ).fetchone()
        assert (status, payload) == ("accepted", "{}")


def test_query_scoped_claims_preserve_active_overlap_and_report_evidence(database):
    first_url = "https://www.vinted.co.uk/catalog?search_text=lamp"
    second_url = "https://www.vinted.co.uk/catalog?brand_ids[]=1"
    first_query = _add_query(first_url)
    second_query = _add_query(second_url)
    observation.migrate_schema()

    first_execution = observation.start_execution(
        first_query, first_url, 20, started_at=10_000
    )
    first = observation.record_success(
        first_execution,
        first_query,
        first_url,
        [_snapshot(77, 9_999)],
        duration_ms=100,
        finished_at=10_000,
    )
    assert first.candidate_ids == frozenset({77})

    second_execution = observation.start_execution(
        second_query, second_url, 20, started_at=10_010
    )
    second = observation.record_success(
        second_execution,
        second_query,
        second_url,
        [_snapshot(77, 9_999)],
        duration_ms=110,
        finished_at=10_010,
    )
    assert second.candidate_ids == frozenset({77})
    assert second.metrics["already_known_count"] == 0
    assert second.metrics["cross_query_overlap_count"] == 1

    report = observation.get_efficiency_report(days=1, now=10_020)
    assert report["summary"]["execution_count"] == 2
    assert report["summary"]["success_count"] == 2
    overlap = report["overlaps"][0]
    assert overlap["query_a_id"] == first_query
    assert overlap["query_b_id"] == second_query
    assert overlap["shared_item_count"] == 1
    assert overlap["query_a_item_count"] == 1
    assert overlap["query_b_item_count"] == 1
    assert overlap["overlap_rate"] == 1.0
    assert "item_key" not in overlap


def test_reset_query_state_is_transactional_and_next_success_bootstraps(database):
    old_url = "https://www.vinted.co.uk/catalog?search_text=old"
    new_url = "https://www.vinted.co.uk/catalog?search_text=new"
    query_id = _add_query(old_url)
    observation.migrate_schema()
    execution_id = observation.start_execution(query_id, old_url, 20, started_at=100)
    observation.record_success(
        execution_id,
        query_id,
        old_url,
        [_snapshot(1, 99)],
        duration_ms=10,
        finished_at=100,
    )

    with closing(db.get_db_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE queries SET query=? WHERE id=?", (new_url, query_id))
        observation.reset_query_state_with_cursor(
            connection.cursor(), query_id, new_url
        )
        connection.commit()

    with closing(db.get_db_connection()) as connection:
        progress = connection.execute(
            """
            SELECT query_fingerprint, anchor_item_key, successful_observations
            FROM query_progress WHERE query_id=?
            """,
            (query_id,),
        ).fetchone()
        assert progress == (observation.query_fingerprint(new_url), None, 0)
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM pending_query_items
            WHERE query_id=? AND status='pending'
            """,
                (query_id,),
            ).fetchone()[0]
            == 0
        )


def test_retention_keeps_progress_and_unfinished_pending_work(database):
    url = "https://www.vinted.co.uk/catalog?search_text=keep"
    query_id = _add_query(url)
    observation.migrate_schema()
    execution_id = observation.start_execution(query_id, url, 20, started_at=100)
    observation.record_success(
        execution_id,
        query_id,
        url,
        [_snapshot(8, 99)],
        duration_ms=10,
        finished_at=100,
    )

    deleted = observation.prune_retention(days=1, now=200_000)
    assert deleted["executions_deleted"] == 0
    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                "SELECT successful_observations FROM query_progress WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM pending_query_items WHERE status='pending'"
            ).fetchone()[0]
            == 1
        )
