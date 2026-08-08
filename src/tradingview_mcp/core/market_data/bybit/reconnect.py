"""Reconnect backoff policy (exponential + jitter) and staleness detection.

Pure, deterministic (given an injected RNG), and independent of any
websocket library — the collector calls into this for "how long do I wait
before reconnecting" and "is my data too old to trust" decisions.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass


@dataclass
class BackoffPolicy:
    """Exponential backoff with full jitter, capped at ``max_delay_seconds``.

    ``base_delay_seconds`` is the delay after the first failure;
    each subsequent failure doubles the exponential ceiling.
    """

    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0

    def delay_for_attempt(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """``attempt`` is 1-indexed (1 = first reconnect attempt).

        Uses "full jitter": a uniform random delay in
        ``[0, min(max_delay, base * multiplier**(attempt-1))]``. This is
        the AWS-recommended jitter strategy — it avoids thundering herds
        while still bounding worst-case wait time.
        """
        if attempt < 1:
            attempt = 1
        ceiling = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (self.multiplier ** (attempt - 1)),
        )
        rng = rng or random
        return rng.uniform(0, ceiling)


class ReconnectState:
    """Tracks consecutive-failure count for :class:`BackoffPolicy`, and
    resets it after a sustained successful connection."""

    def __init__(self, policy: BackoffPolicy | None = None) -> None:
        self.policy = policy or BackoffPolicy()
        self.attempt = 0
        self.total_reconnects = 0

    def next_delay(self, *, rng: random.Random | None = None) -> float:
        self.attempt += 1
        self.total_reconnects += 1
        return self.policy.delay_for_attempt(self.attempt, rng=rng)

    def reset(self) -> None:
        self.attempt = 0


def is_stale(
    last_seen: dt.datetime | None,
    max_age_seconds: float,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """True if *last_seen* is missing or older than *max_age_seconds*."""
    if last_seen is None:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    age = (now - last_seen).total_seconds()
    return age > max_age_seconds


def data_age_seconds(
    last_seen: dt.datetime | None, *, now: dt.datetime | None = None
) -> float | None:
    if last_seen is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - last_seen).total_seconds()
