import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import db
from url_normalizer import normalise_vinted_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "vinted_notifications.db"
    monkeypatch.setattr(db, "DB_PATH", str(database_path))
    assert db.create_or_update_sqlite_db(str(ROOT / "initial_db.sql"))
    assert db.configure_database_runtime()
    yield database_path


@pytest.fixture
def web_client(database, monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "")
    monkeypatch.setattr(web, "WEB_PASSWORD", "")
    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://example.invalid"),
    )
    return web, web.app.test_client()


def _add_query(name=None):
    url = normalise_vinted_url("https://www.vinted.co.uk/catalog?search_text=pooky")
    query_id, created, _subscribed = db.add_query_to_db(url, name=name)
    assert created
    return query_id, url


def _csrf_token(client):
    page = client.get("/queries")
    match = re.search(rb'name="_csrf_token" value="([^"]+)"', page.data)
    assert match is not None
    return match.group(1).decode()


def _edit_form(token, revision, url, **overrides):
    form = {
        "_csrf_token": token,
        "edit_revision": revision,
        "query": url,
        "query_name": "",
        "poll_mode": "normal",
        "deal_mode": "off",
        "deal_excellent_max": "",
        "deal_good_max": "",
        "deal_currency": "GBP",
    }
    form.update(overrides)
    return form


def test_edit_state_is_fresh_no_store_and_keeps_null_name_blank(web_client):
    _web, client = web_client
    query_id, _url = _add_query(name=None)
    assert db.set_query_preferences(
        query_id,
        poll_mode="fast",
        monitor_during_quiet_hours=True,
    )

    first = client.get(f"/api/queries/{query_id}/edit-state")
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    state = first.get_json()
    assert state["query_name"] == ""
    assert state["poll_mode"] == "fast"
    assert state["monitor_during_quiet_hours"] is True
    assert len(state["revision"]) == 64
    assert (
        client.get(f"/api/queries/{query_id}/edit-state").get_json()["revision"]
        == state["revision"]
    )

    assert db.set_query_preferences(query_id, poll_mode="normal")
    latest = client.get(f"/api/queries/{query_id}/edit-state").get_json()
    assert latest["poll_mode"] == "normal"
    assert latest["revision"] != state["revision"]


def test_stale_revision_cannot_overwrite_newer_query_settings(web_client):
    _web, client = web_client
    query_id, url = _add_query(name="Original")
    stale_revision = client.get(f"/api/queries/{query_id}/edit-state").get_json()[
        "revision"
    ]

    # Simulate another browser saving after this page/modal was loaded.
    current_preferences = {
        "poll_mode": "normal",
        "monitor_during_quiet_hours": False,
        "deal_evaluator_enabled": True,
        "deal_excellent_max": "25",
        "deal_good_max": "50",
        "deal_currency": "GBP",
    }
    assert (
        db.update_query_configuration_atomic(
            query_id,
            url,
            "Newer name",
            current_preferences,
            False,
            expected_revision=stale_revision,
        )
        == "updated"
    )

    response = client.post(
        f"/update_query/{query_id}",
        data=_edit_form(
            _csrf_token(client),
            stale_revision,
            url,
            query_name="Stale name",
            poll_mode="fast",
            monitor_during_quiet_hours="on",
            deal_mode="ai",
        ),
    )

    assert response.status_code == 302
    current = db.get_query_edit_state(query_id)
    assert current["query_name"] == "Newer name"
    assert current["poll_mode"] == "normal"
    assert current["monitor_during_quiet_hours"] is False
    assert current["deal_evaluator_enabled"] is True
    assert current["deal_ai_enabled"] is False
    with client.session_transaction() as session:
        messages = session.get("_flashes", [])
    assert any("Nothing was saved" in message for _category, message in messages)


def test_missing_revision_is_rejected_before_atomic_update(web_client, monkeypatch):
    _web, client = web_client
    query_id, url = _add_query(name="Current name")
    called = []
    monkeypatch.setattr(
        db,
        "update_query_configuration_atomic",
        lambda *args, **kwargs: called.append((args, kwargs)) or "updated",
    )

    response = client.post(
        f"/update_query/{query_id}",
        data=_edit_form(_csrf_token(client), "", url, query_name="Replacement"),
    )

    assert response.status_code == 302
    assert called == []


