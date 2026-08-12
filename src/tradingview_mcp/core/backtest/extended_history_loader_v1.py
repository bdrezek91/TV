"""Load BACKTEST_COMPARISON_V1 extended cache into replay inputs.

The loader never substitutes fake liquidations/orderbook data. It returns the
available HistoricalInputs plus an explicit capability matrix describing which
comparison variants may be interpreted honestly for this cache.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tradingview_mcp.core.backtest.comparison_history_v1 import HistoricalInputs
from tradingview_mcp.core.backtest.extended_history_downloader_v1 import load_cached_rows


@dataclass(frozen=True)
class ExtendedReplayBundle:
    cache_dir: Path
    manifest: Mapping[str, Any]
    inputs: HistoricalInputs
    capabilities: Mapping[str, Any]


def _daily_trade_rows(root: Path) -> list[dict]:
    daily = root / "trades_1m_daily"
    if not daily.exists():
        return []
    rows: list[dict] = []
    for path in sorted(daily.glob("*.json")):
        rows.extend(load_cached_rows(path, time_fields=("bucket_start",)))
    dedup = {r["bucket_start"]: r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def load_extended_bundle(cache_dir: Path) -> ExtendedReplayBundle:
    cache_dir = Path(cache_dir)
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing extended-history manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    c1m = load_cached_rows(cache_dir / "kline_1m.json", time_fields=("open_time",))
    c15 = load_cached_rows(cache_dir / "kline_15m.json", time_fields=("open_time",))
    c1h = load_cached_rows(cache_dir / "kline_1h.json", time_fields=("open_time",))
    c4h = load_cached_rows(cache_dir / "kline_4h.json", time_fields=("open_time",))
    trades = _daily_trade_rows(cache_dir)
    derivatives = load_cached_rows(cache_dir / "derivatives_reconstructed.json", time_fields=("source_timestamp",))

    # Deliberately empty until a validated historical source exists.
    liquidations: list[dict] = []
    orderbook: list[dict] = []

    inputs = HistoricalInputs(
        candles_4h=c4h,
        candles_1h=c1h,
        candles_15m=c15,
        candles_1m=c1m,
        trades_1m=trades,
        liquidations_1m=liquidations,
        orderbook=orderbook,
        derivatives=derivatives,
    )

    sources = manifest.get("sources") or {}
    has_price = all(bool(sources.get(name, {}).get("available")) for name in ("kline_1m", "kline_15m", "kline_1h", "kline_4h"))
    has_trades = bool(sources.get("trades_1m", {}).get("available")) and bool(trades)
    has_derivatives = bool(sources.get("derivatives", {}).get("available")) and bool(derivatives)
    has_orderbook = bool(sources.get("orderbook", {}).get("available")) and bool(orderbook)
    has_liquidations = bool(sources.get("liquidations", {}).get("available")) and bool(liquidations)

    # Legacy Feature Engine has orderbook in required_sources, therefore a
    # no-orderbook extended run would systematically force is_tradeable=False.
    legacy_exact = has_price and has_trades and has_derivatives and has_orderbook
    research_degraded = has_price and has_trades and has_derivatives
    research_exact = research_degraded and has_orderbook and has_liquidations

    capabilities = {
        "LEGACY_AS_IS_EXACT": legacy_exact,
        "LEGACY_FIXED_TV_EXACT": legacy_exact,
        "RESEARCH_V2_EXACT": research_exact,
        "RESEARCH_V2_DEGRADED": research_degraded,
        "RESEARCH_V2_TV_EXACT": False,
        "missing_orderbook": not has_orderbook,
        "missing_liquidations": not has_liquidations,
        "interpretation": (
            "Full legacy comparison forbidden: legacy data-quality gate requires orderbook."
            if not legacy_exact else "Legacy exact replay inputs available."
        ),
    }
    return ExtendedReplayBundle(cache_dir, manifest, inputs, capabilities)


def discover_cache_dirs(cache_root: Path, symbol: str | None = None) -> list[Path]:
    root = Path(cache_root)
    if not root.exists():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    if symbol:
        prefix = symbol.upper() + "_"
        dirs = [p for p in dirs if p.name.startswith(prefix)]
    return sorted(dirs)
