"""Render BACKTEST_COMPARISON_V1 JSON outputs into one Polish Markdown report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradingview_mcp.core.backtest.comparison_report_v1 import render_markdown


DEFAULT_ROOT = Path("/app/artifacts/research/backtest_comparison")


def _load(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--guarded", help="override guarded summary JSON path")
    p.add_argument("--extended", help="override extended summary JSON path")
    p.add_argument("--output", help="override output .md path")
    args = p.parse_args()

    root = Path(args.root)
    guarded_path = Path(args.guarded) if args.guarded else root / "guarded_smoke_summary.json"
    extended_path = Path(args.extended) if args.extended else root / "extended_research_v2_tv_shadow_summary.json"
    output = Path(args.output) if args.output else root / "comparison_report.md"
    guarded = _load(guarded_path) or {}
    extended = _load(extended_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(guarded, extended), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "guarded_loaded": bool(guarded),
        "extended_loaded": extended is not None,
    }, indent=2))


if __name__ == "__main__":
    main()
