import pytest


@pytest.fixture
def web_client(monkeypatch):
    import web_ui_plugin.web_ui as web

    monkeypatch.setattr(web, "WEB_USERNAME", "")
    monkeypatch.setattr(web, "WEB_PASSWORD", "")
    monkeypatch.setattr(
        web.core,
        "check_version",
        lambda: (True, "test", "test", "https://example.invalid"),
    )
    return web, web.app.test_client()


def _report(days):
    return {
        "days": days,
        "summary": {
            "execution_count": 12,
            "success_count": 10,
            "failed_count": 2,
            "returned_count": 100,
            "fresh_count": 8,
            "already_known_count": 92,
            "accepted_count": 6,
            "locally_rejected_count": 2,
            "notifications_generated": 5,
            "overlap_return_count": 20,
            "data_start": None,
            "data_end": None,
            "evidence_note": "Full result windows are right-censored.",
        },
        "queries": [
            {
                "query_id": 1,
                "query_name": "Pooky lamps",
                "execution_count": 12,
                "success_count": 10,
                "failed_count": 2,
                "success_rate": 10 / 12,
                "avg_duration_ms": 125.0,
                "returned_count": 100,
                "fresh_count": 8,
                "already_known_count": 92,
                "accepted_count": 6,
                "locally_rejected_count": 2,
                "notifications_generated": 5,
                "overlap_return_count": 20,
                "overlap_rate": 0.2,
                "evidence_status": "limited",
                "recommendation": "Collect evidence; no automatic change.",
            }
        ],
        "overlaps": [
            {
                "query_a_id": 1,
                "query_b_id": 2,
                "query_a_name": "Pooky lamps",
                "query_b_name": "Raffield lamps",
                "shared_item_count": 4,
                "query_a_item_count": 20,
                "query_b_item_count": 10,
                "overlap_rate": 0.1538,
                "recommendation": "Manual exactness review only.",
            }
        ],
    }


def _schedule():
    return {
        "cadence": {
            "normal_count": 34,
            "fast_count": 2,
            "effective_normal_seconds": 1093,
            "effective_fast_seconds": 90,
        },
        "volume": {
            "requests_per_minute": 3.2,
            "requests_per_day": 4608.0,
        },
        "normal_latency": {"mean_seconds": 546.5},
        "fast_latency": {"mean_seconds": 45.0},
    }


def test_efficiency_page_is_read_only_and_renders_observed_and_scheduled_data(
    web_client, monkeypatch
):
    web, client = web_client
    calls = []
    monkeypatch.setattr(
        web.query_observability,
        "get_efficiency_report",
        lambda days: calls.append(days) or _report(days),
    )
    monkeypatch.setattr(web, "_query_efficiency_schedule", _schedule)

    response = client.get("/query-efficiency?days=7")

    assert response.status_code == 200
    assert calls == [7]
    html = response.data.decode()
    assert "Query Efficiency" in html
    assert "Pooky lamps" in html
    assert "Raffield lamps" in html
    assert "4608/day" in html
    assert "right-censored" in html
    assert "does not prove two searches can be merged" in html
    assert "No schedule or query setting is changed from this page" in html
    assert 'href="/query-efficiency"' in html
    assert "seller-identifier" not in html
    assert "https://www.vinted.co.uk/catalog" not in html


@pytest.mark.parametrize("days", ["0", "8", "365", "not-a-number"])
def test_efficiency_page_rejects_unapproved_windows(web_client, monkeypatch, days):
    web, client = web_client
    monkeypatch.setattr(
        web.query_observability,
        "get_efficiency_report",
        lambda **_kwargs: pytest.fail("report must not run"),
    )

    response = client.get(f"/query-efficiency?days={days}")

    assert response.status_code == 400


def test_efficiency_schedule_uses_shared_cadence_and_pure_volume_helpers(
    web_client, monkeypatch
):
    web, _client = web_client
    monkeypatch.setattr(
        web.db,
        "get_all_parameters",
        lambda: {
            "query_refresh_delay": "180",
            "fast_query_refresh_delay": "90",
            "catalogue_request_spacing_seconds": "15",
        },
    )
    monkeypatch.setattr(
        web.db,
        "get_query_enabled_map",
        lambda: {1: True, 2: True, 3: False},
    )
    monkeypatch.setattr(
        web.db,
        "get_query_preferences_map",
        lambda query_ids: {
            1: {"poll_mode": "fast"},
            2: {"poll_mode": "normal"},
        },
    )

    schedule = web._query_efficiency_schedule()

    assert schedule["cadence"]["fast_count"] == 1
    assert schedule["cadence"]["normal_count"] == 1
    assert schedule["volume"]["requests_per_day"] > 0
    assert schedule["fast_latency"]["mean_seconds"] == (
        schedule["cadence"]["effective_fast_seconds"] / 2
    )
