import json
from pathlib import Path

import pytest

import db

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.migrate_pending_notifications_table()
    assert db.migrate_pending_ai_evaluations_table()
    yield database_path


def _subscribed_query():
    query_id, created, _ = db.add_query_to_db(
        "https://www.vinted.co.uk/catalog?search_text=pooky",
        name=None,
    )
    assert created
    with db.get_db_connection() as conn:
        conn.execute("""
            INSERT INTO telegram_users (chat_id, status, is_admin)
            VALUES ('1001', 'approved', 1)
            """)
        conn.execute(
            "INSERT INTO query_subscriptions (query_id, chat_id) VALUES (?, '1001')",
            (query_id,),
        )
        conn.commit()
    return query_id


def test_ai_job_is_persisted_leased_retried_and_completed_atomically(database):
    query_id = _subscribed_query()
    result = db.persist_item_and_notification(
        id=123,
        title="Pooky lamp",
        query_id=query_id,
        price="20",
        timestamp=1_700_000_000,
        photo_url="https://images.example/item.jpg",
        currency="GBP",
        content="New item",
        notification_url="https://www.vinted.co.uk/items/123",
        button_text="View",
        ai_evaluation={
            "item_id": 123,
            "query_id": query_id,
            "title": "Pooky lamp",
            "brand": "Pooky",
            "condition": "Very good",
            "price": "20",
            "currency": "GBP",
            "photo_url": "https://images.example/item.jpg",
            "item_url": "https://www.vinted.co.uk/items/123",
        },
    )
    assert result == (True, ["1001"])
    assert db.count_pending_notifications() == 1
    assert db.count_pending_ai_evaluations() == 1

    job = db.claim_due_ai_evaluation(now=2_000_000_000, lease_seconds=60)
    assert job[1:10] == (
        123,
        query_id,
        "Pooky lamp",
        "Pooky",
        "Very good",
        "20",
        "GBP",
        "https://images.example/item.jpg",
        "https://www.vinted.co.uk/items/123",
    )
    assert json.loads(job[10]) == ["1001"]
    assert db.is_notification_pending(job[11]) is True
    assert job[12] == 0
    assert db.claim_due_ai_evaluation(now=2_000_000_010) is None

    assert db.reschedule_ai_evaluation(job[0], 1, 2_000_000_100, "temporary")
    assert db.claim_due_ai_evaluation(now=2_000_000_099) is None
    retried = db.claim_due_ai_evaluation(now=2_000_000_100)
    assert retried[12] == 1

    assert db.complete_ai_evaluation(
        retried[0],
        content="AI verdict",
        url=None,
        button_text=None,
        chat_ids=["1001"],
        query_id=query_id,
        ignore_query_pause=True,
    )
    assert db.count_pending_ai_evaluations() == 0
    assert db.count_pending_notifications() == 2
    with db.get_db_connection() as conn:
        follow_up = conn.execute("""
            SELECT query_id, ignore_query_pause
            FROM pending_notifications
            WHERE content='AI verdict'
            """).fetchone()
        assert follow_up == (query_id, 1)


def test_ai_outbox_policy_ignores_pause_but_still_honours_unsubscribe(database):
    from telegram_bot_plugin.telegram_bot import _eligible_outbox_chat_ids

    query_id = _subscribed_query()
    assert db.set_query_enabled(query_id, False)
    assert _eligible_outbox_chat_ids(query_id, ["1001"]) == []
    assert _eligible_outbox_chat_ids(
        query_id,
        ["1001"],
        ignore_query_pause=True,
    ) == ["1001"]

    assert db.remove_query_subscription(query_id, "1001")
    assert (
        _eligible_outbox_chat_ids(
            query_id,
            ["1001"],
            ignore_query_pause=True,
        )
        == []
    )


def test_query_edit_revision_rejects_stale_full_form_without_partial_writes(database):
    query_id = _subscribed_query()
    original = db.get_query_edit_state(query_id)
    original_revision = db.query_edit_revision(original)

    status = db.update_query_configuration_atomic(
        query_id,
        original["query"],
        "Current name",
        {
            "poll_mode": "fast",
            "monitor_during_quiet_hours": True,
            "deal_evaluator_enabled": False,
            "deal_excellent_max": None,
            "deal_good_max": None,
            "deal_currency": "GBP",
        },
        True,
        expected_revision=original_revision,
    )
    assert status == "updated"

    status = db.update_query_configuration_atomic(
        query_id,
        "https://www.vinted.co.uk/catalog?search_text=stale",
        "Stale name",
        {
            "poll_mode": "normal",
            "monitor_during_quiet_hours": False,
            "deal_evaluator_enabled": False,
            "deal_excellent_max": None,
            "deal_good_max": None,
            "deal_currency": "EUR",
        },
        False,
        expected_revision=original_revision,
    )
    assert status == "stale"

    current = db.get_query_edit_state(query_id)
    assert current["query"] == original["query"]
    assert current["query_name"] == "Current name"
    assert current["poll_mode"] == "fast"
    assert current["monitor_during_quiet_hours"] is True
    assert current["deal_currency"] == "GBP"
    assert current["deal_ai_enabled"] is True
