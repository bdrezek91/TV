from __future__ import annotations

import datetime as dt
import random

from tradingview_mcp.core.market_data.bybit.reconnect import (
    BackoffPolicy,
    ReconnectState,
    data_age_seconds,
    is_stale,
)


def test_backoff_delay_within_bounds_and_grows():
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=60.0, multiplier=2.0)
    rng = random.Random(42)
    d1 = policy.delay_for_attempt(1, rng=rng)
    d2 = policy.delay_for_attempt(2, rng=rng)
    d10 = policy.delay_for_attempt(10, rng=rng)
    assert 0 <= d1 <= 1.0
    assert 0 <= d2 <= 2.0
    assert 0 <= d10 <= 60.0  # capped


def test_backoff_jitter_is_non_deterministic_across_calls():
    policy = BackoffPolicy(base_delay_seconds=5.0, max_delay_seconds=60.0)
    rng = random.Random(1)
    samples = {policy.delay_for_attempt(3, rng=rng) for _ in range(5)}
    assert len(samples) > 1  # jitter actually varies


def test_reconnect_state_tracks_attempts_and_resets():
    state = ReconnectState(BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=10.0))
    rng = random.Random(7)
    state.next_delay(rng=rng)
    state.next_delay(rng=rng)
    assert state.attempt == 2
    assert state.total_reconnects == 2
    state.reset()
    assert state.attempt == 0
    assert state.total_reconnects == 2  # cumulative counter persists across resets


def test_is_stale_missing_timestamp():
    assert is_stale(None, max_age_seconds=10) is True


def test_is_stale_boundary():
    now = dt.datetime(2024, 1, 1, 0, 1, 0, tzinfo=dt.timezone.utc)
    fresh = dt.datetime(2024, 1, 1, 0, 0, 55, tzinfo=dt.timezone.utc)  # 5s old
    stale = dt.datetime(2024, 1, 1, 0, 0, 40, tzinfo=dt.timezone.utc)  # 20s old
    assert is_stale(fresh, max_age_seconds=10, now=now) is False
    assert is_stale(stale, max_age_seconds=10, now=now) is True


def test_data_age_seconds():
    now = dt.datetime(2024, 1, 1, 0, 1, 0, tzinfo=dt.timezone.utc)
    last = dt.datetime(2024, 1, 1, 0, 0, 45, tzinfo=dt.timezone.utc)
    assert data_age_seconds(last, now=now) == 15.0
    assert data_age_seconds(None) is None
