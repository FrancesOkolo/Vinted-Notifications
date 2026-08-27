"""Pure shared-budget calculations for the Vinted catalogue scheduler."""

import math

DEFAULT_REQUEST_SPACING_SECONDS = 60.0
MIN_REQUEST_SPACING_SECONDS = 60.0
MAX_REQUEST_SPACING_SECONDS = 120.0
SCHEDULED_CAPACITY_UTILIZATION = 0.80
FAST_CAPACITY_SHARE = 0.75
REQUEST_JITTER_MAX_SECONDS = 3.0
MAX_CONSECUTIVE_FAST_DISPATCHES = 3


def bounded_request_spacing(value=None):
    """Return a safe catalogue-request gap; configuration can only slow it."""
    try:
        spacing = float(value)
    except (TypeError, ValueError):
        spacing = DEFAULT_REQUEST_SPACING_SECONDS
    if not math.isfinite(spacing):
        spacing = DEFAULT_REQUEST_SPACING_SECONDS
    return min(
        max(spacing, MIN_REQUEST_SPACING_SECONDS),
        MAX_REQUEST_SPACING_SECONDS,
    )


def build_cadence_plan(
    normal_count,
    fast_count,
    requested_normal_seconds,
    requested_fast_seconds,
    request_spacing_seconds=DEFAULT_REQUEST_SPACING_SECONDS,
):
    """Allocate safe aggregate capacity while preserving Fast-query priority.

    Scheduled work uses at most 80% of the hard requester capacity, leaving
    headroom for the one bounded session-refresh retry. When both modes exist,
    Fast may use up to 75% of that scheduled capacity; Normal therefore cannot
    be starved. Unused Fast capacity is immediately available to Normal work.
    """
    normal_count = max(0, int(normal_count))
    fast_count = max(0, int(fast_count))
    requested_normal = max(1, int(requested_normal_seconds))
    requested_fast = max(1, min(requested_normal, int(requested_fast_seconds)))
    spacing = bounded_request_spacing(request_spacing_seconds)
    capacity = SCHEDULED_CAPACITY_UTILIZATION / spacing

    effective_fast = requested_fast
    effective_normal = requested_normal

    if fast_count and normal_count:
        fast_demand = fast_count / requested_fast
        fast_capacity = min(fast_demand, capacity * FAST_CAPACITY_SHARE)
        effective_fast = max(
            requested_fast,
            math.ceil(fast_count / max(fast_capacity, 1e-12)),
        )
        remaining_capacity = max(capacity - (fast_count / effective_fast), 1e-12)
        effective_normal = max(
            requested_normal,
            math.ceil(normal_count / remaining_capacity),
        )
    elif fast_count:
        effective_fast = max(
            requested_fast,
            math.ceil(fast_count / capacity),
        )
    elif normal_count:
        effective_normal = max(
            requested_normal,
            math.ceil(normal_count / capacity),
        )

    scheduled_rate = 0.0
    if fast_count:
        scheduled_rate += fast_count / effective_fast
    if normal_count:
        scheduled_rate += normal_count / effective_normal

    return {
        "normal_count": normal_count,
        "fast_count": fast_count,
        "requested_normal_seconds": requested_normal,
        "requested_fast_seconds": requested_fast,
        "effective_normal_seconds": int(effective_normal),
        "effective_fast_seconds": int(effective_fast),
        "request_spacing_seconds": spacing,
        "scheduled_requests_per_minute": scheduled_rate * 60,
        "scheduled_capacity_per_minute": capacity * 60,
    }
