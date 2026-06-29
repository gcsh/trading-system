"""Trade Ranking Engine.

Grades a candidate trade (A+ / A / B / C / Reject) by blending the things that
genuinely separate good setups from noise:

    composite = w_p * win_probability
              + w_rr * normalised(R/R)
              + w_reg * regime_alignment
              + w_conf * multi_timeframe_confluence
              + w_flow * institutional-flow agreement
              + w_liq * liquidity quality

Grade cutoffs come from ``TUNABLES.rank_grade_*`` so the user can re-tune the bar
without touching code. The output includes a per-component breakdown so anyone
auditing the call can see exactly which factors made or broke it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES


_PIN_HEADWIND_THRESHOLD = 0.6
_REGIME_ALIGN_HELP_THRESHOLD = 0.65
_REGIME_ALIGN_HURT_THRESHOLD = 0.40
_CI_PENALTY_NOTABLE_THRESHOLD = 0.90

# Weights — sum to 1.0. Adjust here (or move to Tunables) to retune.
_WEIGHTS = {
    "probability": 0.35,
    "risk_reward": 0.15,
    "regime":      0.15,
    "confluence":  0.15,
    "flow":        0.10,
    "liquidity":   0.10,
}

_GRADE_ORDER = ["Reject", "C", "B", "A", "A+"]


@dataclass
class TradeRank:
    grade: str = "Reject"
    score: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    reasoning: List[str] = field(default_factory=list)
    # MITS Phase 14.E placeholder — populated by the explainer pass.
    # The field exists here so the dataclass is stable when downstream
    # consumers (UI cards, agent_context) read it.
    grade_explainer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _grade_for(score: float) -> str:
    if score >= TUNABLES.rank_grade_aplus:
        return "A+"
    if score >= TUNABLES.rank_grade_a:
        return "A"
    if score >= TUNABLES.rank_grade_b:
        return "B"
    if score >= TUNABLES.rank_grade_c:
        return "C"
    return "Reject"


def passes_min_grade(grade: str, min_grade: Optional[str]) -> bool:
    if not min_grade:
        return True
    try:
        return _GRADE_ORDER.index(grade) >= _GRADE_ORDER.index(min_grade)
    except ValueError:
        return True


def _aligned(direction: str, signed_value: float) -> float:
    """Map a signed bias [-1..1] into a 0-1 alignment score with `direction`."""
    if direction == "LONG":
        x = signed_value
    elif direction == "SHORT":
        x = -signed_value
    else:
        return 0.5
    return max(0.0, min(1.0, 0.5 + 0.5 * x))


def rank_trade(
    probability: Any,                # SignalProbability (duck-typed)
    regime: Any,                     # MarketRegime
    confluence: Optional[Any],       # ConfluenceScore
    features: Dict[str, Any],
) -> TradeRank:
    """Grade a trade given the upstream analytics outputs. Pure + safe."""
    direction = getattr(probability, "direction", "NEUTRAL")
    p = float(getattr(probability, "probability", 0.0) or 0.0)
    rr = getattr(probability, "risk_reward", None)
    rr_norm = max(0.0, min(1.0, (float(rr) - 1.0) / 3.0)) if rr else 0.4   # 1:1 → 0, 4:1 → 1.0

    regime_trend = getattr(regime, "trend", "unknown")
    regime_signed = 1.0 if regime_trend == "bullish" else (-1.0 if regime_trend == "bearish" else 0.0)
    regime_align = _aligned(direction, regime_signed)
    if getattr(regime, "volatility", "normal") == "high":
        regime_align *= 0.85   # whippy environments penalise

    if confluence is None:
        conf_align = 0.5
    else:
        cdir = getattr(confluence, "direction", "neutral")
        cscore = float(getattr(confluence, "score", 0.0) or 0.0)
        c_signed = (1.0 if cdir == "bullish" else (-1.0 if cdir == "bearish" else 0.0)) * cscore
        conf_align = _aligned(direction, c_signed)

    flow_align = _aligned(direction, float(features.get("flow_bullishness") or 0.0))
    vol_ratio = features.get("volume_ratio")
    liquidity = max(0.0, min(1.0, float(vol_ratio))) if isinstance(vol_ratio, (int, float)) else 0.5

    # Dealer pinning penalty: a long trade straight into a high-pin call wall
    # (or a short into a put wall) is a classic edge eraser — dampen regime/flow
    # alignment scores when pinning probability is high AND we're aimed at the
    # dominant wall. Pure feature-driven, no extra inputs.
    pin = float(features.get("pinning_probability") or 0.0)
    dominant = features.get("dominant_wall") or "neutral"
    if pin >= 0.6 and ((direction == "LONG" and dominant == "call") or
                        (direction == "SHORT" and dominant == "put")):
        pin_penalty = 1.0 - (pin - 0.5)            # 0.6 → ×0.9, 0.9 → ×0.6
        regime_align *= pin_penalty
        flow_align *= pin_penalty

    comps = {
        "probability": p,
        "risk_reward": rr_norm,
        "regime":      regime_align,
        "confluence":  conf_align,
        "flow":        flow_align,
        "liquidity":   liquidity,
    }
    score = round(sum(comps[k] * w for k, w in _WEIGHTS.items()), 4)

    reasoning: List[str] = []

    # MITS Phase 14.A — wide-CI cohort posterior gets a multiplicative
    # haircut. Floor at 0.6 keeps the penalty from zeroing out the
    # whole composite. cohort_ci_width = Wilson upper - lower from the
    # knowledge_graph cell that backed this probability.
    ci_width = float(features.get("cohort_ci_width") or 0.0)
    if ci_width > 0:
        ci_penalty = max(0.6, 1.0 - ci_width * TUNABLES.rank_ci_penalty_coef)
        score = round(score * ci_penalty, 4)
        comps["ci_penalty"] = round(ci_penalty, 3)
        if ci_penalty < 0.9:
            reasoning.append(
                f"wide cohort CI ({ci_width:.2f}) — "
                f"score discounted ×{ci_penalty:.2f}"
            )

    grade = _grade_for(score)

    if p >= 0.7:
        reasoning.append(f"high win probability ({p:.0%})")
    elif p < 0.55:
        reasoning.append(f"weak win probability ({p:.0%})")
    if rr and rr >= 2.5:
        reasoning.append(f"strong R/R {rr:.1f}:1")
    if regime_align >= 0.7:
        reasoning.append(f"regime aligned ({regime_trend})")
    elif regime_align <= 0.35:
        reasoning.append(f"fighting the {regime_trend} regime")
    if confluence and conf_align >= 0.7:
        reasoning.append(f"multi-timeframe confluence ({getattr(confluence, 'direction', '')})")
    if confluence and getattr(confluence, "conflicting_timeframes", []):
        reasoning.append(f"conflicts on {', '.join(confluence.conflicting_timeframes)}")
    if flow_align >= 0.7:
        reasoning.append("institutional flow agrees")
    elif flow_align <= 0.35:
        reasoning.append("flow disagrees")
    if liquidity < 0.6:
        reasoning.append("thin liquidity")
    if pin >= 0.6 and ((direction == "LONG" and dominant == "call") or
                       (direction == "SHORT" and dominant == "put")):
        reasoning.append(f"entering into a {dominant} wall (pin prob {pin:.0%}) — edge dampened")

    explainer = _build_grade_explainer(
        grade=grade, score=score, comps=comps,
        probability=probability, regime=regime, features=features,
    )

    return TradeRank(
        grade=grade, score=score, components=comps,
        reasoning=reasoning, grade_explainer=explainer,
    )


def _build_grade_explainer(
    *,
    grade: str, score: float, comps: Dict[str, float],
    probability: Any, regime: Any, features: Dict[str, Any],
) -> str:
    """Compose a one-paragraph operator-readable explanation of the rank.

    Lead with the grade + score. Then describe in plain English:
      - the cohort evidence backing the probability (N, CI bounds)
      - whether regime alignment helped or hurt
      - whether dealer-pin risk is present and dampened the edge
      - whether the CI was wide enough to discount the score
    """
    p_obj_post = getattr(probability, "probability", None)
    direction = getattr(probability, "direction", "NEUTRAL")
    posterior_pct: Optional[float] = None
    try:
        if p_obj_post is not None:
            posterior_pct = float(p_obj_post) * 100.0
    except Exception:
        posterior_pct = None

    n = features.get("cohort_sample_size") or 0
    ci_lo = features.get("cohort_ci_lower")
    ci_hi = features.get("cohort_ci_upper")
    cohort_post = features.get("cohort_posterior")

    parts: List[str] = [f"This is a {grade} ({score * 100:.0f}/100)"]

    # Cohort evidence — prefer the cohort posterior when present (more
    # direct), otherwise quote the probability engine's blended estimate.
    quoted_post: Optional[float] = None
    if cohort_post is not None:
        try:
            quoted_post = float(cohort_post) * 100.0
        except Exception:
            quoted_post = None
    if quoted_post is None:
        quoted_post = posterior_pct

    if n and ci_lo is not None and ci_hi is not None and quoted_post is not None:
        parts.append(
            f"because the cohort posterior is {quoted_post:.0f}% "
            f"on N={int(n)} with CI [{float(ci_lo) * 100:.0f}%, "
            f"{float(ci_hi) * 100:.0f}%]"
        )
    elif n and quoted_post is not None:
        parts.append(
            f"because the cohort posterior is {quoted_post:.0f}% on N={int(n)} "
            "(no CI available)"
        )
    elif quoted_post is not None:
        parts.append(
            f"with model-blended probability {quoted_post:.0f}% (no cohort match)"
        )
    else:
        parts.append("without a cohort match to anchor the probability")

    # Regime alignment — pulled from the component the ranker already
    # computed, so phrasing always matches the math that produced the grade.
    regime_align = float(comps.get("regime", 0.5))
    regime_trend = getattr(regime, "trend", "unknown")
    if regime_align >= _REGIME_ALIGN_HELP_THRESHOLD:
        parts.append(f"the {regime_trend} regime aligns with the trade")
    elif regime_align <= _REGIME_ALIGN_HURT_THRESHOLD:
        parts.append(f"the {regime_trend} regime is fighting the trade")

    # Dealer-pin risk — only call out when pin probability is HIGH and we're
    # aimed at the dominant wall, mirroring the penalty logic upstream.
    pin = float(features.get("pinning_probability") or 0.0)
    dominant = features.get("dominant_wall") or "neutral"
    if pin >= _PIN_HEADWIND_THRESHOLD and (
        (direction == "LONG" and dominant == "call")
        or (direction == "SHORT" and dominant == "put")
    ):
        parts.append(
            f"a {dominant} wall sits in the path with pin probability "
            f"{pin:.0%} — a known edge eraser"
        )

    # CI penalty — surfaced when the ranker actually applied a discount.
    ci_penalty = comps.get("ci_penalty")
    if ci_penalty is not None and float(ci_penalty) < _CI_PENALTY_NOTABLE_THRESHOLD:
        parts.append(
            f"the cohort CI was wide enough to discount the score by "
            f"x{float(ci_penalty):.2f}"
        )

    return ", ".join(parts) + "."


def build_grade_explainer_for_cohort(
    *,
    posterior: float, sample_size: int,
    ci_lower: Optional[float], ci_upper: Optional[float],
    regime_label: str, pinning_probability: float,
    grade: str, score: float,
    direction: str = "LONG",
    dominant_wall: str = "neutral",
) -> str:
    """Standalone explainer for callers that don't run the full
    rank_trade pipeline (e.g. the per-ticker /analysis route).

    Mirrors the prose that ``_build_grade_explainer`` emits inside
    ``rank_trade`` so the operator sees the same wording everywhere.
    Re-uses ``_build_grade_explainer`` by synthesising the minimal
    ``probability`` / ``regime`` / ``features`` shapes it needs.
    """

    class _P:
        pass

    p = _P()
    p.probability = float(posterior)
    p.direction = direction

    class _R:
        pass

    r = _R()
    r.trend = regime_label
    r.volatility = "normal"

    # Use the upstream component math as the source of truth: we recompute
    # regime alignment so the prose matches what rank_trade would have said
    # given the same inputs.
    if regime_label == "bullish":
        regime_signed = 1.0
    elif regime_label == "bearish":
        regime_signed = -1.0
    else:
        regime_signed = 0.0
    regime_align = _aligned(direction, regime_signed)

    comps: Dict[str, float] = {"regime": regime_align}
    return _build_grade_explainer(
        grade=grade, score=float(score), comps=comps,
        probability=p, regime=r,
        features={
            "cohort_sample_size": int(sample_size),
            "cohort_ci_lower": ci_lower,
            "cohort_ci_upper": ci_upper,
            "cohort_posterior": float(posterior),
            "pinning_probability": float(pinning_probability),
            "dominant_wall": dominant_wall,
        },
    )
