"""Markdown report renderer for BACKTEST_COMPARISON_V1 outputs.

The renderer is intentionally conservative: it never upgrades a smoke/degraded
run into a statistical verdict and never declares a winner when the underlying
summary says the sample is insufficient/preliminary.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional


VARIANT_ORDER = ("LEGACY_AS_IS", "LEGACY_FIXED_TV", "RESEARCH_V2")


def _num(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _pct(value: Any) -> str:
    d = _num(value)
    return "—" if d is None else f"{(d * Decimal('100')):.1f}%"


def _fmt(value: Any, places: int = 3) -> str:
    d = _num(value)
    return "—" if d is None else f"{d:.{places}f}"


def _variant_rows(summary: Mapping[str, Mapping[str, Any]]) -> list[str]:
    rows = []
    for variant in VARIANT_ORDER:
        s = summary.get(variant) or {}
        signals = s.get("signals", 0)
        filled = s.get("filled", 0)
        trades = s.get("trades_with_r", 0)
        fill_rate = (Decimal(filled) / Decimal(signals)) if signals else None
        rows.append(
            "| " + " | ".join((
                variant,
                str(signals),
                str(filled),
                _pct(fill_rate),
                str(trades),
                _pct(s.get("win_rate")),
                _fmt(s.get("expectancy_r")),
                _fmt(s.get("profit_factor")),
                str(s.get("sample_status") or "—"),
            )) + " |"
        )
    return rows


def statistical_verdict(guarded: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Return a deliberately strict verdict from exact-run metadata only."""
    reasons: list[str] = []
    if not guarded:
        return "NO_STATISTICALLY_MEANINGFUL_WINNER", ["brak exact guarded run"]
    if not guarded.get("final_statistical_verdict_allowed", False):
        reasons.append("exact runner nie zezwala na finalny werdykt dla tego runu")
    summary = ((guarded.get("primary_execution") or {}).get("summary") or {})
    for variant in VARIANT_ORDER:
        status = (summary.get(variant) or {}).get("sample_status")
        if status in {None, "INSUFFICIENT_SAMPLE", "PRELIMINARY"}:
            reasons.append(f"{variant}: próbka {status or 'brak'}")
    if reasons:
        return "NO_STATISTICALLY_MEANINGFUL_WINNER", reasons

    # The current exact smoke contract intentionally does not provide enough
    # portfolio/drawdown metrics for a responsible automatic ranking. Even if
    # every variant has >=100 trades, leave the final winner to the fuller
    # report version rather than selecting on one metric such as ROI/PF.
    return "NO_STATISTICALLY_MEANINGFUL_WINNER", [
        "automatyczny ranking risk-adjusted nie jest jeszcze dopuszczony; wymagany full-system report"
    ]


