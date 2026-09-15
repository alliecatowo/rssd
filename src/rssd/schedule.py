"""Polling interval, backoff and jitter math. SPEC §10.2 - §10.3. PURE.

Deliberately split from randomness: ``next_interval`` and ``backoff_delay``
return deterministic values that callers then jitter with ``apply_jitter``
(or the full-jitter formula baked into ``backoff_delay`` itself, per spec).
Keeping the un-jittered computation separate is what makes this testable.
"""

from __future__ import annotations

import random

from rssd.config import Limits


def _clamp(value: int, limits: Limits) -> int:
    return max(limits.min_interval, min(limits.max_interval, value))


def next_interval(
    *,
    sub_interval: int | None,
    max_age: int | None,
    ttl_seconds: int | None,
    has_validators: bool,
    limits: Limits,
) -> int:
    """Resolve the polling interval, SPEC §10.2 precedence, clamped.

    Precedence: subscription <interval> -> Cache-Control max-age -> RSS ttl
    -> limits.default_interval. When the feed sends no validators the floor
    rises to limits.no_validator_floor (SPEC §10.4).
    """
    if sub_interval is not None:
        raw = sub_interval
    elif max_age is not None:
        raw = max_age
    elif ttl_seconds is not None:
        raw = ttl_seconds
    else:
        raw = limits.default_interval

    floor = limits.min_interval
    if not has_validators:
        floor = max(floor, limits.no_validator_floor)

    return max(floor, min(limits.max_interval, raw))


def backoff_delay(failures: int, base: int, limits: Limits) -> float:
    """Exponential backoff with full jitter, capped at limits.backoff_cap.

    delay = min(base * 2**min(failures, 8), cap) * (0.75 + random()*0.5)
    """
    capped_failures = min(failures, 8)
    raw = min(base * (2**capped_failures), limits.backoff_cap)
    return raw * (0.75 + random.random() * 0.5)


def apply_jitter(value: float, spread: float = 0.1) -> float:
    """Multiply ``value`` by ``(1 - spread) + random() * 2*spread``.

    With the default spread=0.1 this matches SPEC §10.2's
    ``(0.9 + random() * 0.2)`` multiplier.
    """
    return value * ((1 - spread) + random.random() * (2 * spread))


def stagger_offsets(count: int, limits: Limits) -> list[float]:
    """Startup stagger offsets, spaced ``stagger_step`` apart, capped total.

    SPEC §10: "Startup staggers feeds by ~750ms each, capped at 30s total."
    """
    if count <= 0:
        return []
    offsets = []
    for i in range(count):
        offset = min(i * limits.stagger_step, limits.stagger_cap)
        offsets.append(offset)
    return offsets


def should_downgrade_health(failures: int, limits: Limits) -> bool:
    """True once consecutive failures reach the failing threshold."""
    return failures >= limits.max_failures_before_failing