def test_current_revision_updates_atomically_and_preserves_empty_name(web_client):
    _web, client = web_client
    query_id, url = _add_query(name=None)
    revision = client.get(f"/api/queries/{query_id}/edit-state").get_json()["revision"]

    response = client.post(
        f"/update_query/{query_id}",
        data=_edit_form(
            _csrf_token(client),
            revision,
            url,
            poll_mode="fast",
            monitor_during_quiet_hours="on",
            deal_mode="ai",
        ),
    )

    assert response.status_code == 302
    current = db.get_query_edit_state(query_id)
    assert current["query_name"] is None
    assert current["poll_mode"] == "fast"
    assert current["monitor_during_quiet_hours"] is True
    assert current["deal_evaluator_enabled"] is False
    assert current["deal_ai_enabled"] is True


def test_queries_template_fetches_fresh_state_before_enabling_save(web_client):
    _web, client = web_client
    _add_query(name="Pooky")
    html = client.get("/queries").data.decode()

    assert 'id="editQueryRevision"' in html
    assert 'id="editQuerySave" disabled' in html
    assert "'/api/queries/' + encodeURIComponent(queryId) + '/edit-state'" in html
    assert "cache: 'no-store'" in html
    assert "editSave.disabled = false" in html
    assert "button.dataset.queryPollMode" not in html


def test_concurrent_edits_cannot_jointly_exceed_fast_query_cap(database):
    queries = []
    for index in range(6):
        url = normalise_vinted_url(
            f"https://www.vinted.co.uk/catalog?search_text=fast-race-{index}"
        )
        query_id, created, _subscribed = db.add_query_to_db(url, name=f"Query {index}")
        assert created
        queries.append((query_id, url))

    for query_id, _url in queries[:4]:
        assert db.set_query_preferences(query_id, poll_mode="fast")

    candidates = []
    for query_id, url in queries[4:]:
        state = db.get_query_edit_state(query_id)
        candidates.append((query_id, url, db.query_edit_revision(state)))

    barrier = threading.Barrier(2)

    def promote_to_fast(candidate):
        query_id, url, revision = candidate
        barrier.wait(timeout=5)
        return db.update_query_configuration_atomic(
            query_id,
            url,
            f"Query {query_id}",
            {
                "poll_mode": "fast",
                "monitor_during_quiet_hours": False,
                "deal_evaluator_enabled": False,
                "deal_excellent_max": None,
                "deal_good_max": None,
                "deal_currency": "GBP",
            },
            False,
            expected_revision=revision,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(promote_to_fast, candidates))

    assert sorted(statuses) == ["fast_limit", "updated"]
    enabled = db.get_query_enabled_map()
    preferences = db.get_query_preferences_map()
    active_fast = [
        query_id
        for query_id, is_enabled in enabled.items()
        if is_enabled and preferences[query_id]["poll_mode"] == "fast"
    ]
    assert len(active_fast) == db.MAX_ACTIVE_FAST_QUERIES


def test_add_and_resume_writes_cannot_bypass_fast_query_cap(database):
    query_ids = []
    for index in range(7):
        url = normalise_vinted_url(
            f"https://www.vinted.co.uk/catalog?search_text=fast-guard-{index}"
        )
        query_id, created, _subscribed = db.add_query_to_db(url)
        assert created
        query_ids.append(query_id)

    for query_id in query_ids[: db.MAX_ACTIVE_FAST_QUERIES]:
        assert db.set_query_preferences(query_id, poll_mode="fast")

    # Add-query preference saving uses this same guarded DB method. A stale
    # preflight can therefore leave the new query safely Normal, never sixth.
    assert not db.set_query_preferences(query_ids[5], poll_mode="fast")
    assert db.get_query_preferences(query_ids[5])["poll_mode"] == "normal"

    # Paused Fast queries are valid; only making them active consumes the cap.
    for query_id in query_ids[5:]:
        assert db.set_query_enabled(query_id, False)
        assert db.set_query_preferences(query_id, poll_mode="fast")

    status, changed = db.set_queries_enabled_with_fast_limit(query_ids[5:], True)
    assert (status, changed) == ("fast_limit", 0)
    assert not db.is_query_enabled(query_ids[5])
    assert not db.is_query_enabled(query_ids[6])

    assert db.set_query_enabled(query_ids[0], False)
    assert db.set_query_enabled_with_fast_limit(query_ids[5], True) == "updated"
    assert db.set_query_enabled_with_fast_limit(query_ids[6], True) == "fast_limit"
