"""Optional raw-data archival: append-only, per-symbol, per-day JSONL files.

Off by default (``RAW_DATA_ARCHIVE_ENABLED=false``). When enabled, writes
are done through a bounded asyncio queue drained by a background task so a
slow disk never blocks the collector's main receive loop. The format is
JSON Lines rather than true Parquet to avoid pulling in a heavy columnar
dependency (pyarrow) for an optional, off-by-default feature — each line is
one raw exchange message plus a receive timestamp, which downstream tooling
can batch-convert to Parquet later if desired.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Optional


class RawDataArchiver:
    def __init__(self, base_path: str, *, enabled: bool = False, queue_maxsize: int = 10_000) -> None:
        self.base_path = Path(base_path)
        self.enabled = enabled
        self._queue: "asyncio.Queue[tuple[str, str, dict]]" = asyncio.Queue(maxsize=queue_maxsize)
        self._task: Optional[asyncio.Task] = None
        self._dropped = 0

    def file_for(self, symbol: str, topic: str, *, now: Optional[dt.datetime] = None) -> Path:
        now = now or dt.datetime.now(dt.timezone.utc)
        day = now.strftime("%Y-%m-%d")
        return self.base_path / symbol / day / f"{topic}.jsonl"

    async def start(self) -> None:
        if not self.enabled:
            return
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def record(self, symbol: str, topic: str, message: dict) -> None:
        """Non-blocking enqueue; drops (and counts) the message if the
        archiver's queue is saturated rather than ever blocking the caller."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait((symbol, topic, message))
        except asyncio.QueueFull:
            self._dropped += 1

    async def _drain_loop(self) -> None:
        while True:
            symbol, topic, message = await self._queue.get()
            try:
                path = self.file_for(symbol, topic)
                path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(
                    {"received_at": dt.datetime.now(dt.timezone.utc).isoformat(), "message": message}
                )
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                # Archival must never crash the collector.
                pass
            finally:
                self._queue.task_done()
