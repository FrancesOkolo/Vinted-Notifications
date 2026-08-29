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
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "sources": [
                            {
                                "type": "url",
                                "url": (
                                    "https://www.pooky.com/products/lamp"
                                    "?colour=green&size=small"
                                ),
                                "title": "Pooky lamp",
                            },
                            {
                                "type": "url",
                                "url": "https://www.ebay.co.uk/sch/pooky-lamp",
                                "title": "Pooky lamp resale listings",
                            },
                        ],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"benchmark_price":100,'
                                '"benchmark_currency":"GBP",'
                                '"benchmark_basis":"typical used resale"}'
                            ),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": (
                                        "https://www.pooky.com/products/lamp"
                                        "?colour=green&size=small"
                                    ),
                                    "title": "Pooky lamp",
                                }
                            ],
                        }
                    ],
                },
            ],
        }

    def json(self):
        return self._data


def test_evaluate_uses_web_comparisons_without_price_anchoring(monkeypatch):
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

    assert "GREAT DEAL" in rating
    assert '"benchmark_price":100' in evaluator._response_text(_Response().json())
    assert captured["payload"]["model"] == "gpt-5.6-terra"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["text"]["format"]["type"] == "json_schema"
    schema = captured["payload"]["text"]["format"]["schema"]
    assert set(schema["required"]) == {
        "benchmark_price",
        "benchmark_currency",
        "benchmark_basis",
    }
    assert "verdict" not in schema["properties"]
    assert captured["payload"]["tools"] == [
        {
            "type": "web_search",
            "user_location": {"type": "approximate", "country": "GB"},
        }
    ]
    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["include"] == ["web_search_call.action.sources"]
    item_text = captured["payload"]["input"][1]["content"][0]["text"]
    assert "Asking price" not in item_text
    assert "https://www.vinted.co.uk/items/101" not in item_text
    assert "Benchmark currency: GBP" in item_text
    assert (
        'href="https://www.pooky.com/products/lamp?colour=green&amp;size=small"'
        in rating
    )
    assert "Sources checked:" in rating
    assert captured["headers"] == {"Authorization": "Bearer test-key"}
    system_prompt = captured["payload"]["input"][0]["content"]
    assert "Search the web for live UK price evidence" in system_prompt
    assert "Do not use the target Vinted listing as evidence" in system_prompt
    assert "under 50% saving = don't buy" in system_prompt
    assert "50% through exactly 65% saving = good" in system_prompt
    assert "strictly over 65% saving = great" in system_prompt


@pytest.mark.parametrize(
    ("asking_price", "expected_label", "expected_comparison"),
    [
        ("75", "DON'T BUY", "25% saving"),
        ("50.01", "DON'T BUY", "49.99% saving"),
        ("50", "GOOD DEAL", "50% saving"),
        ("35", "GOOD DEAL", "65% saving"),
        ("34.99", "GREAT DEAL", "65.01% saving"),
        ("34", "GREAT DEAL", "66% saving"),
    ],
)
def test_ai_benchmark_uses_exact_personal_saving_thresholds(
    asking_price,
    expected_label,
    expected_comparison,
):
    raw = (
        '{"benchmark_price":100,"benchmark_currency":"GBP",'
        '"benchmark_basis":"typical used resale"}'
    )

    rating = evaluator.format_evaluation(raw, asking_price, "GBP")

    assert expected_label in rating
    assert expected_comparison in rating
    assert f"£{asking_price} vs £100" in rating


def test_ai_benchmark_rejects_a_currency_mismatch():
    raw = (
        '{"benchmark_price":100,"benchmark_currency":"USD",'
        '"benchmark_basis":"typical used resale"}'
    )

    assert evaluator.format_evaluation(raw, "25", "GBP") is None


def test_response_sources_prioritise_citations_deduplicate_and_cap():
    cited_url = "https://shop.example/item?a=1&b=2"
    data = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "{}",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": cited_url,
                                "title": "Best <matching> product",
                            },
                            {
                                "type": "url_citation",
                                "url": "javascript:alert(1)",
                                "title": "unsafe",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://user:password@evil.example/item",
                                "title": "credentials",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://bad-port.example:99999/item",
                                "title": "bad port",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://backslash.example\\item",
                                "title": "backslash",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://control.example/\x00item",
                                "title": "control",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://long.example/" + ("x" * 600),
                                "title": "too long",
                            },
                        ],
                    }
                ],
            },
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "sources": [
                        {"url": cited_url, "title": "duplicate"},
                        {"url": "https://resale.example/one", "title": "Used one"},
                        {"url": "https://retail.example/two", "title": "New two"},
                        {"url": "https://extra.example/three", "title": "Extra"},
                    ],
                },
            },
        ]
    }

    sources = evaluator._response_sources(data)

    assert [source["url"] for source in sources] == [
        cited_url,
        "https://resale.example/one",
        "https://retail.example/two",
    ]
    rendered = evaluator._format_sources(sources)
    assert 'href="https://shop.example/item?a=1&amp;b=2"' in rendered
    assert "Best &lt;matching&gt; product" in rendered
    assert "javascript:" not in rendered
    assert "evil.example" not in rendered
    assert "bad-port.example" not in rendered
    assert "backslash.example" not in rendered
    assert "control.example" not in rendered
    assert "long.example" not in rendered


def test_source_block_stays_within_telegram_budget():
    sources = [
        {
            "url": f"https://source-{index}.example/" + ("x" * 470),
            "title": "T" * 72,
        }
        for index in range(3)
    ]

    rendered = evaluator._format_sources(sources)

    assert rendered
    assert len(rendered) <= evaluator._MAX_SOURCE_BLOCK_CHARS


def test_evaluate_retries_when_web_search_returns_no_sources(monkeypatch):
    monkeypatch.setenv("VN_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        evaluator.requests,
        "post",
        lambda *args, **kwargs: _Response(
            data={
                "output_text": (
                    '{"benchmark_price":100,'
                    '"benchmark_currency":"GBP",'
                    '"benchmark_basis":"typical used resale"}'
                )
            }
        ),
    )

    with pytest.raises(evaluator.AITransientError, match="no comparison sources"):
        evaluator.evaluate({"price": "25", "currency": "GBP"})


def test_evaluate_classifies_configuration_and_http_failures(monkeypatch):
    monkeypatch.delenv("VN_OPENAI_API_KEY", raising=False)
    with pytest.raises(evaluator.AIConfigurationError):
        evaluator.evaluate({})

    monkeypatch.setenv("VN_OPENAI_API_KEY", "test-key")
    with pytest.raises(evaluator.AIPermanentError):
        evaluator.evaluate({})

    called = []
    monkeypatch.setattr(
        evaluator.requests,
        "post",
        lambda *args, **kwargs: called.append(True),
    )
    with pytest.raises(evaluator.AIPermanentError, match="currency"):
        evaluator.evaluate({"price": "10"})
    assert not called

    item = {"price": "10", "currency": "GBP"}
    monkeypatch.setattr(evaluator.requests, "post", lambda *a, **k: _Response(429))
    with pytest.raises(evaluator.AITransientError):
        evaluator.evaluate(item)

    monkeypatch.setattr(evaluator.requests, "post", lambda *a, **k: _Response(401))
    with pytest.raises(evaluator.AIPermanentError):
        evaluator.evaluate(item)


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
