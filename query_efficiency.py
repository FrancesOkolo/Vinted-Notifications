"""Pure, read-only calculations for catalogue-query efficiency analysis.

The functions in this module deliberately have no database, scheduler, or
network dependencies.  They model observations; they do not change live
polling policy and they must not be used to claim that a request rate is safe.
"""

import math
from collections.abc import Iterable, Mapping


def _non_negative_int(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer.") from exc
    if parsed != value or parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return parsed


def _positive_float(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    return parsed


def _non_negative_float(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative finite number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative finite number.") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a non-negative finite number.")
    return parsed


def request_volume_scenario(
    *,
    normal_count,
    fast_count,
    effective_normal_seconds,
    effective_fast_seconds,
    horizon_hours=24,
):
    """Return nominal scheduled catalogue-request volume for one cadence plan.

    The result excludes retries, session refreshes, quiet hours, cooldowns,
    failures, process downtime, and manual requests.  It is therefore a clean-
    run planning figure rather than observed traffic or a safety guarantee.
    """
    normal_count = _non_negative_int(normal_count, "normal_count")
    fast_count = _non_negative_int(fast_count, "fast_count")
    normal_interval = _positive_float(
        effective_normal_seconds, "effective_normal_seconds"
    )
    fast_interval = _positive_float(effective_fast_seconds, "effective_fast_seconds")
    horizon = _non_negative_float(horizon_hours, "horizon_hours")

    normal_per_second = normal_count / normal_interval
    fast_per_second = fast_count / fast_interval
    total_per_second = normal_per_second + fast_per_second

    return {
        "normal_count": normal_count,
        "fast_count": fast_count,
        "effective_normal_seconds": normal_interval,
        "effective_fast_seconds": fast_interval,
        "normal_requests_per_minute": normal_per_second * 60,
        "fast_requests_per_minute": fast_per_second * 60,
        "requests_per_minute": total_per_second * 60,
        "requests_per_hour": total_per_second * 3600,
        "requests_per_day": total_per_second * 86400,
        "horizon_hours": horizon,
        "requests_in_horizon": total_per_second * horizon * 3600,
    }


def periodic_poll_latency(poll_interval_seconds):
    """Model detection latency for uniformly timed arrivals between clean polls.

    Network time, queue time, retries, blocks, cooldowns, quiet hours, and an
    item falling outside the returned result window are intentionally excluded.
    """
    interval = _positive_float(poll_interval_seconds, "poll_interval_seconds")
    return {
        "poll_interval_seconds": interval,
        "minimum_seconds": 0.0,
        "mean_seconds": interval / 2,
        "median_seconds": interval / 2,
        "p95_seconds": interval * 0.95,
        "maximum_seconds": interval,
    }


def result_window_scenario(
    *, items_per_request, matching_arrivals_per_minute, poll_interval_seconds
):
    """Return a deterministic capacity scenario for one catalogue result window.

    ``expected_arrivals`` is arithmetic, not a probability forecast.  Actual
    arrivals are bursty.  Whenever more than ``items_per_request`` matching
    listings arrive between two successful observations, at least some new IDs
    can pass through the finite newest-first window unseen.
    """
    window = _non_negative_int(items_per_request, "items_per_request")
    if window < 1:
        raise ValueError("items_per_request must be at least 1.")
    arrivals_per_minute = _non_negative_float(
        matching_arrivals_per_minute, "matching_arrivals_per_minute"
    )
    interval = _positive_float(poll_interval_seconds, "poll_interval_seconds")

    expected_arrivals = arrivals_per_minute * interval / 60
    fill_time_seconds = (
        math.inf if arrivals_per_minute == 0 else window / arrivals_per_minute * 60
    )
    return {
        "items_per_request": window,
        "matching_arrivals_per_minute": arrivals_per_minute,
        "poll_interval_seconds": interval,
        "expected_arrivals_between_polls": expected_arrivals,
        "expected_capacity_margin": window - expected_arrivals,
        "expected_overflow": max(0.0, expected_arrivals - window),
        "window_fill_time_seconds": fill_time_seconds,
        "break_even_arrivals_per_minute": window * 60 / interval,
        "expected_arrivals_fit_window": expected_arrivals <= window,
    }


def execution_censoring(*, returned_count, requested_window):
    """Classify whether a single limited catalogue response is right-censored.

    A short response gives an exact count for that response.  A full response
    only proves that at least ``requested_window`` rows were available; it does
    not prove there were exactly that many.
    """
    returned = _non_negative_int(returned_count, "returned_count")
    window = _non_negative_int(requested_window, "requested_window")
    if window < 1:
        raise ValueError("requested_window must be at least 1.")
    if returned > window:
        raise ValueError("returned_count cannot exceed requested_window.")

    right_censored = returned == window
    return {
        "returned_count": returned,
        "requested_window": window,
        "right_censored": right_censored,
        "minimum_available_count": returned,
        "exact_available_count": None if right_censored else returned,
        "window_utilization": returned / window,
    }


def summarize_censoring(executions: Iterable[Mapping]):
    """Summarize limited-response censoring records without estimating the tail."""
    execution_count = 0
    censored_count = 0
    observed_items = 0

    for execution in executions:
        classification = execution_censoring(
            returned_count=execution["returned_count"],
            requested_window=execution["requested_window"],
        )
        execution_count += 1
        censored_count += int(classification["right_censored"])
        observed_items += classification["returned_count"]

    return {
        "execution_count": execution_count,
        "right_censored_execution_count": censored_count,
        "right_censored_execution_fraction": (
            censored_count / execution_count if execution_count else 0.0
        ),
        "observed_item_count": observed_items,
        "minimum_available_item_count": observed_items,
        "unobserved_tail_count": None if censored_count else 0,
    }


def returned_id_overlap(left_ids, right_ids):
    """Return set-overlap measures for two query result-ID collections.

    High overlap can identify queries worth reviewing, but it cannot establish
    that the query filters can be merged without widening or losing coverage.
    """
    left = set(left_ids)
    right = set(right_ids)
    intersection = left & right
    union = left | right
    return {
        "left_count": len(left),
        "right_count": len(right),
        "intersection_count": len(intersection),
        "union_count": len(union),
        "jaccard_similarity": len(intersection) / len(union) if union else 1.0,
        "left_containment": len(intersection) / len(left) if left else 1.0,
        "right_containment": len(intersection) / len(right) if right else 1.0,
    }
