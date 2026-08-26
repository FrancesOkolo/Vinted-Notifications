import math

import pytest

import query_efficiency


def test_current_clean_run_request_volume_is_reproducible():
    scenario = query_efficiency.request_volume_scenario(
        normal_count=34,
        fast_count=2,
        effective_normal_seconds=1093,
        effective_fast_seconds=90,
    )

    assert scenario["normal_requests_per_minute"] == pytest.approx(34 / 1093 * 60)
    assert scenario["fast_requests_per_minute"] == pytest.approx(2 / 90 * 60)
    assert scenario["requests_per_minute"] == pytest.approx(3.1997560232)
    assert scenario["requests_per_hour"] == pytest.approx(191.9853614)
    assert scenario["requests_per_day"] == pytest.approx(4607.6486734)
    assert scenario["requests_in_horizon"] == pytest.approx(
        scenario["requests_per_day"]
    )


def test_request_volume_supports_empty_modes_and_custom_horizon():
    scenario = query_efficiency.request_volume_scenario(
        normal_count=0,
        fast_count=1,
        effective_normal_seconds=180,
        effective_fast_seconds=120,
        horizon_hours=6,
    )

    assert scenario["normal_requests_per_minute"] == 0
    assert scenario["requests_per_minute"] == 0.5
    assert scenario["requests_in_horizon"] == 180


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("normal_count", -1),
        ("fast_count", 1.5),
        ("effective_normal_seconds", 0),
        ("effective_fast_seconds", math.inf),
        ("horizon_hours", -1),
    ],
)
def test_request_volume_rejects_invalid_inputs(field, value):
    arguments = {
        "normal_count": 1,
        "fast_count": 1,
        "effective_normal_seconds": 180,
        "effective_fast_seconds": 90,
        "horizon_hours": 24,
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        query_efficiency.request_volume_scenario(**arguments)


def test_periodic_poll_latency_models_clean_uniform_arrivals():
    fast = query_efficiency.periodic_poll_latency(90)
    normal = query_efficiency.periodic_poll_latency(1093)

    assert fast == {
        "poll_interval_seconds": 90.0,
        "minimum_seconds": 0.0,
        "mean_seconds": 45.0,
        "median_seconds": 45.0,
        "p95_seconds": 85.5,
        "maximum_seconds": 90.0,
    }
    assert normal["mean_seconds"] == 546.5
    assert normal["p95_seconds"] == pytest.approx(1038.35)


def test_result_window_scenario_exposes_capacity_not_a_probability():
    exactly_full = query_efficiency.result_window_scenario(
        items_per_request=20,
        matching_arrivals_per_minute=10,
        poll_interval_seconds=120,
    )
    overflowing = query_efficiency.result_window_scenario(
        items_per_request=20,
        matching_arrivals_per_minute=12,
        poll_interval_seconds=120,
    )

    assert exactly_full["expected_arrivals_between_polls"] == 20
    assert exactly_full["expected_capacity_margin"] == 0
    assert exactly_full["expected_arrivals_fit_window"] is True
    assert exactly_full["break_even_arrivals_per_minute"] == 10
    assert overflowing["expected_overflow"] == 4
    assert overflowing["expected_arrivals_fit_window"] is False


def test_zero_arrival_window_never_fills():
    scenario = query_efficiency.result_window_scenario(
        items_per_request=20,
        matching_arrivals_per_minute=0,
        poll_interval_seconds=90,
    )

    assert scenario["expected_arrivals_between_polls"] == 0
    assert scenario["expected_capacity_margin"] == 20
    assert math.isinf(scenario["window_fill_time_seconds"])


def test_full_response_is_right_censored_but_short_response_is_exact():
    full = query_efficiency.execution_censoring(returned_count=20, requested_window=20)
    short = query_efficiency.execution_censoring(returned_count=13, requested_window=20)

    assert full["right_censored"] is True
    assert full["minimum_available_count"] == 20
    assert full["exact_available_count"] is None
    assert short["right_censored"] is False
    assert short["exact_available_count"] == 13


def test_censoring_summary_does_not_invent_an_unobserved_tail():
    summary = query_efficiency.summarize_censoring(
        [
            {"returned_count": 20, "requested_window": 20},
            {"returned_count": 7, "requested_window": 20},
        ]
    )

    assert summary == {
        "execution_count": 2,
        "right_censored_execution_count": 1,
        "right_censored_execution_fraction": 0.5,
        "observed_item_count": 27,
        "minimum_available_item_count": 27,
        "unobserved_tail_count": None,
    }


def test_empty_censoring_summary_is_well_defined():
    summary = query_efficiency.summarize_censoring([])

    assert summary["execution_count"] == 0
    assert summary["right_censored_execution_fraction"] == 0
    assert summary["unobserved_tail_count"] == 0


def test_returned_id_overlap_deduplicates_each_result_set():
    overlap = query_efficiency.returned_id_overlap(
        [1, 1, 2, 3],
        [2, 3, 4],
    )

    assert overlap == {
        "left_count": 3,
        "right_count": 3,
        "intersection_count": 2,
        "union_count": 4,
        "jaccard_similarity": 0.5,
        "left_containment": 2 / 3,
        "right_containment": 2 / 3,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"returned_count": 21, "requested_window": 20},
        {"returned_count": -1, "requested_window": 20},
        {"returned_count": 0, "requested_window": 0},
    ],
)
def test_censoring_rejects_impossible_counts(arguments):
    with pytest.raises(ValueError):
        query_efficiency.execution_censoring(**arguments)
