"""MITS Phase 15.C — YAML strategy templates + pydantic-validated loader.

The loaded dict is a descriptive layer used by ``strategy_matrix`` to
rank candidate strategies against a live ``RegimeVector`` + analog
cohort. The execution layer (``backend/bot/strategies/all_strategies.py``)
is unchanged — templates are read-only metadata.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "config" / "strategies"


class RequiresRegime(BaseModel):
    trend: Optional[List[str]] = None
    intraday_regime: Optional[Dict[str, List[str]]] = None


class RequiresGamma(BaseModel):
    # `not` is a Python keyword — accept it via alias from YAML.
    not_: Optional[str] = Field(default=None, alias="not")

    model_config = {"populate_by_name": True}


class StrategyLeg(BaseModel):
    side: str
    delta_target: float


class EdgeKey(BaseModel):
    pattern: str
    regime: str
    vol_state: str


class ScoringWeights(BaseModel):
    pattern_alignment: float
    regime_alignment: float
    iv_alignment: float
    analog_support: float


class StrategyTemplate(BaseModel):
    name: str
    label: str
    direction: str
    category: str
    requires_regime: Optional[RequiresRegime] = None
    requires_iv_rank_range: Optional[List[float]] = None
    requires_dte_range: List[int]
    requires_gamma_state: Optional[RequiresGamma] = None
    legs: List[StrategyLeg]
    max_loss_calculation: str
    edge_keys: List[EdgeKey]
    scoring_weights: ScoringWeights
    invalidation_default: List[str]


_TEMPLATES: Optional[Dict[str, StrategyTemplate]] = None


def load_strategy_templates(directory: Path = _DEFAULT_DIR
                                       ) -> Dict[str, StrategyTemplate]:
    """Eager-load every ``*.yaml`` in ``directory`` and pydantic-validate.

    Raises ``FileNotFoundError`` if the directory is missing or empty,
    ``yaml.YAMLError`` on malformed YAML, ``ValueError`` on schema
    violation. Cached at module level after the first successful load —
    boot fails loud if any template is bad.
    """
    global _TEMPLATES
    if _TEMPLATES is not None:
        return _TEMPLATES

    path = Path(directory)
    if not path.is_dir():
        raise FileNotFoundError(
            f"strategy templates directory missing: {path}"
        )
    files = sorted(path.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(
            f"no *.yaml strategy templates found under {path}"
        )

    loaded: Dict[str, StrategyTemplate] = {}
    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError(
                f"{f.name}: top-level YAML must be a mapping, got "
                f"{type(raw).__name__}"
            )
        try:
            tpl = StrategyTemplate.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"{f.name}: {exc}") from exc
        if tpl.name in loaded:
            raise ValueError(
                f"duplicate strategy name {tpl.name!r} "
                f"({f.name} collides with earlier file)"
            )
        loaded[tpl.name] = tpl

    _TEMPLATES = loaded
    logger.info("strategy_templates: loaded %d templates from %s",
                len(loaded), path)
    return _TEMPLATES


def reset_templates_cache() -> None:
    """Test hook — clears the module-level cache so a subsequent
    ``load_strategy_templates()`` call re-reads the directory."""
    global _TEMPLATES
    _TEMPLATES = None


def get_templates() -> Dict[str, StrategyTemplate]:
    """Convenience accessor for callers that just want the cached dict."""
    if _TEMPLATES is None:
        return load_strategy_templates()
    return _TEMPLATES


__all__ = [
    "EdgeKey",
    "RequiresGamma",
    "RequiresRegime",
    "ScoringWeights",
    "StrategyLeg",
    "StrategyTemplate",
    "get_templates",
    "load_strategy_templates",
    "reset_templates_cache",
]
