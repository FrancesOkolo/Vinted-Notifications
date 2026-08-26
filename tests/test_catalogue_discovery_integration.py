import queue
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import core
import db
import query_observability as observation
from url_normalizer import normalise_vinted_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.configure_database_runtime()
    assert db.migrate_pending_notifications_table()
    db.set_parameter("telegram_chat_id", "123")
    assert db.migrate_multi_user_schema()
    assert observation.migrate_schema()
    db.set_parameter("quiet_hours_enabled", "False")
    db.set_parameter("banwords", "")
    db.set_parameter("message_template", db.DEFAULT_MESSAGE_TEMPLATE)
    core._clear_scraper_cooldown()
    yield database_path


def _query(search_text="durable-discovery"):
    url = normalise_vinted_url(
        f"https://www.vinted.co.uk/catalog?search_text={search_text}"
    )
    query_id, created, subscribed = db.add_query_to_db(
        url,
        name=search_text,
        chat_id="123",
    )
    assert created and subscribed
    return query_id, url


def _item(item_id, listed_at, *, title=None):
    return SimpleNamespace(
        id=item_id,
        title=title or f"Listing {item_id}",
        brand_title="Brand",
        condition="Very good",
        description=None,
        size_title=None,
        currency="GBP",
        price="12.50",
        photo=None,
        url=f"https://www.vinted.co.uk/items/{item_id}",
        raw_timestamp=listed_at,
        raw_data={"country_iso_code": "GB"},
        is_new_item=lambda: (_ for _ in ()).throw(
            AssertionError("ongoing discovery must not use the 20-minute clock")
        ),
    )


def _snapshot(item_id, listed_at, *, title=None):
    return observation.item_snapshot(_item(item_id, listed_at, title=title))


def _record_success(query_id, url, items, *, started_at=None):
    started_at = time.time() if started_at is None else started_at
    execution_id = observation.start_execution(
        query_id,
        url,
        20,
        started_at=started_at,
    )
    result = observation.record_success(
        execution_id,
        query_id,
        url,
        [_snapshot(item.id, item.raw_timestamp, title=item.title) for item in items],
        duration_ms=25,
        finished_at=started_at + 0.025,
    )
    return execution_id, result


def _execution(execution_id):
    with closing(db.get_db_connection()) as connection:
        return connection.execute(
            """
            SELECT outcome, http_status, returned_count, fresh_count,
                   already_known_count, accepted_count,
                   locally_rejected_count, notifications_generated,
                   pending_count
            FROM catalogue_query_executions
            WHERE id=?
            """,
            (execution_id,),
        ).fetchone()


def test_process_items_discovers_item_older_than_twenty_minutes_after_anchor(
    database, monkeypatch
):
    query_id, url = _query("older-than-twenty")
    now = time.time()
    anchor = _item(100, now - 120)
    first_execution, first = _record_success(
        query_id,
        url,
        [anchor],
        started_at=now - 60,
    )
    assert first.candidate_ids == frozenset({100})
    assert observation.classify_pending(
        first_execution,
        query_id,
        100,
        "already_known",
    )

    old_but_newly_observed = _item(101, now - 3600)

    class Items:
        def search(self, query_url, nbr_items):
            assert query_url == url
            assert nbr_items == 20
            return [old_but_newly_observed, anchor]

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    source = queue.Queue()
    core.process_items(source, query_ids=[query_id])

    data, queued_query_id, execution_id = source.get_nowait()
    assert queued_query_id == query_id
    assert [item.id for item in data] == [101]
    assert _execution(execution_id)[2:4] == (2, 1)


def test_failed_request_is_recorded_without_advancing_progress(database, monkeypatch):
    query_id, url = _query("failed-request")

    class Items:
        def search(self, _query_url, nbr_items):
            assert nbr_items == 20
            response = SimpleNamespace(status_code=500, headers={})
            raise requests.exceptions.HTTPError("500 Server Error", response=response)

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    core.process_items(queue.Queue(), query_ids=[query_id])

    with closing(db.get_db_connection()) as connection:
        progress = connection.execute(
            """
            SELECT successful_observations, last_execution_id
            FROM query_progress WHERE query_id=?
            """,
            (query_id,),
        ).fetchone()
        execution = connection.execute(
            """
            SELECT outcome, http_status, finished_at
            FROM catalogue_query_executions
            WHERE query_id=? ORDER BY id DESC LIMIT 1
            """,
            (query_id,),
        ).fetchone()

    # A newly created query need not gain a progress row until its first
    # successful response; if an implementation pre-creates one, it must
    # still remain at the zero/null state after the failed request.
    assert progress in (None, (0, None))
    assert execution[0] != "started"
    assert execution[1] == 500
    assert execution[2] is not None


