import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.configure_database_runtime()
    return database_path


def test_safety_migration_upgrades_fast_values_and_preserves_slower_ones(database):
    db.set_parameter("catalogue_request_spacing_seconds", "15")
    assert db.migrate_scraper_safety_parameters()
    assert db.get_parameter("catalogue_request_spacing_seconds") == "60"
    assert db.get_scraper_pause_state(now=100)["active"] is False

    db.set_parameter("catalogue_request_spacing_seconds", "90")
    assert db.migrate_scraper_safety_parameters()
    assert db.get_parameter("catalogue_request_spacing_seconds") == "90"


@pytest.mark.parametrize(
    "missing_key",
    [
        "scraper_pause_active",
        "scraper_pause_until",
        "scraper_pause_reason",
        "scraper_pause_started_at",
    ],
)
def test_missing_pause_parameter_fails_closed(database, missing_key):
    conn = db.get_db_connection()
    try:
        conn.execute("DELETE FROM parameters WHERE key = ?", (missing_key,))
        conn.commit()
    finally:
        conn.close()

    assert db.get_scraper_pause_state(now=100) is None
    assert core.get_scraper_pause(now=100)["active"] is True
    assert core.get_scraper_pause(now=100)["available"] is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("scraper_pause_active", "perhaps"),
        ("scraper_pause_until", "soon"),
        ("scraper_pause_until", "-1"),
        ("scraper_pause_started_at", "recently"),
        ("scraper_pause_started_at", "-1"),
    ],
)
def test_malformed_pause_parameter_fails_closed(database, key, value):
    db.set_parameter(key, value)

    assert db.get_scraper_pause_state(now=100) is None
    pause = core.get_scraper_pause(now=100)
    assert pause["active"] is True
    assert pause["available"] is False


def test_timed_and_indefinite_pause_survive_fresh_reads(database):
    indefinite = db.set_scraper_pause(
        duration_seconds=None,
        reason="phone_blocked",
        now=100,
    )
    assert indefinite == {
        "active": True,
        "until": 0,
        "remaining": None,
        "reason": "phone_blocked",
        "started_at": 100,
    }
    assert db.get_scraper_pause_state(now=100_000)["active"] is True
    assert db.clear_scraper_pause()
    assert db.get_scraper_pause_state(now=100_000)["active"] is False

    timed = db.set_scraper_pause(
        duration_seconds=3600,
        reason="manual_1h",
        now=200,
    )
    assert timed["until"] == 3800
    assert timed["remaining"] == 3600
    assert db.get_scraper_pause_state(now=3799)["active"] is True
    expired = db.get_scraper_pause_state(now=3800)
    assert expired["active"] is False
    assert expired["remaining"] == 0


def test_core_pause_status_fails_closed_when_database_state_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(db, "get_scraper_pause_state", lambda now=None: None)
    pause = core.get_scraper_pause(now=123)
    assert pause["active"] is True
    assert pause["available"] is False


def test_pause_reports_success_only_after_inflight_request_drains(
    database,
    monkeypatch,
):
    calls = []

    def wait_for_idle():
        assert db.get_scraper_pause_state(now=100)["active"] is True
        calls.append("drained")
        return True

    monkeypatch.setattr(core, "wait_for_shared_request_idle", wait_for_idle)
    state = core.pause_scraper(
        duration_seconds=3600,
        reason="manual_1h",
        now=100,
    )

    assert calls == ["drained"]
    assert state["active"] is True
    assert state["remaining"] == 3600


@pytest.mark.parametrize("pause_state", [None, {"active": True}])
def test_low_level_gate_suppresses_every_vinted_request_when_paused(
    monkeypatch,
    pause_state,
):
    requester = importlib.import_module("pyVintedVN.requester")
    calls = []

    class Session:
        def get(self, url, params=None, timeout=None):
            calls.append(("get", url))
            return object()

        def head(self, url, params=None, timeout=None):
            calls.append(("head", url))
            return object()

    monkeypatch.setattr(
        requester.db,
        "get_scraper_pause_state",
        lambda: pause_state,
    )
    requester.configure_shared_request_gate(None, None)
    requester._reset_catalogue_request_gate()
    try:
        assert (
            requester._session_request(
                Session(),
                "get",
                "https://www.vinted.co.uk/api/v2/catalog/items",
            )
            is None
        )
        assert (
            requester._session_request(
                Session(),
                "head",
                "https://www.vinted.co.uk/",
                force_gate=True,
            )
            is None
        )
    finally:
        requester._reset_catalogue_request_gate()
    assert calls == []


def test_pause_does_not_stop_non_vinted_services(monkeypatch):
    requester = importlib.import_module("pyVintedVN.requester")
    calls = []
    response = object()

    class Session:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            return response

    monkeypatch.setattr(
        requester.db,
        "get_scraper_pause_state",
        lambda: {"active": True},
    )
    result = requester._session_request(
        Session(),
        "get",
        "https://api.telegram.org/example",
    )
    assert result is response
    assert calls == ["https://api.telegram.org/example"]


def test_loop_top_pause_does_not_increment_failed_cycle(database, monkeypatch):
    db.set_parameter("quiet_hours_enabled", "False")
    conn = db.get_db_connection()
    try:
        conn.execute(
            "INSERT INTO queries(query, query_name) VALUES (?, ?)",
            ("https://www.vinted.co.uk/catalog?search_text=safe", "Safe"),
        )
        conn.commit()
    finally:
        conn.close()

    pause_reads = iter(
        [
            {
                "active": False,
                "available": True,
                "until": 0,
                "remaining": 0,
                "reason": "",
                "started_at": 0,
            },
            {
                "active": True,
                "available": True,
                "until": 0,
                "remaining": None,
                "reason": "phone_blocked",
                "started_at": 100,
            },
        ]
    )
    monkeypatch.setattr(core, "get_scraper_pause", lambda now=None: next(pause_reads))

    class Items:
        @staticmethod
        def search(url, nbr_items):
            pytest.fail("Pause should stop the cycle before any Vinted request")

    class FakeVinted:
        def __init__(self):
            self.items = Items()

    monkeypatch.setattr(core, "Vinted", FakeVinted)
    core.process_items(object())

    assert db.get_parameter("scraper_failed_cycles") in (None, "0")
