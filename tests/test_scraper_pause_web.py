import base64
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.configure_database_runtime()
    return database_path


@pytest.fixture
def web_client(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://example.invalid"),
    )
    return web, web.app.test_client()


def _basic_auth(username="admin", password="secret"):
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


def _csrf_token(client, path="/config", headers=None):
    response = client.get(path, headers=headers or {})
    assert response.status_code == 200
    return (
        re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        .group(1)
        .decode()
    )


@pytest.mark.parametrize(
    ("preset", "duration_seconds", "reason"),
    [
        ("phone_blocked", None, "phone_blocked"),
        ("1h", 60 * 60, "manual_1h"),
        ("6h", 6 * 60 * 60, "manual_6h"),
        ("24h", 24 * 60 * 60, "manual_24h"),
    ],
)
def test_scraper_pause_presets_are_csrf_protected_and_bounded(
    web_client,
    monkeypatch,
    preset,
    duration_seconds,
    reason,
):
    web, client = web_client
    calls = []

    def fake_pause_scraper(*, duration_seconds, reason):
        calls.append((duration_seconds, reason))
        return {
            "active": True,
            "available": True,
            "until": 0 if duration_seconds is None else 123 + duration_seconds,
            "remaining": duration_seconds,
            "reason": reason,
            "started_at": 123,
        }

    monkeypatch.setattr(web.core, "pause_scraper", fake_pause_scraper)
    assert client.post("/scraper/pause", json={"preset": preset}).status_code == 400

    token = _csrf_token(client)
    response = client.post(
        "/scraper/pause",
        json={"preset": preset},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json()["pause"]["active"] is True
    assert calls == [(duration_seconds, reason)]


def test_scraper_pause_rejects_arbitrary_durations(web_client):
    _web, client = web_client
    token = _csrf_token(client)

    response = client.post(
        "/scraper/pause",
        json={"preset": "999-years"},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_scraper_pause_requires_confirmed_active_state(web_client, monkeypatch):
    web, client = web_client
    monkeypatch.setattr(
        web.core,
        "pause_scraper",
        lambda **kwargs: {
            "active": False,
            "available": True,
            "until": 0,
            "remaining": None,
            "reason": kwargs["reason"],
            "started_at": 123,
        },
    )
    token = _csrf_token(client)

    response = client.post(
        "/scraper/pause",
        json={"preset": "phone_blocked"},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 503
    assert response.get_json()["status"] == "error"


def test_scraper_resume_uses_persistent_core_control(web_client, monkeypatch):
    web, client = web_client
    calls = []
    monkeypatch.setattr(web.core, "resume_scraper", lambda: calls.append(True) or True)
    monkeypatch.setattr(
        web.core,
        "get_scraper_pause",
        lambda: {
            "active": False,
            "available": True,
            "until": 0,
            "remaining": None,
            "reason": "",
            "started_at": 0,
        },
    )
    token = _csrf_token(client)

    response = client.post(
        "/scraper/resume",
        json={},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json()["pause"]["active"] is False
    assert calls == [True]


def test_scraper_resume_survives_post_write_state_read_failure(web_client, monkeypatch):
    web, client = web_client
    monkeypatch.setattr(web.core, "resume_scraper", lambda: True)

    def fail_state_read():
        raise RuntimeError("state unavailable")

    monkeypatch.setattr(web.core, "get_scraper_pause", fail_state_read)
    token = _csrf_token(client)

    response = client.post(
        "/scraper/resume",
        json={},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "warning"
    assert payload["pause"]["active"] is True
    assert payload["pause"]["available"] is False


def test_pause_state_is_visible_in_health_and_both_control_surfaces(
    web_client, monkeypatch
):
    web, client = web_client
    pause = {
        "active": True,
        "available": True,
        "until": 0,
        "remaining": None,
        "reason": "phone_blocked",
        "started_at": 123,
    }
    health = web.core.get_scraper_health()
    health.update(
        {
            "pause_active": pause["active"],
            "pause_available": pause["available"],
            "pause_until": pause["until"],
            "pause_remaining": pause["remaining"],
            "pause_reason": pause["reason"],
            "pause_started_at": pause["started_at"],
        }
    )
    monkeypatch.setattr(web.core, "get_scraper_health", lambda: health)
    monkeypatch.setattr(
        web.core,
        "get_scraper_pause",
        lambda *args, **kwargs: pytest.fail("health must reuse its pause snapshot"),
    )

    health = client.get("/config/health").get_json()
    assert health["scraper"]["manual_pause"] == pause
    assert health["scraper"]["status"] == "paused"

    config_html = client.get("/config").data.decode()
    dashboard_html = client.get("/").data.decode()
    for html in (config_html, dashboard_html):
        assert "data-" in html
        assert "phone_blocked" in html
        assert "/scraper/pause" in html
        assert "/scraper/resume" in html


def test_scraper_mutations_require_basic_auth_and_csrf(web_client, monkeypatch):
    web, client = web_client
    monkeypatch.setattr(web, "WEB_USERNAME", "admin")
    monkeypatch.setattr(web, "WEB_PASSWORD", "secret")
    monkeypatch.setattr(
        web.core,
        "pause_scraper",
        lambda **kwargs: {
            "active": True,
            "available": True,
            "until": 0,
            "remaining": None,
            "reason": kwargs["reason"],
            "started_at": 123,
        },
    )
    monkeypatch.setattr(web.core, "resume_scraper", lambda: True)
    monkeypatch.setattr(
        web.core,
        "get_scraper_pause",
        lambda: {
            "active": False,
            "available": True,
            "until": 0,
            "remaining": None,
            "reason": "",
            "started_at": 0,
        },
    )

    for path, payload in (
        ("/scraper/pause", {"preset": "phone_blocked"}),
        ("/scraper/resume", {}),
    ):
        denied = client.post(path, json=payload)
        assert denied.status_code == 401
        assert denied.headers["WWW-Authenticate"].startswith("Basic ")

    auth_headers = _basic_auth()
    token = _csrf_token(client, headers=auth_headers)
    for path, payload in (
        ("/scraper/pause", {"preset": "phone_blocked"}),
        ("/scraper/resume", {}),
    ):
        missing_csrf = client.post(path, json=payload, headers=auth_headers)
        assert missing_csrf.status_code == 400

        response = client.post(
            path,
            json=payload,
            headers={**auth_headers, "X-CSRF-Token": token},
        )
        assert response.status_code == 200
