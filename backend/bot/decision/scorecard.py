"""MITS Phase 16.C — Decision Quality Scorecard.

Pure function that reads a decision-provenance bag-of-fields and emits a
composite quality score along four axes:

  • analysis_quality   — regime health + ensemble agreement + top-strategy fit
  • council_agreement  — chairman dissent share + consensus confidence
  • risk_quality       — correlation pressure + policy soft penalties
  • execution_quality  — spread + IV freshness + liquidity

Each sub-score and the composite are normalized to 0..100. The composite
is a config-weighted blend (defaults sum to 1.0). The function is pure
— no DB reads, no network — so it can be replayed offline against any
historical provenance row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


_DEFAULT_WEIGHTS: Dict[str, float] = {
    "analysis_quality": 0.30,
    "council_agreement": 0.30,
    "risk_quality": 0.25,
    "execution_quality": 0.15,
}

# Regime health maps 1:1 to a numeric prior. Green = fully trustworthy
# regime read; yellow = partial; red = degraded. Default to yellow when
# the regime_vector is absent (engine cycle that ran with stub data).
_REGIME_HEALTH_PRIOR = {"green": 1.0, "yellow": 0.6, "red": 0.2}


@dataclass
class DecisionQualityScore:
    analysis_quality: float
    council_agreement: float
    risk_quality: float
    execution_quality: float
    composite: float
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "analysis_quality": round(self.analysis_quality, 2),
            "council_agreement": round(self.council_agreement, 2),
            "risk_quality": round(self.risk_quality, 2),
            "execution_quality": round(self.execution_quality, 2),
            "composite": round(self.composite, 2),
            "components": {
                k: round(float(v), 4) for k, v in self.components.items()
            },
        }


def _clip01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _f(x: Any, default: float = 0.0) -> float:
    """Coerce JSON-decoded value to float; preserve default on bad input."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def _analysis_quality(provenance: Dict[str, Any]) -> tuple[float, Dict[str, float]]:
    rv = provenance.get("regime_vector") or {}
    regime_health_str = str(rv.get("health") or "yellow")
    regime_health = _REGIME_HEALTH_PRIOR.get(regime_health_str, 0.6)

    # Engine cycle path doesn't run the deep composer — the fast/deep
    # ensemble agreement is therefore an unobservable here. The 0.75
    # prior says "no contradictory signal" without claiming certainty.
    ensemble_agreement = 0.75

    sm = provenance.get("strategy_matrix") or {}
    top_strat = sm.get("top_strategy") or {}
    top_strategy_fit = _clip01(_f(top_strat.get("fit_score"), 0.6))

    score = 100.0 * (regime_health + ensemble_agreement + top_strategy_fit) / 3.0
    components = {
        "regime_health": float(regime_health),
        "ensemble_agreement": float(ensemble_agreement),
        "top_strategy_fit": float(top_strategy_fit),
    }
    return score, components


def _council_agreement(provenance: Dict[str, Any]) -> tuple[float, Dict[str, float]]:
    chairman = provenance.get("chairman_memo") or {}
    dissent = chairman.get("dissent") or {}
    dissent_share = _clip01(_f(dissent.get("dissent_share"), 0.0))

    consensus = provenance.get("consensus") or {}
    consensus_conf = _clip01(_f(consensus.get("confidence"), 0.0))

    score = 100.0 * (1.0 - dissent_share) * min(1.0, consensus_conf * 1.25)
    components = {
        "dissent_share": dissent_share,
        "consensus_confidence": consensus_conf,
    }
    return score, components


def _risk_quality(provenance: Dict[str, Any]) -> tuple[float, Dict[str, float]]:
    cap = provenance.get("correlation_cap") or {}
    max_corr = abs(_f(cap.get("worst_rho"), 0.0))
    # Linear decay from 1.0 at |rho| <= 0.5 to 0.0 at |rho| >= 1.0.
    correlation_score = _clip01(1.0 - max(0.0, max_corr - 0.5) / 0.5)

    policy = provenance.get("policy_result") or {}
    soft_pen = max(0.0, _f(policy.get("soft_penalties_total_pct"), 0.0))

    score = 100.0 * correlation_score * max(0.0, 1.0 - soft_pen / 100.0)
    components = {
        "max_correlation": max_corr,
        "soft_penalties_pct": soft_pen,
        "correlation_score": correlation_score,
    }
    return score, components


def _execution_quality(provenance: Dict[str, Any]) -> tuple[float, Dict[str, float]]:
    # The regime_vector carries the only execution-context fields the
    # engine cycle currently stamps: iv_rank freshness. Spread and
    # volume_ratio are NOT in regime_vector today — the helpers fall
    # back to neutral 0.5 defaults so the axis is comparable across
    # cycles regardless of whether those fields are populated.
    rv = provenance.get("regime_vector") or {}

    spread_pct = max(0.0, _f(rv.get("spread_pct"), 0.5))
    spread_score = _clip01(1.0 - min(1.0, spread_pct))

    iv_rank_obj = rv.get("iv_rank") or {}
    if isinstance(iv_rank_obj, dict):
        iv_freshness_sec = _f(iv_rank_obj.get("freshness_seconds"), 600.0)
    else:
        iv_freshness_sec = 600.0
    if iv_freshness_sec < 300:
        iv_freshness_score = 1.0
    else:
        iv_freshness_score = max(0.2, 1.0 - iv_freshness_sec / 1800.0)

    liq_score = _clip01(_f(rv.get("volume_ratio"), 0.5))

    score = 100.0 * (
        0.4 * spread_score + 0.3 * iv_freshness_score + 0.3 * liq_score
    )
    components = {
        "spread_score": spread_score,
        "iv_freshness_score": iv_freshness_score,
        "liquidity_score": liq_score,
        "iv_freshness_seconds": iv_freshness_sec,
    }
    return score, components


def score_decision(
    provenance: Dict[str, Any],
    *,
    weights: Optional[Dict[str, float]] = None,
) -> DecisionQualityScore:
    """Score a decision-provenance dict along four axes + composite.

    ``provenance`` must contain (any subset of) the keys written into a
    ``decision_provenance`` row: ``regime_vector``, ``strategy_matrix``,
    ``consensus``, ``chairman_memo``, ``policy_result``,
    ``simulator_verdict``, ``correlation_cap``, ``portfolio_context``,
    ``agent_outputs``. Missing keys → axis falls back to neutral default
    so a sparse row still produces a score on [0, 100].

    Weights override the per-axis blend; the four keys
    ``analysis_quality / council_agreement / risk_quality /
    execution_quality`` must each be non-negative. Normalization is the
    caller's responsibility.
    """
    aq, aq_components = _analysis_quality(provenance)
    cg, cg_components = _council_agreement(provenance)
    rq, rq_components = _risk_quality(provenance)
    eq, eq_components = _execution_quality(provenance)

    w = weights or _DEFAULT_WEIGHTS
    composite = (
        w.get("analysis_quality", 0.0) * aq
        + w.get("council_agreement", 0.0) * cg
        + w.get("risk_quality", 0.0) * rq
        + w.get("execution_quality", 0.0) * eq
    )

    components: Dict[str, float] = {}
    for prefix, src in (
        ("analysis", aq_components),
        ("council", cg_components),
        ("risk", rq_components),
        ("execution", eq_components),
    ):
        for k, v in src.items():
            components[f"{prefix}.{k}"] = float(v)

    return DecisionQualityScore(
        analysis_quality=aq,
        council_agreement=cg,
        risk_quality=rq,
        execution_quality=eq,
        composite=composite,
        components=components,
    )
