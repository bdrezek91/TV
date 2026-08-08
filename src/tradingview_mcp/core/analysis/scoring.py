"""Scoring Engine: loads versioned weights from `config/strategies/v1.yaml`
and turns a strategy's raw component readings into a 0-100 score plus a
per-component breakdown (persisted as `signal_components` rows).

The score NEVER overrides hard gates (`hard_gates.py`) — a 100/100 setup
that fails a hard gate is still REJECTED. This module only computes the
number; gating is a separate, later step in the orchestrator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import yaml

_RELATIVE_CONFIG = Path("config") / "strategies" / "v1.yaml"

# `__file__` lives at src/tradingview_mcp/core/analysis/scoring.py in a
# source checkout, so parents[4] is the repo root there — but a `pip`/`uv
# pip install` copies the package into site-packages, severing that
# relationship entirely (this is exactly what the Docker image does via
# `uv pip install --system .`). So this is tried first as a nice-to-have in
# dev, never assumed to exist.
_PACKAGE_RELATIVE_CANDIDATE = Path(__file__).resolve().parents[4] / _RELATIVE_CONFIG


class StrategyConfigError(ValueError):
    pass


def _resolve_default_config_path() -> Path:
    """Resolution order: explicit env override -> package-relative (source
    checkout) -> current working directory -> the container's known
    `/app` WORKDIR (matches this project's Dockerfile). Raises a clear
    error if none of them exist, rather than failing deep inside `yaml`
    with a confusing FileNotFoundError."""
    env_override = os.environ.get("STRATEGY_CONFIG_PATH")
    candidates = []
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(_PACKAGE_RELATIVE_CANDIDATE)
    candidates.append(Path.cwd() / _RELATIVE_CONFIG)
    candidates.append(Path("/app") / _RELATIVE_CONFIG)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise StrategyConfigError(
        "could not locate config/strategies/v1.yaml — tried: "
        + ", ".join(str(c) for c in candidates)
        + ". Set STRATEGY_CONFIG_PATH explicitly if the file lives elsewhere."
    )


DEFAULT_CONFIG_PATH = None  # resolved lazily by _resolve_default_config_path(); kept as a name for backwards-compat imports


@lru_cache(maxsize=8)
def load_strategy_config(path: Optional[str] = None) -> dict:
    config_path = Path(path) if path else _resolve_default_config_path()
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    weights = config.get("scoring", {}).get("weights", {})
    total = sum(Decimal(str(w)) for w in weights.values())
    if total != Decimal("100"):
        raise StrategyConfigError(
            f"strategy config {config_path} scoring.weights must sum to 100, got {total}"
        )
    if not config.get("version"):
        raise StrategyConfigError(f"strategy config {config_path} is missing a top-level 'version'")
    return config


def clear_config_cache() -> None:
    load_strategy_config.cache_clear()


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    weight: Decimal
    fraction: Decimal  # 0..1 achievement of this component
    contribution: Decimal
    notes: str = ""


@dataclass(frozen=True)
class ScoreResult:
    total_score: int
    components: list
    config_version: str


def score_setup(component_fractions: Dict[str, Decimal], *, config_path: Optional[str] = None) -> ScoreResult:
    """`component_fractions` maps a weight-key (see v1.yaml's
    `scoring.weights`) to a 0..1 Decimal fraction of how strongly that
    component was satisfied. Missing keys score 0 for that component
    (conservative default — an unevaluated component never contributes).
    """
    config = load_strategy_config(config_path)
    weights = config["scoring"]["weights"]

    components = []
    total = Decimal("0")
    for name, weight in weights.items():
        weight_dec = Decimal(str(weight))
        fraction = component_fractions.get(name, Decimal("0"))
        fraction = max(Decimal("0"), min(Decimal("1"), Decimal(str(fraction))))
        contribution = (weight_dec * fraction).quantize(Decimal("0.01"))
        total += contribution
        components.append(
            ScoreComponent(name=name, weight=weight_dec, fraction=fraction, contribution=contribution)
        )

    return ScoreResult(
        total_score=int(round(total)),
        components=components,
        config_version=config["version"],
    )
