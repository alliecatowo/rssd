"""Tests for rssd.schedule -- pure interval/backoff/jitter/stagger math."""

from __future__ import annotations

import pytest

from rssd.config import Limits
from rssd.schedule import (
    apply_jitter,
    backoff_delay,
    next_interval,
    should_downgrade_health,
    stagger_offsets,
)

LIMITS = Limits()


# ── next_interval precedence ─────────────────────────────────────────────


def test_precedence_subscription_wins():
    value = next_interval(
        sub_interval=300, max_age=600, ttl_seconds=900, has_validators=True, limits=LIMITS
    )
    assert value == 300


def test_precedence_max_age_when_no_sub_interval():
    value = next_interval(
        sub_interval=None, max_age=600, ttl_seconds=900, has_validators=True, limits=LIMITS
    )
    assert value == 600


def test_precedence_ttl_when_no_sub_interval_or_max_age():
    value = next_interval(
        sub_interval=None, max_age=None, ttl_seconds=900, has_validators=True, limits=LIMITS
    )
    assert value == 900


def test_precedence_default_when_nothing_else():
    value = next_interval(
        sub_interval=None, max_age=None, ttl_seconds=None, has_validators=True, limits=LIMITS
    )
    assert value == LIMITS.default_interval


# ── clamping ──────────────────────────────────────────────────────────────


def test_clamp_low():
    value = next_interval(
        sub_interval=5, max_age=None, ttl_seconds=None, has_validators=True, limits=LIMITS
    )
    assert value == LIMITS.min_interval


def test_clamp_high():
    value = next_interval(
        sub_interval=10 * 3600,
        max_age=None,
        ttl_seconds=None,
        has_validators=True,
        limits=LIMITS,
    )
    assert value == LIMITS.max_interval


def test_clamp_applies_to_max_age_and_ttl_too():
    assert (
        next_interval(
            sub_interval=None, max_age=5, ttl_seconds=None, has_validators=True, limits=LIMITS
        )
        == LIMITS.min_interval
    )
    assert (
        next_interval(
            sub_interval=None,
            max_age=None,
            ttl_seconds=99999,
            has_validators=True,
            limits=LIMITS,
        )
        == LIMITS.max_interval
    )


# ── no-validator floor ───────────────────────────────────────────────────


def test_no_validator_floor_raises_low_values():
    value = next_interval(
        sub_interval=None, max_age=None, ttl_seconds=None, has_validators=False, limits=LIMITS
    )
    # default_interval (900) is below no_validator_floor (1800)? default is
    # 900 < 1800, so the floor should win.
    assert value == max(LIMITS.no_validator_floor, LIMITS.default_interval)


def test_no_validator_floor_does_not_lower_a_larger_value():
    value = next_interval(
        sub_interval=3600, max_age=None, ttl_seconds=None, has_validators=False, limits=LIMITS
    )
    assert value == 3600


def test_no_validator_floor_overrides_small_sub_interval():
    value = next_interval(
        sub_interval=90, max_age=None, ttl_seconds=None, has_validators=False, limits=LIMITS
    )
    assert value == LIMITS.no_validator_floor


def test_has_validators_true_does_not_apply_floor():
    value = next_interval(
        sub_interval=90, max_age=None, ttl_seconds=None, has_validators=True, limits=LIMITS
    )
    assert value == 90


# ── backoff ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("failures", range(0, 13))
def test_backoff_bounds(failures):
    base = 30
    for _ in range(20):
        delay = backoff_delay(failures, base, LIMITS)
        capped_failures = min(failures, 8)
        raw = min(base * (2**capped_failures), LIMITS.backoff_cap)
        assert raw * 0.75 - 1e-9 <= delay <= raw * 1.25 + 1e-9
        assert delay <= LIMITS.backoff_cap * 1.25 + 1e-9


def test_backoff_caps_out_for_huge_failure_counts():
    delay = backoff_delay(1000, 30, LIMITS)
    assert delay <= LIMITS.backoff_cap * 1.25


def test_backoff_grows_with_failures_on_average():
    base = 10
    low = [backoff_delay(1, base, LIMITS) for _ in range(200)]
    high = [backoff_delay(6, base, LIMITS) for _ in range(200)]
    assert sum(high) / len(high) > sum(low) / len(low)


# ── jitter ───────────────────────────────────────────────────────────────


def test_apply_jitter_within_spread():
    for _ in range(200):
        result = apply_jitter(1000.0, spread=0.1)
        assert 900.0 <= result <= 1100.0


def test_apply_jitter_default_spread_matches_spec_range():
    for _ in range(200):
        result = apply_jitter(100.0)
        assert 90.0 <= result <= 110.0


# ── stagger ─────────────────────────────────────────────────────────────


def test_stagger_offsets_spacing():
    offsets = stagger_offsets(5, LIMITS)
    assert len(offsets) == 5
    assert offsets[0] == 0
    for i in range(1, len(offsets)):
        assert offsets[i] - offsets[i - 1] <= LIMITS.stagger_step + 1e-9


def test_stagger_offsets_respects_cap():
    offsets = stagger_offsets(1000, LIMITS)
    assert len(offsets) == 1000
    assert max(offsets) <= LIMITS.stagger_cap


def test_stagger_offsets_empty():
    assert stagger_offsets(0, LIMITS) == []


# ── health downgrade ───────────────────────────────────────────────────


def test_should_downgrade_health():
    assert not should_downgrade_health(LIMITS.max_failures_before_failing - 1, LIMITS)
    assert should_downgrade_health(LIMITS.max_failures_before_failing, LIMITS)
    assert should_downgrade_health(LIMITS.max_failures_before_failing + 5, LIMITS)