def test_restart_drains_durable_pending_item_exactly_once(database):
    query_id, url = _query("restart-drain")
    item = _item(201, time.time() - 1)
    execution_id, result = _record_success(query_id, url, [item])
    assert result.candidate_ids == frozenset({201})

    destination = queue.Queue()
    core.clear_item_queue(queue.Queue(), destination)

    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM items WHERE item=201").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute(
                "SELECT status FROM pending_query_items WHERE execution_id=?",
                (execution_id,),
            ).fetchone()[0]
            == "accepted"
        )
    assert _execution(execution_id)[5:9] == (1, 0, 1, 1)
    assert destination.qsize() == 1

    # Simulate another restart/run: terminal pending work must not be replayed.
    core.clear_item_queue(queue.Queue(), destination)
    with closing(db.get_db_connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[
                0
            ]
            == 1
        )
    assert destination.qsize() == 1


def test_two_items_with_the_same_timestamp_are_both_persisted(database):
    query_id, url = _query("same-second")
    listed_at = time.time() - 1
    execution_id, result = _record_success(
        query_id,
        url,
        [_item(301, listed_at), _item(302, listed_at)],
    )
    assert result.candidate_ids == frozenset({301, 302})

    destination = queue.Queue()
    core.clear_item_queue(queue.Queue(), destination)

    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM items WHERE item IN (301, 302)"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[
                0
            ]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM pending_query_items WHERE status='accepted'"
            ).fetchone()[0]
            == 2
        )
    assert _execution(execution_id)[5:9] == (2, 0, 2, 2)
    assert destination.qsize() == 2


def test_ongoing_same_second_item_behind_anchor_is_discovered(database):
    query_id, url = _query("same-second-reorder")
    first_execution = observation.start_execution(
        query_id,
        url,
        20,
        started_at=1_000.75,
    )
    first = observation.record_success(
        first_execution,
        query_id,
        url,
        [_snapshot(351, 1_000)],
        duration_ms=100,
        finished_at=1_001,
    )
    assert first.candidate_ids == frozenset({351})

    second_execution = observation.start_execution(
        query_id,
        url,
        20,
        started_at=1_001.25,
    )
    second = observation.record_success(
        second_execution,
        query_id,
        url,
        [_snapshot(351, 1_000), _snapshot(352, 1_000)],
        duration_ms=100,
        finished_at=1_002,
    )

    assert second.candidate_ids == frozenset({352})
    assert second.metrics["anchor_found"] == 1


def test_local_rejection_finalizes_pending_and_updates_metrics(database):
    query_id, url = _query("local-rejection")
    db.set_parameter("banwords", "empty+box")
    item = _item(401, time.time() - 1, title="Empty Pooky Lamp Box")
    execution_id, result = _record_success(query_id, url, [item])
    assert result.candidate_ids == frozenset({401})

    destination = queue.Queue()
    core.clear_item_queue(queue.Queue(), destination)

    with closing(db.get_db_connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT status FROM pending_query_items WHERE execution_id=?",
                (execution_id,),
            ).fetchone()[0]
            == "locally_rejected"
        )
    assert _execution(execution_id)[5:9] == (0, 1, 0, 1)
    assert destination.empty()


def _save_query_edit(query_id, *, url=None, name=None):
    current = db.get_query_edit_state(query_id)
    target_url = current["query"] if url is None else url
    target_name = current["query_name"] if name is None else name
    preferences = {
        "poll_mode": current["poll_mode"],
        "monitor_during_quiet_hours": current["monitor_during_quiet_hours"],
        "deal_evaluator_enabled": current["deal_evaluator_enabled"],
        "deal_excellent_max": current["deal_excellent_max"],
        "deal_good_max": current["deal_good_max"],
        "deal_currency": current["deal_currency"],
    }
    return db.update_query_configuration_atomic(
        query_id,
        target_url,
        target_name,
        preferences,
        current["deal_ai_enabled"],
        expected_revision=db.query_edit_revision(current),
    )