def _shadow_section(shadow: Mapping[str, Any]) -> list[str]:
    if not shadow:
        return ["Brak danych TV shadow w exact runie."]
    lines = [
        "| TV shadow | Sygnały | Trades | Win rate | Expectancy R | PF | Próbka |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    outcomes = shadow.get("outcomes_by_status") or {}
    for status in ("CONFIRMED", "WEAK_CONFIRMATION", "NEUTRAL", "CONTRADICTED", "INSUFFICIENT_DATA"):
        s = outcomes.get(status) or {}
        lines.append(
            "| " + " | ".join((
                status,
                str(s.get("signals", 0)),
                str(s.get("trades_with_r", 0)),
                _pct(s.get("win_rate")),
                _fmt(s.get("expectancy_r")),
                _fmt(s.get("profit_factor")),
                str(s.get("sample_status") or "—"),
            )) + " |"
        )
    return lines


def render_markdown(guarded: Mapping[str, Any], extended: Optional[Mapping[str, Any]] = None) -> str:
    exact_summary = ((guarded.get("primary_execution") or {}).get("summary") or {}) if guarded else {}
    verdict, verdict_reasons = statistical_verdict(guarded)
    lines = [
        "# TV / Legacy vs Research V2 — wynik benchmarku",
        "",
        f"**Werdykt:** `{verdict}`",
        "",
    ]
    if verdict_reasons:
        lines.append("Powody: " + "; ".join(verdict_reasons) + ".")
        lines.append("")

    lines += [
        "## 1. Exact local — signal quality",
        "",
        f"Run status: `{guarded.get('status', 'BRAK') if guarded else 'BRAK'}`; "
        f"okno: `{((guarded.get('used_window') or {}).get('start')) if guarded else None}` → "
        f"`{((guarded.get('used_window') or {}).get('end')) if guarded else None}`.",
        "",
        "| System | Sygnały | Filled | Fill rate | Closed/R | Win rate | Expectancy R | PF | Status próbki |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        *_variant_rows(exact_summary),
        "",
        "## 2. Paired signals",
        "",
    ]
    pairs = guarded.get("paired_legacy_fixed_vs_research") or {} if guarded else {}
    if pairs:
        for key in ("BOTH_SIGNAL_SAME_DIRECTION", "ONLY_LEGACY", "ONLY_RESEARCH_V2", "DIRECTION_CONFLICT", "BOTH_NO_TRADE"):
            lines.append(f"- `{key}`: {pairs.get(key, 0)}")
    else:
        lines.append("Brak paired-signal danych.")

    lines += ["", "## 3. TradingView shadow — value-add", ""]
    shadow = guarded.get("research_v2_tv_shadow") or {} if guarded else {}
    lines += _shadow_section(shadow)
    lines += [
        "",
        "TV shadow jest obserwacyjny (`can_affect_decision=false`); powyższe wyniki nie zmieniały historycznych W4/W5.",
        "",
        "## 4. W5 production-as-is sensitivity",
        "",
    ]
    prod_summary = ((guarded.get("production_as_is_execution_sensitivity") or {}).get("summary") or {}) if guarded else {}
    if prod_summary:
        lines += [
            "| System | Sygnały | Filled | Fill rate | Closed/R | Win rate | Expectancy R | PF | Status próbki |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
            *_variant_rows(prod_summary),
        ]
    else:
        lines.append("Brak production-as-is sensitivity run.")

    lines += ["", "## 5. Extended 30/90d", ""]
    if extended:
        lines += [
            f"Run: `{extended.get('run_kind')}` / `{extended.get('status')}`.",
            "",
            f"Final TV-vs-bot verdict allowed: `{extended.get('final_tv_vs_bot_verdict_allowed')}`.",
            "",
        ]
        limits = extended.get("limitations") or []
        if limits:
            lines.append("Ograniczenia extended:")
            lines += [f"- `{x}`" for x in limits]
        lines += ["", "### Research V2 baseline", ""]
        b = extended.get("baseline_research_v2") or {}
        lines.append(
            f"Sygnały: {b.get('signals', 0)}, trades/R: {b.get('trades_with_r', 0)}, "
            f"win rate: {_pct(b.get('win_rate'))}, expectancy: {_fmt(b.get('expectancy_r'))}R, "
            f"PF: {_fmt(b.get('profit_factor'))}, próbka: `{b.get('sample_status', '—')}`."
        )
    else:
        lines.append("Brak extended runu.")

    lines += [
        "",
        "## 6. Znane ograniczenia metodologiczne",
        "",
        "- Historyczne TradingView jest rekonstruowane z zamkniętych świec (`TV_RECONSTRUCTED_CLASSIC_TA_V1`), a nie pobierane jako prawdziwy point-in-time TV snapshot.",
        "- Extended replay bez zweryfikowanego historycznego orderbooka/likwidacji jest `DEGRADED` i nie rozstrzyga legacy vs V2.",
        "- Finalny wybór systemu musi uwzględnić co najmniej expectancy, PF, drawdown, recovery, sample size i stabilność między okresami/symbolami.",
        "",
    ]
    return "\n".join(lines)
