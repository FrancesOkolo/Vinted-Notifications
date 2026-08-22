import importlib
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

import core
import scraper_rate
import vinted_notifications as app


def _active_queries(count):
    return [
        (
            query_id,
            f"https://www.vinted.co.uk/catalog?search_text=item-{query_id}",
            None,
            f"Item {query_id}",
        )
        for query_id in range(1, count + 1)
    ]


def _patch_reconcile_dependencies(monkeypatch, preferences, query_count=1):
    monkeypatch.setattr(
        app.db,
        "get_queries",
        lambda enabled_only=False, raise_errors=False: _active_queries(query_count),
    )
    monkeypatch.setattr(app, "_get_query_preferences", preferences)
    monkeypatch.setattr(app, "_scrape_intervals", lambda: (180, 90))
    monkeypatch.setattr(app, "_scraper_request_spacing_seconds", lambda: 12)
    monkeypatch.setattr(core, "record_scraper_heartbeat", lambda: None)
    monkeypatch.setattr(app.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(app.logger, "warning", lambda *args, **kwargs: None)


def _job_ids(scheduler):
    return [job.id for job in scheduler.get_jobs()]


def test_shared_budget_preserves_fast_priority_and_reserves_normal_capacity():
    normal_only = scraper_rate.build_cadence_plan(40, 0, 180, 90, 12)
    assert normal_only["effective_normal_seconds"] == 600
    assert normal_only["scheduled_requests_per_minute"] <= 4

    typical_mix = scraper_rate.build_cadence_plan(38, 2, 180, 90, 12)
    assert typical_mix["effective_fast_seconds"] == 90
    assert 855 <= typical_mix["effective_normal_seconds"] <= 856
    assert (
        typical_mix["scheduled_requests_per_minute"]
        <= typical_mix["scheduled_capacity_per_minute"] + 1e-12
    )

    overloaded_mix = scraper_rate.build_cadence_plan(120, 5, 180, 90, 12)
    assert overloaded_mix["effective_fast_seconds"] == 100
    assert overloaded_mix["effective_normal_seconds"] >= 7200
    assert (
        overloaded_mix["effective_fast_seconds"]
        < overloaded_mix["effective_normal_seconds"]
    )
    assert (
        overloaded_mix["scheduled_requests_per_minute"]
        <= overloaded_mix["scheduled_capacity_per_minute"] + 1e-12
    )


def test_request_spacing_is_bounded_to_safe_values():
    assert scraper_rate.bounded_request_spacing(None) == 12
    assert scraper_rate.bounded_request_spacing("invalid") == 12
    assert scraper_rate.bounded_request_spacing(1) == 12
    assert scraper_rate.bounded_request_spacing(30) == 30
    assert scraper_rate.bounded_request_spacing(999) == 120


def test_reconcile_applies_poll_mode_changes_without_per_query_jobs(monkeypatch):
    preferences = {1: {"poll_mode": "normal"}}
    _patch_reconcile_dependencies(
        monkeypatch,
        lambda query_ids: {query_id: preferences[query_id] for query_id in query_ids},
    )
    scheduler = BackgroundScheduler(timezone=timezone.utc)
    initial_now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    try:
        initial = app._reconcile_scraper_jobs(scheduler, object(), now=initial_now)
        initial_plan = getattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE)
        assert initial["changed"] >= 1
        assert initial_plan["queries"][1]["interval"] == 180
        assert _job_ids(scheduler) == [app._SCRAPER_DISPATCH_JOB_ID]
        assert (
            app._job_interval_seconds(scheduler.get_job(app._SCRAPER_DISPATCH_JOB_ID))
            == 12
        )

        preferences[1]["poll_mode"] = "fast"
        fast_now = datetime(2026, 8, 9, 12, 1, tzinfo=timezone.utc)
        fast = app._reconcile_scraper_jobs(scheduler, object(), now=fast_now)
        fast_plan = getattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE)

        assert fast["changed"] >= 1
        assert fast_plan["queries"][1]["mode"] == "fast"
        assert fast_plan["queries"][1]["interval"] == 90
        assert _job_ids(scheduler) == [app._SCRAPER_DISPATCH_JOB_ID]
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_initial_preference_failure_keeps_retry_heartbeat_then_recovers(
    monkeypatch,
):
    reads = iter([None, {1: {"poll_mode": "fast"}}])
    _patch_reconcile_dependencies(monkeypatch, lambda query_ids: next(reads))
    monkeypatch.setattr(
        core, "get_scraper_cooldown", lambda now=None: {"active": False}
    )
    monkeypatch.setattr(core, "_quiet_hours_active", lambda: False)
    calls = []
    monkeypatch.setattr(
        core,
        "process_items",
        lambda items_queue, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(app.time, "time", lambda: 1000.0)
    scheduler = BackgroundScheduler(timezone=timezone.utc)

    try:
        first = app._reconcile_scraper_jobs(scheduler, object(), now=988)
        assert first["changed"] == 1
        assert not hasattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE)
        assert _job_ids(scheduler) == [app._SCRAPER_DISPATCH_JOB_ID]
        assert calls == []

        dispatched = app._run_scraper_dispatch(scheduler, object())
        plan = getattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE)
        assert dispatched == 1
        assert plan["queries"][1]["mode"] == "fast"
        assert calls == [{"query_ids": [1], "monitor_during_quiet_hours": False}]
        assert _job_ids(scheduler) == [app._SCRAPER_DISPATCH_JOB_ID]
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_transient_query_read_failure_keeps_plan_and_suppresses_dispatch(
    monkeypatch,
):
    preferences = {1: {"poll_mode": "normal"}}
    _patch_reconcile_dependencies(
        monkeypatch,
        lambda query_ids: {
            query_id: preferences[query_id] for query_id in query_ids
        },
    )
    monkeypatch.setattr(core, "get_scraper_cooldown", lambda now=None: {"active": False})
    monkeypatch.setattr(core, "_quiet_hours_active", lambda: False)
    calls = []
    monkeypatch.setattr(
        core,
        "process_items",
        lambda items_queue, **kwargs: calls.append(kwargs),
    )
    scheduler = BackgroundScheduler(timezone=timezone.utc)

    try:
        initial = app._reconcile_scraper_jobs(scheduler, object(), now=1000)
        old_plan = getattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE)
        assert initial["reconciled"] is True

        def fail_query_read(**kwargs):
            raise RuntimeError("temporary database failure")

        monkeypatch.setattr(app.db, "get_queries", fail_query_read)
        failed = app._reconcile_scraper_jobs(scheduler, object(), now=1012)
        assert failed["reconciled"] is False
        assert getattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE) is old_plan
        assert _job_ids(scheduler) == [app._SCRAPER_DISPATCH_JOB_ID]

        assert app._run_scraper_dispatch(scheduler, object()) is None
        assert calls == []
        assert getattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE) is old_plan
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_dispatch_coalesces_overdue_work_and_prevents_normal_starvation(monkeypatch):
    scheduler = BackgroundScheduler(timezone=timezone.utc)
    plan = {
        "queries": {
            **{
                query_id: {
                    "mode": "fast",
                    "monitor_during_quiet_hours": False,
                    "interval": 90,
                    "last_started": None,
                    "next_due": 0,
                }
                for query_id in range(1, 5)
            },
            9: {
                "mode": "normal",
                "monitor_during_quiet_hours": False,
                "interval": 600,
                "last_started": None,
                "next_due": 0,
            },
        },
        "fast_streak": 0,
    }
    setattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE, plan)
    monkeypatch.setattr(
        core, "get_scraper_cooldown", lambda now=None: {"active": False}
    )
    monkeypatch.setattr(core, "_quiet_hours_active", lambda: False)
    calls = []
    monkeypatch.setattr(
        core,
        "process_items",
        lambda items_queue, **kwargs: calls.append(kwargs),
    )

    selected = [
        app._dispatch_due_query(scheduler, object(), now=1000) for _ in range(5)
    ]

    assert selected == [1, 2, 3, 9, 4]
    assert [call["query_ids"] for call in calls] == [[1], [2], [3], [9], [4]]
    assert all(len(call["query_ids"]) == 1 for call in calls)
    assert plan["queries"][1]["next_due"] == 1090
    assert plan["queries"][9]["next_due"] == 1600