def test_atomic_edit_resets_progress_only_when_query_url_changes(database):
    query_id, old_url = _query("edit-reset-old")
    item = _item(501, time.time() - 1)
    execution_id, result = _record_success(query_id, old_url, [item])
    assert result.candidate_ids == frozenset({501})

    assert _save_query_edit(query_id, name="Renamed only") == "updated"
    with closing(db.get_db_connection()) as connection:
        progress_before_url_change = connection.execute(
            """
            SELECT query_fingerprint, successful_observations
            FROM query_progress WHERE query_id=?
            """,
            (query_id,),
        ).fetchone()
        pending_before_url_change = connection.execute(
            """
            SELECT COUNT(*) FROM pending_query_items
            WHERE execution_id=? AND status='pending'
            """,
            (execution_id,),
        ).fetchone()[0]
    assert progress_before_url_change == (
        observation.query_fingerprint(old_url),
        1,
    )
    assert pending_before_url_change == 1

    new_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=edit-reset-new"
    )
    assert _save_query_edit(query_id, url=new_url) == "updated"
    with closing(db.get_db_connection()) as connection:
        progress = connection.execute(
            """
            SELECT query_fingerprint, anchor_item_key, successful_observations
            FROM query_progress WHERE query_id=?
            """,
            (query_id,),
        ).fetchone()
        pending = connection.execute(
            "SELECT COUNT(*) FROM pending_query_items WHERE query_id=?",
            (query_id,),
        ).fetchone()[0]
        observations = connection.execute(
            "SELECT COUNT(*) FROM query_item_observations WHERE query_id=?",
            (query_id,),
        ).fetchone()[0]

    assert progress == (observation.query_fingerprint(new_url), None, 0)
    assert pending == 0
    assert observations == 0


def test_url_edit_during_http_response_discards_old_execution(database, monkeypatch):
    query_id, old_url = _query("edit-during-response-old")
    new_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=edit-during-response-new"
    )
    returned_item = _item(601, time.time() - 1)

    class Items:
        def search(self, query_url, nbr_items):
            assert query_url == old_url
            assert nbr_items == 20
            assert db.update_query_in_db(query_id, new_url, "Changed mid-request")
            return [returned_item]

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    source = queue.Queue()
    core.process_items(source, query_ids=[query_id])

    data, queued_query_id, execution_id = source.get_nowait()
    assert data == []
    assert queued_query_id == query_id
    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                "SELECT query FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            == new_url
        )
        assert (
            connection.execute(
                """
            SELECT query_fingerprint, successful_observations
            FROM query_progress WHERE query_id=?
            """,
                (query_id,),
            ).fetchone()
            == (observation.query_fingerprint(new_url), 0)
        )
        execution = connection.execute(
            """
            SELECT outcome, http_status, finished_at
            FROM catalogue_query_executions WHERE id=?
            """,
            (execution_id,),
        ).fetchone()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM query_item_observations WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM pending_query_items WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 0
        )

    assert execution[0:2] == ("discarded_query_changed", 200)
    assert execution[2] is not None


def test_url_edit_removes_terminal_claims_from_old_query(database):
    query_id, old_url = _query("terminal-reset-old")
    execution_id, result = _record_success(
        query_id,
        old_url,
        [_item(612, time.time() - 1)],
    )
    assert result.candidate_ids == frozenset({612})
    assert observation.classify_pending(
        execution_id,
        query_id,
        612,
        "locally_rejected",
    )

    new_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=terminal-reset-new"
    )
    assert db.update_query_in_db(query_id, new_url, "Changed")

    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM pending_query_items WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 0
        )


def test_url_reset_invalidates_old_in_memory_batch(database):
    query_id, old_url = _query("stale-batch-old")
    item = _item(611, time.time() - 1)
    execution_id, result = _record_success(query_id, old_url, [item])
    assert result.candidate_ids == frozenset({611})

    new_url = normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=stale-batch-new"
    )
    assert db.update_query_in_db(query_id, new_url, "Reset query")

    source = queue.Queue()
    destination = queue.Queue()
    source.put(([item], query_id, execution_id))
    core.clear_item_queue(source, destination)

    with closing(db.get_db_connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM pending_query_items WHERE query_id=?",
                (query_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT last_item FROM queries WHERE id=?", (query_id,)
            ).fetchone()[0]
            is None
        )
    assert destination.empty()


