import asyncio
import json
import time
from contextlib import closing
from pathlib import Path

import pytest

import ai_deal_evaluator
import db
import vinted_notifications as app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "vinted_notifications.db"))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.migrate_pending_notifications_table()
    assert db.migrate_pending_ai_evaluations_table()
    yield


def _query_with_subscribers(*chat_ids):
    query_id, created, _ = db.add_query_to_db(
        "https://www.vinted.co.uk/catalog?search_text=pooky",
        name="Pooky",
    )
    assert created
    with closing(db.get_db_connection()) as conn:
        for index, chat_id in enumerate(chat_ids):
            conn.execute(
                """
                INSERT INTO telegram_users (chat_id, status, is_admin)
                VALUES (?, 'approved', ?)
                """,
                (chat_id, int(index == 0)),
            )
            conn.execute(
                "INSERT INTO query_subscriptions (query_id, chat_id) VALUES (?, ?)",
                (query_id, chat_id),
            )
        conn.commit()
    return query_id


def _persist_ai_item(query_id):
    result = db.persist_item_and_notification(
        id=501,
        title="Pooky lamp",
        query_id=query_id,
        price="20",
        timestamp=1_700_000_000,
        photo_url="https://images.example/item.jpg",
        currency="GBP",
        content="Primary item alert",
        notification_url="https://www.vinted.co.uk/items/501",
        button_text="Open Vinted",
        ai_evaluation={
            "brand": "Pooky",
            "condition": "Very good",
            "item_url": "https://www.vinted.co.uk/items/501",
        },
    )
    assert result is not None and result[0] is True
    with closing(db.get_db_connection()) as conn:
        return conn.execute(
            "SELECT id FROM pending_notifications WHERE content='Primary item alert'"
        ).fetchone()[0]


def _claim(now=None):
    return db.claim_due_ai_evaluation(
        now=time.time() + 60 if now is None else now,
        lease_seconds=30,
    )


def _followups():
    with closing(db.get_db_connection()) as conn:
        return conn.execute("""
            SELECT chat_ids, query_id, ignore_query_pause
            FROM pending_notifications
            WHERE content LIKE '🤖 <b>AI check</b>%'
            ORDER BY id
            """).fetchall()


def test_pause_before_primary_send_cancels_unstarted_ai_job(database, monkeypatch):
    query_id = _query_with_subscribers("111")
    _persist_ai_item(query_id)
    assert db.set_query_enabled(query_id, False)
    monkeypatch.setattr(
        ai_deal_evaluator,
        "evaluate",
        lambda _item: pytest.fail("paused job must not call OpenAI"),
    )

    assert app.process_ai_evaluation_job(_claim()) == "cancelled"
    assert db.count_pending_ai_evaluations() == 0
    assert _followups() == []


def test_parent_retry_exhaustion_without_ack_never_sends_ai(database, monkeypatch):
    query_id = _query_with_subscribers("111")
    parent_id = _persist_ai_item(query_id)
    assert db.delete_notification(parent_id)  # same terminal state as exhaustion
    monkeypatch.setattr(
        ai_deal_evaluator,
        "evaluate",
        lambda _item: pytest.fail("failed primary recipient must not call OpenAI"),
    )

    assert app.process_ai_evaluation_job(_claim()) == "parent_finished"
    assert db.count_pending_ai_evaluations() == 0
    assert _followups() == []


def test_mixed_primary_success_and_failure_follows_up_only_ack_once(
    database, monkeypatch
):
    query_id = _query_with_subscribers("111", "222")
    parent_id = _persist_ai_item(query_id)
    assert db.ack_notification_recipient(parent_id, "111") == 1
    calls = []
    monkeypatch.setattr(
        ai_deal_evaluator,
        "evaluate",
        lambda _item: calls.append("called") or "🔥 <b>AI: EXCELLENT DEAL</b>",
    )

    assert app.process_ai_evaluation_job(_claim()) == "completed"
    assert calls == ["called"]
    followups = _followups()
    assert len(followups) == 1
    assert json.loads(followups[0][0]) == ["111"]
    assert followups[0][1:] == (query_id, 1)
    assert db.count_pending_ai_evaluations() == 1

    # Recipient 222 exhausts primary retries. The cached verdict is not
    # regenerated and is never sent to that unacknowledged recipient.
    assert db.delete_notification(parent_id)
    assert app.process_ai_evaluation_job(_claim(time.time() + 120)) == "parent_finished"
    assert calls == ["called"]
    assert db.count_pending_ai_evaluations() == 0
    assert len(_followups()) == 1


def test_pause_during_evaluation_allows_only_acked_subscribed_recipient(
    database, monkeypatch
):
    query_id = _query_with_subscribers("111")
    parent_id = _persist_ai_item(query_id)
    assert db.ack_notification_recipient(parent_id, "111") == 0

    def evaluate_after_pause(_item):
        assert db.set_query_enabled(query_id, False)
        return "✅ <b>AI: GOOD DEAL</b>"

    monkeypatch.setattr(ai_deal_evaluator, "evaluate", evaluate_after_pause)

    assert app.process_ai_evaluation_job(_claim()) == "completed"
    followups = _followups()
    assert len(followups) == 1
    assert json.loads(followups[0][0]) == ["111"]
    assert followups[0][1:] == (query_id, 1)
    assert db.count_pending_ai_evaluations() == 0


def test_revocation_between_eligibility_and_send_is_not_a_primary_ack(
    database, monkeypatch
):
    from telegram_bot_plugin.telegram_bot import LeRobot

    query_id = _query_with_subscribers("111")
    _persist_ai_item(query_id)
    robot = LeRobot.__new__(LeRobot)
    robot.polling_enabled = False
    monkeypatch.setattr(
        db,
        "get_query_delivery_state",
        lambda _query_id: (True, ["111"]),
    )
    monkeypatch.setattr(
        db,
        "get_telegram_user_approval_state",
        lambda _chat_id: False,
    )
    monkeypatch.setattr(
        robot,
        "_send_message_with_retries",
        lambda *_args, **_kwargs: pytest.fail("revoked recipient must not be sent"),
    )

    asyncio.run(robot.drain_outbox(None))
    claimed = _claim()
    assert json.loads(claimed[13]) == []
    monkeypatch.setattr(
        ai_deal_evaluator,
        "evaluate",
        lambda _item: pytest.fail("non-ACKed recipient must not trigger AI"),
    )
    assert app.process_ai_evaluation_job(claimed) == "parent_finished"
    assert db.count_pending_ai_evaluations() == 0
    assert _followups() == []


def test_final_ack_after_claim_is_reloaded_before_job_cleanup(database, monkeypatch):
    query_id = _query_with_subscribers("111")
    parent_id = _persist_ai_item(query_id)
    stale_claim = _claim()
    assert json.loads(stale_claim[13]) == []
    assert db.ack_notification_recipient(parent_id, "111") == 0
    calls = []
    monkeypatch.setattr(
        ai_deal_evaluator,
        "evaluate",
        lambda _item: calls.append("called") or "✅ <b>AI: GOOD DEAL</b>",
    )

    assert app.process_ai_evaluation_job(stale_claim) == "parent_finished"
    assert db.count_pending_ai_evaluations() == 1
    refreshed = _claim(time.time() + 120)
    assert json.loads(refreshed[13]) == ["111"]
    assert app.process_ai_evaluation_job(refreshed) == "completed"
    assert calls == ["called"]
    assert json.loads(_followups()[0][0]) == ["111"]
