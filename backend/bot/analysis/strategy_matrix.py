"""MITS Phase 15.C — StrategyMatrix matcher core.

Pairs each loaded YAML strategy template against a live
``RegimeVector`` + detector hits + analog cluster + IV state and emits
a ranked ``StrategyCandidate`` list. Hard gates exclude templates whose
``requires_*`` declarations don't match; soft scoring computes a
weighted alignment score; the final rank multiplies fit by cohort
posterior, analog win-rate and a sample-size shrinkage factor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backend.bot.analysis.strategy_templates import (
    StrategyTemplate, get_templates,
)
from backend.bot.corpus.knowledge_graph import get_posterior_with_fallback

if TYPE_CHECKING:
    from backend.bot.corpus.analog_retrieval import AnalogCluster
    from backend.bot.regime.vector import RegimeVector

logger = logging.getLogger(__name__)


_SHRINKAGE_PSEUDO = 30


@dataclass
class StrategyCandidate:
    strategy_name: str
    label: str
    direction: str
    fit_score: float
    cohort_win_rate: Optional[float]
    cohort_n: int
    cohort_ci_lower: Optional[float]
    cohort_ci_upper: Optional[float]
    cohort_source: str
    analog_win_rate: Optional[float]
    analog_n: int
    expected_payoff: float
    payoff_std: float
    p_max_loss: float
    ranked_position: int
    supporting_patterns: List[str]
    invalidation: List[str]
    requires_passed: List[str]
    requires_failed: List[str]
    final_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "label": self.label,
            "direction": self.direction,
            "fit_score": round(self.fit_score, 4),
            "cohort_win_rate": (round(self.cohort_win_rate, 4)
                                  if self.cohort_win_rate is not None else None),
            "cohort_n": int(self.cohort_n),
            "cohort_ci_lower": (round(self.cohort_ci_lower, 4)
                                  if self.cohort_ci_lower is not None else None),
            "cohort_ci_upper": (round(self.cohort_ci_upper, 4)
                                  if self.cohort_ci_upper is not None else None),
            "cohort_source": self.cohort_source,
            "analog_win_rate": (round(self.analog_win_rate, 4)
                                  if self.analog_win_rate is not None else None),
            "analog_n": int(self.analog_n),
            "expected_payoff": round(self.expected_payoff, 4),
            "payoff_std": round(self.payoff_std, 4),
            "p_max_loss": round(self.p_max_loss, 4),
            "ranked_position": int(self.ranked_position),
            "supporting_patterns": list(self.supporting_patterns),
            "invalidation": list(self.invalidation),
            "requires_passed": list(self.requires_passed),
            "requires_failed": list(self.requires_failed),
            "final_score": round(self.final_score, 6),
        }


@dataclass
class StrategyMatrix:
    ticker: str
    as_of: datetime
    query_state: Dict[str, Any]
    candidates: List[StrategyCandidate]
    top_strategy: Optional[StrategyCandidate]
    regime_health: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "query_state": dict(self.query_state),
            "candidates": [c.to_dict() for c in self.candidates],
            "top_strategy": (self.top_strategy.to_dict()
                                if self.top_strategy else None),
            "regime_health": self.regime_health,
        }


# ── hard-gate helpers ───────────────────────────────────────────────────

def _resolve_trend(rv: "RegimeVector") -> Optional[str]:
    val = getattr(rv.trend, "value", None)
    return str(val) if val else None


def _resolve_intraday(rv: "RegimeVector") -> Optional[str]:
    val = getattr(rv.intraday_regime, "value", None)
    return str(val) if val else None


def _resolve_gamma_state(rv: "RegimeVector") -> Optional[str]:
    val = getattr(rv.gamma_state, "value", None)
    if isinstance(val, dict):
        return val.get("regime")
    return str(val) if val else None


def _hard_gates(template: StrategyTemplate, *, rv: "RegimeVector",
                iv_rank: Optional[float]
                ) -> tuple[bool, List[str], List[str]]:
    """Apply every ``requires_*`` declaration. Returns
    (passed, passed_traces, failed_traces). DTE is tolerated (matcher
    emits the band, doesn't reject)."""
    passed: List[str] = []
    failed: List[str] = []

    rr = template.requires_regime
    if rr is not None:
        if rr.trend:
            trend = _resolve_trend(rv)
            if trend is None or trend == "unknown":
                passed.append(f"trend=unknown tolerated (wants {rr.trend})")
            elif trend in rr.trend:
                passed.append(f"trend={trend} ∈ {rr.trend}")
            else:
                failed.append(f"trend={trend} ∉ {rr.trend}")
        if rr.intraday_regime:
            intra = _resolve_intraday(rv)
            not_in = rr.intraday_regime.get("not_in") or []
            if intra is None or intra == "unknown":
                passed.append(f"intraday=unknown tolerated (blacklist {not_in})")
            elif intra in not_in:
                failed.append(f"intraday={intra} ∈ blacklist {not_in}")
            else:
                passed.append(f"intraday={intra} ∉ blacklist {not_in}")

    if template.requires_iv_rank_range:
        lo, hi = template.requires_iv_rank_range
        if iv_rank is None:
            passed.append(f"iv_rank=None tolerated (band {lo}-{hi})")
        elif lo <= iv_rank <= hi:
            passed.append(f"iv_rank={iv_rank:.0f} ∈ [{lo:.0f}, {hi:.0f}]")
        else:
            failed.append(f"iv_rank={iv_rank:.0f} ∉ [{lo:.0f}, {hi:.0f}]")

    if template.requires_gamma_state and template.requires_gamma_state.not_:
        gs = _resolve_gamma_state(rv)
        block = template.requires_gamma_state.not_
        if gs is None or gs == "unknown":
            passed.append(f"gamma_state=unknown tolerated (blacklist {block})")
        elif gs == block:
            failed.append(f"gamma_state={gs} == blacklist {block}")
        else:
            passed.append(f"gamma_state={gs} != blacklist {block}")

    # DTE is informational only — emit a trace, never gate on it.
    lo_d, hi_d = template.requires_dte_range
    passed.append(f"dte_band [{lo_d}, {hi_d}] emitted")

    return (not failed, passed, failed)


# ── soft scoring ────────────────────────────────────────────────────────

def _pattern_alignment(template: StrategyTemplate,
                       pattern_names: List[str]) -> tuple[float, List[str]]:
    if not template.edge_keys:
        return 0.0, []
    matched: List[str] = []
    for ek in template.edge_keys:
        if ek.pattern in pattern_names:
            matched.append(ek.pattern)
    score = min(1.0, len(matched) / len(template.edge_keys))
    return score, matched


def _regime_alignment(template: StrategyTemplate,
                      rv: "RegimeVector") -> float:
    rr = template.requires_regime
    if rr is None:
        return 1.0
    score = 0.0
    if rr.trend:
        trend = _resolve_trend(rv)
        if trend in rr.trend:
            score += 0.6
    else:
        score += 0.6
    if rr.intraday_regime:
        intra = _resolve_intraday(rv)
        not_in = rr.intraday_regime.get("not_in") or []
        if intra not in not_in:
            score += 0.4
    else:
        score += 0.4
    return score


def _iv_alignment(template: StrategyTemplate,
                  iv_rank: Optional[float]) -> float:
    if not template.requires_iv_rank_range:
        return 1.0
    lo, hi = template.requires_iv_rank_range
    if iv_rank is None:
        return 0.5
    return 1.0 if lo <= iv_rank <= hi else 0.0


def _analog_support_alignment(cohort_size: int) -> float:
    return min(1.0, cohort_size / 30.0)


# ── cohort aggregation ──────────────────────────────────────────────────

_SOURCE_RANK = {
    "cell": 4,
    "pattern_regime": 3,
    "pattern_regime_pool": 3,
    "pattern": 2,
    "pattern_pool": 2,
    "local_thin": 1,
    "none": 0,
}


def _aggregate_cohort(template: StrategyTemplate, *, ticker: str
                      ) -> Dict[str, Any]:
    """For each edge_key, call ``get_posterior_with_fallback``, then
    blend by sample-size weighted mean. Conservative CI band uses
    min(lower) / max(upper) across contributing cells. Best source
    survives across edge_keys."""
    total_n = 0
    weighted_post = 0.0
    ci_lowers: List[float] = []
    ci_uppers: List[float] = []
    best_source = "none"
    best_rank = -1
    for ek in template.edge_keys:
        entry = get_posterior_with_fallback(
            ticker=ticker, pattern=ek.pattern,
            regime=ek.regime, vol_state=ek.vol_state,
            time_bucket="rth", horizon="5d", sample_split="combined",
        )
        if entry is None:
            continue
        n = int(entry.get("n") or 0)
        post = entry.get("posterior")
        if n <= 0 or post is None:
            continue
        total_n += n
        weighted_post += n * float(post)
        lo = entry.get("confidence_lower")
        hi = entry.get("confidence_upper")
        if lo is not None:
            ci_lowers.append(float(lo))
        if hi is not None:
            ci_uppers.append(float(hi))
        src = str(entry.get("source") or "none")
        r = _SOURCE_RANK.get(src, 0)
        if r > best_rank:
            best_rank = r
            best_source = src
    if total_n == 0:
        return {"win_rate": None, "n": 0, "ci_lower": None,
                "ci_upper": None, "source": "none"}
    return {
        "win_rate": weighted_post / total_n,
        "n": total_n,
        "ci_lower": min(ci_lowers) if ci_lowers else None,
        "ci_upper": max(ci_uppers) if ci_uppers else None,
        "source": best_source,
    }


# ── analog stats ────────────────────────────────────────────────────────

def _analog_stats(analogs: "AnalogCluster") -> Dict[str, Any]:
    rows = list(analogs.analogs)
    n = len(rows)
    if n == 0:
        return {
            "win_rate": None, "n": int(analogs.cohort_size),
            "expected_payoff": 0.0, "payoff_std": 0.0, "p_max_loss": 0.0,
        }
    wins = sum(1 for a in rows if a.realized_return_pct > 0)
    losses = sum(1 for a in rows if a.realized_return_pct < -10.0)
    dist = analogs.outcome_distribution or {}
    return {
        "win_rate": wins / n,
        "n": int(analogs.cohort_size),
        "expected_payoff": float(dist.get("mean", 0.0) or 0.0),
        "payoff_std": float(dist.get("std", 0.0) or 0.0),
        "p_max_loss": losses / n,
    }


# ── public builder ──────────────────────────────────────────────────────

def _shrinkage(n: int) -> float:
    return n / (n + _SHRINKAGE_PSEUDO)


def build_strategy_matrix(
    *,
    ticker: str,
    regime_vector: "RegimeVector",
    pattern_hits: List[Dict[str, Any]],
    analogs: "AnalogCluster",
    iv_state: Dict[str, Any],
    greeks: Optional[Dict[str, Any]] = None,
) -> StrategyMatrix:
    """Match every loaded template against the live state and return a
    ranked ``StrategyMatrix``.

    Hard gates exclude templates whose ``requires_*`` fail. Soft scoring
    weights pattern / regime / IV alignment + analog cohort depth. Final
    rank is fit × cohort_win_rate × analog_win_rate × shrinkage(cohort_n).
    """
    templates = get_templates()
    iv_rank = iv_state.get("iv_rank") if iv_state else None
    try:
        iv_rank_f: Optional[float] = (
            float(iv_rank) if iv_rank is not None else None
        )
    except (TypeError, ValueError):
        iv_rank_f = None

    pattern_names = [str(h.get("pattern")) for h in (pattern_hits or [])
                     if h.get("pattern")]
    analog_stats = _analog_stats(analogs)

    query_state: Dict[str, Any] = {
        "ticker": ticker.upper(),
        "trend": _resolve_trend(regime_vector),
        "volatility_state": getattr(regime_vector.volatility_state,
                                      "value", None),
        "iv_rank": iv_rank_f,
        "iv_regime": getattr(regime_vector.iv_regime, "value", None),
        "intraday_regime": _resolve_intraday(regime_vector),
        "gamma_state": _resolve_gamma_state(regime_vector),
        "macro_regime": getattr(regime_vector.macro_regime, "value", None),
        "pattern_hits": pattern_names,
        "analog_cohort_size": int(analogs.cohort_size),
    }

    candidates: List[StrategyCandidate] = []

    for name, template in templates.items():
        ok, passed, failed = _hard_gates(
            template, rv=regime_vector, iv_rank=iv_rank_f,
        )
        if not ok:
            continue
        pa_score, matched = _pattern_alignment(template, pattern_names)
        ra_score = _regime_alignment(template, regime_vector)
        iv_score = _iv_alignment(template, iv_rank_f)
        an_score = _analog_support_alignment(int(analogs.cohort_size))

        w = template.scoring_weights
        fit_score = (
            w.pattern_alignment * pa_score
            + w.regime_alignment * ra_score
            + w.iv_alignment * iv_score
            + w.analog_support * an_score
        )

        cohort = _aggregate_cohort(template, ticker=ticker)
        final_score = (
            fit_score
            * (cohort["win_rate"] if cohort["win_rate"] is not None else 0.5)
            * (analog_stats["win_rate"]
                  if analog_stats["win_rate"] is not None else 0.5)
            * _shrinkage(int(cohort["n"]))
        )

        candidates.append(StrategyCandidate(
            strategy_name=name,
            label=template.label,
            direction=template.direction,
            fit_score=fit_score,
            cohort_win_rate=cohort["win_rate"],
            cohort_n=int(cohort["n"]),
            cohort_ci_lower=cohort["ci_lower"],
            cohort_ci_upper=cohort["ci_upper"],
            cohort_source=cohort["source"],
            analog_win_rate=analog_stats["win_rate"],
            analog_n=int(analog_stats["n"]),
            expected_payoff=float(analog_stats["expected_payoff"]),
            payoff_std=float(analog_stats["payoff_std"]),
            p_max_loss=float(analog_stats["p_max_loss"]),
            ranked_position=0,
            supporting_patterns=matched,
            invalidation=list(template.invalidation_default),
            requires_passed=passed,
            requires_failed=failed,
            final_score=final_score,
        ))

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    for i, c in enumerate(candidates, start=1):
        c.ranked_position = i

    top = candidates[0] if candidates else None

    return StrategyMatrix(
        ticker=ticker.upper(),
        as_of=datetime.utcnow(),
        query_state=query_state,
        candidates=candidates,
        top_strategy=top,
        regime_health=getattr(regime_vector, "health", "unknown"),
    )


__all__ = [
    "StrategyCandidate",
    "StrategyMatrix",
    "build_strategy_matrix",
]
