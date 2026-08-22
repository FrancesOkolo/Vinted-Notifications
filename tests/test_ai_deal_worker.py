import json

import pytest

import ai_deal_evaluator as evaluator
import core
import vinted_notifications as app


def _job(**overrides):
    values = {
        "job_id": 7,
        "item_id": 101,
        "query_id": 3,
        "title": "Pooky <Lamp>",
        "brand": "Pooky",
        "condition": "Very good",
        "price": "20",
        "currency": "GBP",
        "photo_url": "https://images.example/item.jpg",
        "item_url": "https://www.vinted.co.uk/items/101",
        "chat_ids_json": json.dumps(["111", "222"]),
        "parent_notification_id": None,
        "attempts": 0,
        "delivered_chat_ids_json": json.dumps(["111", "222"]),
        "handled_chat_ids_json": "[]",
        "result_content": None,
        "evaluation_started_at": None,
    }
    values.update(overrides)
    return tuple(values[name] for name in app._AI_JOB_FIELDS)


class _Response:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {
            "output_text": '{"verdict":"excellent","reason":"Far below benchmark"}'
        }

    def json(self):
        return self._data


def test_evaluate_uses_intended_default_without_browsing(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            url=url,
            headers=headers,
            payload=json,
            timeout=timeout,
        )
        return _Response()

    monkeypatch.setenv("VN_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("VN_OPENAI_MODEL", raising=False)
    monkeypatch.setattr(evaluator.requests, "post", fake_post)

    rating = evaluator.evaluate(
        {
            "title": "Pooky lamp",
            "brand_title": "Pooky",
            "condition": "Very good",
            "price": 0,
            "currency": "GBP",
            "photo": "https://images.example/item.jpg",
            "url": "https://www.vinted.co.uk/items/101",
        }
    )

    assert "EXCELLENT DEAL" in rating
    assert captured["payload"]["model"] == "gpt-5.6-terra"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["text"]["format"]["type"] == "json_schema"
    assert "tools" not in captured["payload"]
    assert (
        "Asking price: 0 GBP" in captured["payload"]["input"][1]["content"][0]["text"]
    )
    assert captured["headers"] == {"Authorization": "Bearer test-key"}


def test_evaluate_classifies_configuration_and_http_failures(monkeypatch):
    monkeypatch.delenv("VN_OPENAI_API_KEY", raising=False)
    with pytest.raises(evaluator.AIConfigurationError):
        evaluator.evaluate({})

    monkeypatch.setenv("VN_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(evaluator.requests, "post", lambda *a, **k: _Response(429))
    with pytest.raises(evaluator.AITransientError):
        evaluator.evaluate({})

    monkeypatch.setattr(evaluator.requests, "post", lambda *a, **k: _Response(401))
    with pytest.raises(evaluator.AIPermanentError):
        evaluator.evaluate({})


def test_verdict_truncation_never_splits_an_html_entity():
    reason = "a" * 158 + "<" + "zz"
    rating = evaluator.format_verdict(json.dumps({"verdict": "good", "reason": reason}))

    assert "&lt;…" in rating
    assert "&l…" not in rating


def test_worker_waits_for_first_primary_ack(monkeypatch):
    saved = []
    monkeypatch.setattr(app.db, "is_notification_pending", lambda _id: True)
    monkeypatch.setattr(
        app.db,
        "reschedule_ai_evaluation",
        lambda *args: saved.append(args) or True,
    )
    monkeypatch.setattr(
        app.db,
        "get_query_delivery_state",
        lambda _id: (True, ["111", "222"]),
    )

    result = app.process_ai_evaluation_job(
        _job(parent_notification_id=55, delivered_chat_ids_json="[]")
    )

    assert result == "waiting"
    assert saved[0][0] == 7
    assert saved[0][1] == 0  # waiting does not consume an API retry


def test_worker_intersects_recipients_and_ignores_pause_after_discovery(monkeypatch):
    settled = []
    monkeypatch.setattr(
        app.db,
        "get_query_delivery_state",
        lambda _id: (False, ["111", "333"]),  # paused, but 111 still subscribed
    )
    monkeypatch.setattr(
        evaluator,
        "evaluate",
        lambda _item: "🔥 <b>AI: EXCELLENT DEAL</b>",
    )
    monkeypatch.setattr(app.db, "begin_ai_evaluation", lambda _id: "started")
    monkeypatch.setattr(
        app.db,
        "settle_ai_evaluation_recipients",
        lambda *args, **kwargs: settled.append((args, kwargs)) or True,
    )

    result = app.process_ai_evaluation_job(_job(evaluation_started_at=1))

    assert result == "completed"
    args, kwargs = settled[0]
    assert args == (7,)
    assert kwargs["eligible_chat_ids"] == ["111"]
    assert kwargs["handled_chat_ids"] == ["111", "222"]
    assert "Pooky &lt;Lamp&gt;" in kwargs["result_content"]


def test_worker_retries_transient_error_with_bounded_attempt_count(monkeypatch):
    retries = []
    monkeypatch.setattr(
        app.db,
        "get_query_delivery_state",
        lambda _id: (True, ["111", "222"]),
    )
    monkeypatch.setattr(app.db, "begin_ai_evaluation", lambda _id: "started")
    monkeypatch.setattr(
        evaluator,
        "evaluate",
        lambda _item: (_ for _ in ()).throw(evaluator.AITransientError("temporary")),
    )
    monkeypatch.setattr(
        app.db,
        "reschedule_ai_evaluation",
        lambda *args: retries.append(args) or True,
    )

    result = app.process_ai_evaluation_job(_job(attempts=0))

    assert result == "rescheduled"
    assert retries[0][0:2] == (7, 1)


def test_worker_drops_permanent_configuration_failure(monkeypatch):
    completed = []
    monkeypatch.setattr(
        app.db,
        "get_query_delivery_state",
        lambda _id: (True, ["111", "222"]),
    )
    monkeypatch.setattr(app.db, "begin_ai_evaluation", lambda _id: "started")
    monkeypatch.setattr(
        evaluator,
        "evaluate",
        lambda _item: (_ for _ in ()).throw(
            evaluator.AIConfigurationError("missing configuration")
        ),
    )
    monkeypatch.setattr(
        app.db,
        "complete_ai_evaluation",
        lambda *args, **kwargs: completed.append((args, kwargs)) or True,
    )

    result = app.process_ai_evaluation_job(_job())

    assert result == "dropped"
    assert completed == [((7,), {})]


def test_core_ai_snapshot_contains_only_serialisable_primitives():
    class Item:
        title = "Lamp"
        brand_title = "Pooky"
        condition = "New"
        price = 0
        currency = "GBP"
        photo = None
        url = "https://www.vinted.co.uk/items/101"

    snapshot = core._ai_evaluation_snapshot(Item())

    assert snapshot == {
        "title": "Lamp",
        "brand": "Pooky",
        "condition": "New",
        "price": "0",
        "currency": "GBP",
        "photo_url": None,
        "item_url": "https://www.vinted.co.uk/items/101",
    }
