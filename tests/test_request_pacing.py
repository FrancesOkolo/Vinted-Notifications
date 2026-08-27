from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import db

ROOT = Path(__file__).resolve().parents[1]


class SharedValue:
    def __init__(self, value=0):
        self.value = value


def _configure_test_shared_gate(requester_module, lock=None):
    lock = lock or threading.Lock()
    state = {
        "next_allowed": SharedValue(0.0),
        "lease_until": SharedValue(0.0),
        "owner_counter": SharedValue(0),
        "current_owner": SharedValue(0),
    }
    requester_module.configure_shared_request_gate(
        lock,
        state["next_allowed"],
        state["lease_until"],
        state["owner_counter"],
        state["current_owner"],
    )
    return state


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    yield database_path


def test_403_rebuild_head_and_retry_share_one_process_wide_gate(
    database,
    monkeypatch,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    clock = [0.0]
    waits = []
    events = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Cookies:
        def clear_session_cookies(self):
            return None

    class Session:
        def __init__(self, name, get_status):
            self.name = name
            self.get_status = get_status
            self.headers = {}
            self.cookies = Cookies()
            self.closed = False

        def get(self, *args, **kwargs):
            events.append((f"{self.name}.get", clock[0]))
            return Response(self.get_status)

        def head(self, *args, **kwargs):
            events.append((f"{self.name}.head", clock[0]))
            return Response(200)

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    monkeypatch.setattr(
        requester_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep),
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)

    client = requester_module.Requester()
    rejected = Session("rejected", 403)
    fresh = Session("fresh", 200)
    client.session = rejected
    monkeypatch.setattr(requester_module.requests, "Session", lambda: fresh)

    # This test exercises the process-local pacing branch, so ensure no shared
    # cross-process gate is left configured by an earlier test (mirrors the
    # setup in test_get_once_makes_one_paced_request_without_auth_retry).
    requester_module.configure_shared_request_gate(None, None)
    requester_module._reset_catalogue_request_gate()
    try:
        response = client.get(
            "https://www.vinted.co.uk/api/v2/catalog/items",
        )
    finally:
        requester_module._reset_catalogue_request_gate()

    assert response.status_code == 200
    assert rejected.closed is True
    assert events == [
        ("rejected.get", 0.0),
        ("fresh.head", 60.0),
        ("fresh.get", 120.0),
    ]
    assert requester_module.FORBIDDEN_RETRY_DELAY_SECONDS in waits
    assert sum(waits) == 120.0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.vinted.co.uk/api/v2/catalog/items", True),
        ("https://VINTED.fr./api/v2/users/1", True),
        ("https://vinted.example/one-shot", True),
        ("https://notvinted.example/catalog", False),
        ("https://example.com/?next=https://vinted.fr", False),
        ("https://vinted.fr@example.com/", False),
        ("not a URL containing vinted.fr", False),
    ],
)
def test_vinted_request_detection_requires_an_exact_hostname_label(url, expected):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    assert requester_module._is_vinted_request(url) is expected