def test_dispatch_respects_cooldown_and_quiet_hours_without_backlog(monkeypatch):
    scheduler = BackgroundScheduler(timezone=timezone.utc)
    plan = {
        "queries": {
            1: {
                "mode": "normal",
                "monitor_during_quiet_hours": False,
                "interval": 600,
                "last_started": None,
                "next_due": 0,
            },
            2: {
                "mode": "fast",
                "monitor_during_quiet_hours": True,
                "interval": 90,
                "last_started": None,
                "next_due": 0,
            },
        },
        "fast_streak": 0,
    }
    setattr(scheduler, app._SCRAPER_PLAN_ATTRIBUTE, plan)
    cooldown = {"active": True}
    monkeypatch.setattr(core, "get_scraper_cooldown", lambda now=None: cooldown)
    monkeypatch.setattr(core, "_quiet_hours_active", lambda: True)
    calls = []
    monkeypatch.setattr(
        core,
        "process_items",
        lambda items_queue, **kwargs: calls.append(kwargs),
    )

    assert app._dispatch_due_query(scheduler, object(), now=1000) is None
    assert calls == []
    assert plan["queries"][1]["next_due"] == 0
    assert plan["queries"][2]["next_due"] == 0

    cooldown["active"] = False
    assert app._dispatch_due_query(scheduler, object(), now=1000) == 2
    assert calls == [{"query_ids": [2], "monitor_during_quiet_hours": True}]
    assert plan["queries"][1]["next_due"] == 1600
    assert plan["queries"][2]["next_due"] == 1090


