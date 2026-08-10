"""Retention/downsampling for periodic order-book feature snapshots.

The live collector keeps dense snapshots for a configurable high-resolution
window. Older rows are thinned to one row per symbol/time bucket, while a
separate hard-retention horizon eventually removes very old rows.

This module intentionally does not touch raw websocket deltas, candles,
trades, liquidations, derivatives, paper state, or trading decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class OrderbookRetentionPolicy:
    enabled: bool = True
    high_res_days: int = 30
    archive_days: int = 180
    downsample_seconds: int = 60
    batch_size: int = 5000

    def validate(self) -> None:
        if self.high_res_days < 1:
            raise ValueError("high_res_days must be >= 1")
        if self.archive_days <= self.high_res_days:
            raise ValueError("archive_days must be greater than high_res_days")
        if self.downsample_seconds < 5:
            raise ValueError("downsample_seconds must be >= 5")
        if self.batch_size < 100:
            raise ValueError("batch_size must be >= 100")


_TABLE_STATS_SQL = text(
    """
    SELECT
        COUNT(*)::bigint AS rows,
        COALESCE(pg_total_relation_size('orderbook_feature_snapshots'), 0)::bigint AS total_bytes,
        MIN(source_timestamp) AS oldest,
        MAX(source_timestamp) AS newest
    FROM orderbook_feature_snapshots
    """
)

_DELETE_ARCHIVE_BATCH_SQL = text(
    """
    WITH candidates AS (
        SELECT id
        FROM orderbook_feature_snapshots
        WHERE source_timestamp < NOW() - make_interval(days => :archive_days)
        ORDER BY source_timestamp ASC, id ASC
        LIMIT :batch_size
    )
    DELETE FROM orderbook_feature_snapshots AS target
    USING candidates
    WHERE target.id = candidates.id
    RETURNING target.id
    """
)

_DOWNSAMPLE_BATCH_SQL = text(
    """
    WITH ranked AS (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY
                    symbol,
                    FLOOR(EXTRACT(EPOCH FROM source_timestamp) / :downsample_seconds)
                ORDER BY source_timestamp ASC, id ASC
            ) AS rn
        FROM orderbook_feature_snapshots
        WHERE source_timestamp < NOW() - make_interval(days => :high_res_days)
          AND source_timestamp >= NOW() - make_interval(days => :archive_days)
    ), candidates AS (
        SELECT id
        FROM ranked
        WHERE rn > 1
        LIMIT :batch_size
    )
    DELETE FROM orderbook_feature_snapshots AS target
    USING candidates
    WHERE target.id = candidates.id
    RETURNING target.id
    """
)


async def table_stats(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(_TABLE_STATS_SQL)).mappings().one()
    return dict(row)


async def _delete_batches(
    session: AsyncSession,
    statement,
    params: dict[str, Any],
    *,
    max_batches: int | None = None,
) -> int:
    """Delete in bounded transactions so live ingestion is not held hostage.

    Each batch is committed independently. A maintenance crash therefore loses
    at most the current batch and never rolls back already-completed cleanup.
    """
    deleted = 0
    batches = 0
    while True:
        result = await session.execute(statement, params)
        batch_deleted = len(result.fetchall())
        await session.commit()
        deleted += batch_deleted
        batches += 1
        if batch_deleted < int(params["batch_size"]):
            break
        if max_batches is not None and batches >= max_batches:
            break
    return deleted


async def run_orderbook_retention_once(
    session: AsyncSession,
    policy: OrderbookRetentionPolicy,
    *,
    max_batches_per_phase: int | None = None,
) -> dict[str, Any]:
    """Run one maintenance pass and return diagnostics.

    Dense rows newer than ``high_res_days`` are never touched. Between
    ``high_res_days`` and ``archive_days`` exactly one earliest row is retained
    for each symbol/downsample bucket. Rows older than ``archive_days`` are
    removed entirely.
    """
    policy.validate()
    before = await table_stats(session)
    if not policy.enabled:
        return {
            "enabled": False,
            "before": before,
            "after": before,
            "downsampled_deleted": 0,
            "expired_deleted": 0,
        }

    expired_deleted = await _delete_batches(
        session,
        _DELETE_ARCHIVE_BATCH_SQL,
        {
            "archive_days": policy.archive_days,
            "batch_size": policy.batch_size,
        },
        max_batches=max_batches_per_phase,
    )
    downsampled_deleted = await _delete_batches(
        session,
        _DOWNSAMPLE_BATCH_SQL,
        {
            "high_res_days": policy.high_res_days,
            "archive_days": policy.archive_days,
            "downsample_seconds": policy.downsample_seconds,
            "batch_size": policy.batch_size,
        },
        max_batches=max_batches_per_phase,
    )
    after = await table_stats(session)
    return {
        "enabled": True,
        "policy": {
            "high_res_days": policy.high_res_days,
            "archive_days": policy.archive_days,
            "downsample_seconds": policy.downsample_seconds,
            "batch_size": policy.batch_size,
        },
        "before": before,
        "after": after,
        "downsampled_deleted": downsampled_deleted,
        "expired_deleted": expired_deleted,
    }