def test_catalogue_and_profile_calls_share_parent_owned_gate(
    database,
    monkeypatch,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    clock = [0.0]
    starts = []

    class Response:
        status_code = 200

    class Session:
        def get(self, url, **kwargs):
            starts.append((url, clock[0]))
            return Response()

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(
        requester_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep),
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(
        requester_module,
        "catalogue_request_spacing_seconds",
        lambda: 12,
    )
    _configure_test_shared_gate(requester_module)
    requester_module._reset_catalogue_request_gate()
    try:
        requester_module._session_request(
            Session(),
            "get",
            "https://www.vinted.co.uk/api/v2/catalog/items",
        )
        # A spawned child begins with new process-local state, but receives the
        # same parent-owned lock/value. Resetting only local state models that.
        requester_module._reset_catalogue_request_gate()
        requester_module._session_request(
            Session(),
            "get",
            "https://www.vinted.fr/api/v2/users/1",
        )
        # An unrelated host is not held behind the Vinted gate.
        requester_module._session_request(
            Session(),
            "get",
            "https://example.com/health",
        )
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()

    assert starts == [
        ("https://www.vinted.co.uk/api/v2/catalog/items", 0.0),
        ("https://www.vinted.fr/api/v2/users/1", 12.0),
        ("https://example.com/health", 12.0),
    ]


def test_shared_multiprocessing_lock_is_not_held_during_http(
    database,
    monkeypatch,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")

    class RecordingLock:
        held = False

        def __enter__(self):
            assert self.held is False
            self.held = True
            return self

        def __exit__(self, *args):
            self.held = False
            return False

    class Response:
        status_code = 200

    lock = RecordingLock()

    class Session:
        def get(self, url, **kwargs):
            assert lock.held is False
            return Response()

    monkeypatch.setattr(
        requester_module,
        "time",
        SimpleNamespace(monotonic=lambda: 100.0, sleep=time.sleep),
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(
        requester_module,
        "catalogue_request_spacing_seconds",
        lambda: 12,
    )
    _configure_test_shared_gate(requester_module, lock=lock)
    requester_module._reset_catalogue_request_gate()
    try:
        requester_module._session_request(
            Session(),
            "get",
            "https://www.vinted.co.uk/api/v2/catalog/items",
        )
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()

    assert lock.held is False


def test_live_lease_prevents_overlap_when_request_exceeds_spacing(
    database,
    monkeypatch,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    monkeypatch.setattr(
        requester_module,
        "catalogue_request_spacing_seconds",
        lambda: 0.05,
    )
    monkeypatch.setattr(requester_module, "SHARED_REQUEST_LEASE_SECONDS", 1.0)
    monkeypatch.setattr(
        requester_module,
        "SHARED_REQUEST_GATE_POLL_SECONDS",
        0.005,
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)
    state = _configure_test_shared_gate(requester_module)
    requester_module._reset_catalogue_request_gate()
    second = {}

    try:
        first_token = requester_module._wait_for_shared_request_slot()

        def acquire_second_process_slot():
            second["token"] = requester_module._wait_for_shared_request_slot()
            second["acquired_at"] = time.monotonic()

        waiter = threading.Thread(target=acquire_second_process_slot)
        waiter.start()
        # The first request remains in flight longer than the configured gap.
        time.sleep(0.06)
        assert waiter.is_alive()
        assert state["current_owner"].value == first_token

        completed_at = time.monotonic()
        assert requester_module._mark_shared_request_completed(first_token) is True
        waiter.join(timeout=1)
        assert waiter.is_alive() is False
        assert second["acquired_at"] >= completed_at + 0.045
        requester_module._cancel_shared_request_slot(second["token"])
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()


def test_expired_lease_recovers_and_late_completion_cannot_clear_new_owner(
    database,
    monkeypatch,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    clock = [0.0]
    monkeypatch.setattr(
        requester_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: None),
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(
        requester_module,
        "catalogue_request_spacing_seconds",
        lambda: 12,
    )
    state = _configure_test_shared_gate(requester_module)
    requester_module._reset_catalogue_request_gate()

    try:
        stale_token = requester_module._wait_for_shared_request_slot()
        assert state["lease_until"].value == 120.0

        clock[0] = 121.0
        current_token = requester_module._wait_for_shared_request_slot()
        current_lease = state["lease_until"].value
        assert current_token != stale_token
        assert state["current_owner"].value == current_token

        assert requester_module._mark_shared_request_completed(stale_token) is False
        assert state["current_owner"].value == current_token
        assert state["lease_until"].value == current_lease
        assert state["next_allowed"].value == 0.0

        clock[0] = 130.0
        assert requester_module._mark_shared_request_completed(current_token) is True
        assert state["current_owner"].value == 0
        assert state["lease_until"].value == 0.0
        assert state["next_allowed"].value == 142.0
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()


def test_cancel_after_acquisition_releases_only_its_lease_without_gap(
    database,
    monkeypatch,
):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    clock = [0.0]
    monkeypatch.setattr(
        requester_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep),
    )
    monkeypatch.setattr(requester_module.random, "uniform", lambda low, high: 0.0)
    state = _configure_test_shared_gate(requester_module)
    checks = iter([False, False, False, True])

    class Session:
        def get(self, *args, **kwargs):
            pytest.fail("canceled request must not reach HTTP")

    try:
        response = requester_module._session_request(
            Session(),
            "get",
            "https://www.vinted.fr/api/v2/users/1",
            cancel_if=lambda: next(checks),
        )
        assert response is None
        assert state["owner_counter"].value == 1
        assert state["current_owner"].value == 0
        assert state["lease_until"].value == 0.0
        assert state["next_allowed"].value == 0.0
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()


def test_pause_boundary_waits_for_previously_reserved_request(database, monkeypatch):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    monkeypatch.setattr(
        requester_module,
        "SHARED_REQUEST_GATE_POLL_SECONDS",
        0.005,
    )
    state = _configure_test_shared_gate(requester_module)
    token = requester_module._wait_for_shared_request_slot()
    outcome = {}

    try:
        waiter = threading.Thread(
            target=lambda: outcome.setdefault(
                "drained",
                requester_module.wait_for_shared_request_idle(timeout=1),
            )
        )
        waiter.start()
        time.sleep(0.02)
        assert waiter.is_alive()
        assert state["current_owner"].value == token

        assert requester_module._mark_shared_request_completed(token) is True
        waiter.join(timeout=1)
        assert waiter.is_alive() is False
        assert outcome == {"drained": True}
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()


def test_vinted_redirect_is_never_followed_automatically(database):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    calls = []

    class Response:
        status_code = 302

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    response = Response()

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    requester_module.configure_shared_request_gate(None, None)
    requester_module._reset_catalogue_request_gate()
    try:
        with pytest.raises(requester_module.requests.exceptions.TooManyRedirects):
            requester_module._session_request(
                Session(),
                "get",
                "https://www.vinted.co.uk/api/v2/catalog/items",
            )
    finally:
        requester_module._reset_catalogue_request_gate()

    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert response.closed is True


def test_cancel_callback_failure_fails_closed_before_http(database, monkeypatch):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    state = _configure_test_shared_gate(requester_module)

    class Session:
        def get(self, *args, **kwargs):
            pytest.fail("failed cancel check must suppress optional HTTP")

    def failed_cancel_check():
        raise RuntimeError("cooldown database unavailable")

    try:
        response = requester_module._session_request(
            Session(),
            "get",
            "https://www.vinted.fr/api/v2/users/1",
            cancel_if=failed_cancel_check,
        )
        assert response is None
        assert state["owner_counter"].value == 0
        assert state["current_owner"].value == 0
    finally:
        requester_module.configure_shared_request_gate(None, None)
        requester_module._reset_catalogue_request_gate()


def test_get_once_makes_one_paced_request_without_auth_retry(database, monkeypatch):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")

    class Response:
        status_code = 403

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

    monkeypatch.setattr(
        requester_module.proxies,
        "configure_proxy",
        lambda session: False,
    )
    client = requester_module.Requester()
    client.session = Session()
    monkeypatch.setattr(
        client,
        "_rebuild_session",
        lambda: pytest.fail("get_once must not rebuild or retry the session"),
    )
    requester_module.configure_shared_request_gate(None, None)
    requester_module._reset_catalogue_request_gate()
    try:
        response = client.get_once("https://www.vinted.fr/api/v2/users/1")
    finally:
        requester_module._reset_catalogue_request_gate()

    assert response.status_code == 403
    assert client.session.calls == 1


def test_request_spacing_reader_falls_back_safely_on_db_failure(monkeypatch):
    import importlib

    requester_module = importlib.import_module("pyVintedVN.requester")
    monkeypatch.setattr(
        requester_module.db,
        "get_parameter",
        lambda key: (_ for _ in ()).throw(RuntimeError("temporary DB failure")),
    )

    assert requester_module.catalogue_request_spacing_seconds() == 60