def test_child_gate_configurator_forwards_full_lease_state(monkeypatch):
    requester_module = importlib.import_module("pyVintedVN.requester")

    captured = []
    monkeypatch.setattr(
        requester_module,
        "configure_shared_request_gate",
        lambda *args: captured.append(args),
    )
    gate_state = tuple(object() for _ in range(5))

    app._configure_vinted_request_gate(*gate_state)

    assert captured == [gate_state]


def test_process_restarts_forward_complete_shared_request_gate(monkeypatch):
    class FakeProcess:
        def __init__(self, target, args):
            self.target = target
            self.args = args
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    created = []

    def fake_create_process(*, target, args=()):
        process = FakeProcess(target, args)
        created.append(process)
        return process

    gate_state = tuple(object() for _ in range(5))
    gate_names = (
        "vinted_request_gate_lock",
        "vinted_request_next_allowed",
        "vinted_request_lease_until",
        "vinted_request_owner_counter",
        "vinted_request_current_owner",
    )
    for name, value in zip(gate_names, gate_state):
        monkeypatch.setattr(app, name, value)

    monkeypatch.setattr(app, "_create_process", fake_create_process)
    monkeypatch.setattr(app, "scrape_process", None)
    monkeypatch.setattr(app, "item_extractor_process", None)
    items_queue = object()
    new_items_queue = object()

    app.ensure_scrape_process_alive(items_queue)
    app.ensure_item_extractor_process_alive(items_queue, new_items_queue)

    assert len(created) == 2
    assert created[0].target is app.scraper_process
    assert created[0].args == (items_queue, *gate_state)
    assert created[0].started is True
    assert created[1].target is app.item_extractor
    assert created[1].args == (items_queue, new_items_queue, *gate_state)
    assert created[1].started is True

    app.ensure_scrape_process_alive(items_queue)
    app.ensure_item_extractor_process_alive(items_queue, new_items_queue)
    assert len(created) == 2


def test_monitor_processes_monitors_item_extractor_queue_path(monkeypatch):
    calls = []
    items_queue = object()
    new_items_queue = object()
    telegram_queue = object()
    rss_queue = object()

    monkeypatch.setattr(
        app,
        "ensure_scrape_process_alive",
        lambda queue: calls.append(("scraper", queue)),
    )
    monkeypatch.setattr(
        app,
        "ensure_item_extractor_process_alive",
        lambda source, destination: calls.append(
            ("item_extractor", source, destination)
        ),
    )
    monkeypatch.setattr(app, "ensure_ai_evaluator_process_alive", lambda: None)
    monkeypatch.setattr(app, "check_refresh_delay", lambda queue: None)
    monkeypatch.setattr(app, "check_scraper_watchdog", lambda: None)
    monkeypatch.setattr(app, "telegram_process", None)
    monkeypatch.setattr(app, "rss_process", None)
    monkeypatch.setattr(
        app.db,
        "get_parameter",
        lambda name: "False"
        if name in {"telegram_process_running", "rss_process_running"}
        else None,
    )

    app.monitor_processes(
        items_queue,
        new_items_queue,
        telegram_queue,
        rss_queue,
    )

    assert calls == [
        ("scraper", items_queue),
        ("item_extractor", items_queue, new_items_queue),
    ]