def test_paused_query_claim_does_not_block_overlapping_active_query(database):
    paused_query_id, paused_url = _query("overlap-paused")
    active_query_id, active_url = _query("overlap-active")
    item = _item(621, time.time() - 1)

    paused_execution, paused_result = _record_success(
        paused_query_id,
        paused_url,
        [item],
    )
    assert paused_result.candidate_ids == frozenset({621})
    assert db.set_query_enabled(paused_query_id, False)

    active_execution, active_result = _record_success(
        active_query_id,
        active_url,
        [item],
    )
    assert active_result.candidate_ids == frozenset({621})
    assert active_result.metrics["cross_query_overlap_count"] == 1

    destination = queue.Queue()
    core.clear_item_queue(queue.Queue(), destination)
    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute("SELECT query_id FROM items WHERE item=621").fetchone()[
                0
            ]
            == active_query_id
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[
                0
            ]
            == 1
        )
        statuses = dict(
            connection.execute(
                """
                SELECT execution_id, status FROM pending_query_items
                WHERE execution_id IN (?, ?)
                """,
                (paused_execution, active_execution),
            ).fetchall()
        )
    assert statuses == {
        paused_execution: "pending",
        active_execution: "accepted",
    }
    assert destination.qsize() == 1

    # Resuming A drains its claim as already known without a second alert.
    assert db.set_query_enabled(paused_query_id, True)
    core.clear_item_queue(queue.Queue(), destination)
    with closing(db.get_db_connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute(
                """
            SELECT status FROM pending_query_items
            WHERE execution_id=?
            """,
                (paused_execution,),
            ).fetchone()[0]
            == "already_known"
        )
    assert destination.qsize() == 1


def test_migration_reconciles_stale_started_execution_after_restart(database):
    query_id, url = _query("abandoned-restart")
    execution_id = observation.start_execution(
        query_id,
        url,
        20,
        started_at=100,
    )

    assert observation.migrate_schema()
    with closing(db.get_db_connection()) as connection:
        first = connection.execute(
            """
            SELECT outcome, finished_at, processing_finished_at
            FROM catalogue_query_executions WHERE id=?
            """,
            (execution_id,),
        ).fetchone()
    assert first[0] == "abandoned_restart"
    assert first[1] is not None
    assert first[2] is not None

    # A repeated startup migration is idempotent and preserves reconciliation
    # timestamps instead of rewriting history.
    assert observation.migrate_schema()
    with closing(db.get_db_connection()) as connection:
        second = connection.execute(
            """
            SELECT outcome, finished_at, processing_finished_at
            FROM catalogue_query_executions WHERE id=?
            """,
            (execution_id,),
        ).fetchone()
    assert second == first


def test_terminal_classification_is_idempotent_only_for_same_disposition(database):
    query_id, url = _query("terminal-idempotency")
    item = _item(631, time.time() - 1)
    execution_id, result = _record_success(query_id, url, [item])
    assert result.candidate_ids == frozenset({631})

    assert observation.classify_pending(
        execution_id,
        query_id,
        631,
        "accepted",
        notification_generated=1,
    )
    assert observation.classify_pending(
        execution_id,
        query_id,
        631,
        "accepted",
        notification_generated=99,
    )
    assert not observation.classify_pending(
        execution_id,
        query_id,
        631,
        "locally_rejected",
    )
    assert not observation.classify_pending(
        execution_id,
        query_id,
        631,
        "already_known",
    )

    with closing(db.get_db_connection()) as connection:
        execution = connection.execute(
            """
            SELECT accepted_count, locally_rejected_count,
                   already_known_count, notifications_generated
            FROM catalogue_query_executions WHERE id=?
            """,
            (execution_id,),
        ).fetchone()
        item_metrics = connection.execute(
            """
            SELECT accepted, locally_rejected, already_known,
                   notification_count
            FROM catalogue_query_execution_items
            WHERE execution_id=? AND item_key=?
            """,
            (execution_id, observation.item_key(631)),
        ).fetchone()
    assert execution == (1, 0, 0, 1)
    assert item_metrics == (1, 0, 0, 1)


def test_execution_counts_raw_duplicates_separately_from_unique_items(database):
    query_id, url = _query("duplicate-counts")
    listed_at = time.time() - 1
    execution_id = observation.start_execution(
        query_id,
        url,
        20,
        started_at=time.time(),
    )
    result = observation.record_success(
        execution_id,
        query_id,
        url,
        [
            _snapshot(641, listed_at),
            _snapshot(641, listed_at),
            _snapshot(642, listed_at),
        ],
        duration_ms=10,
    )

    assert result.candidate_ids == frozenset({641, 642})
    assert result.metrics["returned_count"] == 3
    assert result.metrics["unique_returned_count"] == 2
    assert result.metrics["fresh_count"] == 2
    assert result.metrics["pending_count"] == 2
    with closing(db.get_db_connection()) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM catalogue_query_execution_items
            WHERE execution_id=?
            """,
                (execution_id,),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM pending_query_items
            WHERE execution_id=? AND status='pending'
            """,
                (execution_id,),
            ).fetchone()[0]
            == 2
        )
