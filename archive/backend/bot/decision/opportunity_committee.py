"""MITS Phase 16.D — Opportunity Committee Lite.

A 3-agent mini-council that reviews every ``OpportunityHypothesis``
emitted by the ``OpportunityBrain`` before it is sized + executed.

The committee fills a gap the Bayesian + cohort + memory layers are
deliberately bypassed for on non-normal regimes: the operator's bet is
that the statistical layer is too cautious during panic/squeeze tape,
so the Brain is allowed to override consensus. But "allowed to override"
must not mean "no second opinion at all" — that drift was producing
0DTE entries with no risk siblings, no precedent check, no devil's
advocate.

The three reviewers cover three orthogonal axes:

  * **Risk** — caps undefined-risk + open exposure + 0DTE-into-panic-VIX.
  * **Analog** — reuses ``retrieve_analogs`` to ask "has this regime
    fingerprint paid off before?". A cohort below 3 analogs is a reject;
    below 8 is weak support.
  * **Devil's Advocate** — adapts the Stage-12.A2 red-team agent to the
    opportunity context, projecting its STANCE_* output onto the
    committee's support/abstain/reject trichotomy.

The blender weights the reviewers' confidence into four axes
(dislocation / historical_precedent / risk / timing), then computes a
composite score in [0, 1]:

  * >= 0.65 → EXECUTE
  * 0.45-0.65 → SIZE_DOWN (size multiplier = max(0.3, composite))
  * < 0.45 → REJECT

Any single hard-reject from Risk or Analog overrides the composite and
forces a REJECT — those reviewers' veto trumps a high blended score.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backend.config import TUNABLES

if TYPE_CHECKING:
    from backend.bot.ai.opportunity_brain import OpportunityHypothesis


STANCE_SUPPORT = "support"
STANCE_ABSTAIN = "abstain"
STANCE_REJECT = "reject"

# Composite thresholds: reviewed against the operator's spec.
_EXECUTE_THRESHOLD = 0.65
_SIZE_DOWN_THRESHOLD = 0.45
_SIZE_DOWN_FLOOR = 0.30        # never size below 30% of base on SIZE_DOWN

# Risk-axis tunables. Cap on concurrent opportunity-brain positions —
# operator's existing knob is ``opportunistic_max_concurrent`` (default 3).
# The committee's own concurrency cap is a tighter ``2`` per the spec
# unless that tunable is overridden.
_DEFAULT_COMMITTEE_CONCURRENT_MAX = 2

# 0DTE + extreme-VIX combo guard.
_VIX_PANIC_GAMMA_THRESHOLD = 35.0

# Undefined-risk directions — long debit options are defined-risk
# (capped at premium), iron condors are defined-risk by construction,
# straddles are defined-risk (debit). Naked short legs would be
# undefined risk; the Brain's schema can emit them but the committee
# treats any leg the gate can't define as undefined.
_DEFINED_RISK_DIRECTIONS = {
    "long_call", "long_put", "long_straddle", "iron_condor",
}


# ── dataclasses ─────────────────────────────────────────────────────────


@dataclass
class OpportunityCommitteeVote:
    agent: str
    stance: str                      # "support" | "abstain" | "reject"
    confidence: float                # 0..1
    supporting_factors: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OpportunityCommitteeResult:
    votes: List[OpportunityCommitteeVote]
    dislocation_score: float         # 0..1 — how much regime + tape deviates from baseline
    historical_precedent_score: float
    risk_score: float                # 1.0 = safe, 0.0 = ruinous
    timing_score: float
    composite_score: float
    recommendation: str              # "EXECUTE" | "SIZE_DOWN" | "REJECT"
    rec_reason: str
    evaluated_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "votes": [v.to_dict() for v in self.votes],
            "dislocation_score": round(float(self.dislocation_score), 4),
            "historical_precedent_score": round(
                float(self.historical_precedent_score), 4),
            "risk_score": round(float(self.risk_score), 4),
            "timing_score": round(float(self.timing_score), 4),
            "composite_score": round(float(self.composite_score), 4),
            "recommendation": self.recommendation,
            "rec_reason": self.rec_reason,
            "evaluated_at": self.evaluated_at,
        }


# ── reviewers ───────────────────────────────────────────────────────────


def agent_opportunity_risk(
    hypothesis: "OpportunityHypothesis",
    context: Dict[str, Any],
) -> OpportunityCommitteeVote:
    """Risk reviewer.

    Hard-rejects when:
      * Hypothesis direction is not in the defined-risk set.
      * Concurrent open opportunistic positions >= committee cap.
      * 0DTE on a hypothesis fired in a panic tape with VIX > 35.

    Supports otherwise; confidence scales with concurrent-open headroom
    so a fresh book on a panic day still carries high support.
    """
    concerns: List[str] = []
    supporting: List[str] = []

    direction = (hypothesis.direction or "").lower()
    if direction not in _DEFINED_RISK_DIRECTIONS:
        concerns.append(
            f"direction '{direction}' is not defined-risk — committee "
            f"rejects undefined-risk legs by default"
        )

    concurrent_open = int(context.get("opportunistic_concurrent_open") or 0)
    concurrent_max = int(getattr(
        TUNABLES, "opportunity_committee_concurrent_max",
        _DEFAULT_COMMITTEE_CONCURRENT_MAX,
    ))
    if concurrent_open >= concurrent_max:
        concerns.append(
            f"concurrent_open={concurrent_open} >= committee cap "
            f"{concurrent_max}"
        )

    dte_bucket = (hypothesis.dte_bucket or "").lower()
    snap = context.get("snapshot") or {}
    vix = snap.get("vix")
    try:
        vix_val = float(vix) if vix is not None else None
    except (TypeError, ValueError):
        vix_val = None
    if dte_bucket == "0d" and vix_val is not None and vix_val > _VIX_PANIC_GAMMA_THRESHOLD:
        concerns.append(
            f"0DTE proposal with VIX={vix_val:.1f} > "
            f"{_VIX_PANIC_GAMMA_THRESHOLD:.0f} — gamma risk on day-of-expiry"
        )

    if concerns:
        # Confidence on reject = how strongly we want the trade off the
        # table; multiple concerns push higher.
        conf = min(0.95, 0.55 + 0.15 * len(concerns))
        return OpportunityCommitteeVote(
            agent="opportunity_risk", stance=STANCE_REJECT,
            confidence=round(conf, 4),
            supporting_factors=supporting, concerns=concerns,
        )

    # Margin-of-safety headroom: 0 open → confidence 0.85, at cap-1 → 0.60.
    headroom = max(0, concurrent_max - concurrent_open)
    if concurrent_max <= 0:
        ms = 0.5
    else:
        ms = headroom / float(concurrent_max)
    confidence = round(0.55 + 0.30 * ms, 4)
    supporting.append(
        f"defined-risk {direction}; concurrent_open={concurrent_open}/"
        f"{concurrent_max}"
    )
    if vix_val is not None:
        supporting.append(f"vix={vix_val:.1f}")
    return OpportunityCommitteeVote(
        agent="opportunity_risk", stance=STANCE_SUPPORT,
        confidence=confidence,
        supporting_factors=supporting, concerns=concerns,
    )


def agent_opportunity_analog(
    hypothesis: "OpportunityHypothesis",
    context: Dict[str, Any],
) -> OpportunityCommitteeVote:
    """Analog reviewer.

    Calls ``retrieve_analogs`` with a transient RegimeVector built from
    the hypothesis' ``regime_state``. Scoring:

      * cohort_size >= 8 → strong support
      * 3 <= cohort_size < 8 → weak support
      * cohort_size < 3 → reject (not enough precedent)

    Mean realized return on the cohort tilts confidence; a positive mean
    boosts support, a negative mean drags confidence down. The
    ``sector_fallback_used`` flag adds a concern but does not flip the
    stance — the operator's spec is "soft penalty".
    """
    from backend.bot.corpus.analog_retrieval import retrieve_analogs
    from backend.bot.regime.vector import RegimeDimension, RegimeVector

    ticker = (hypothesis.ticker or "SPY").upper()
    # OpportunityHypothesis.regime_state is a dict per opportunity_brain schema —
    # extract the trend dimension. Fall back to the raw value if it's a string.
    rs = hypothesis.regime_state
    if isinstance(rs, dict):
        regime_label = str(rs.get("trend") or "unknown").lower()
    else:
        regime_label = str(rs or "unknown").lower()
    dte_bucket = (hypothesis.dte_bucket or "").lower()
    # Map dte_bucket → analog horizon.
    if dte_bucket == "0d":
        horizon = "1d"
    elif dte_bucket == "1d":
        horizon = "1d"
    elif dte_bucket in ("3-5d", "5d"):
        horizon = "5d"
    elif dte_bucket in ("7-14d", "14d"):
        horizon = "20d"
    else:
        horizon = "1d"

    transient_rv = RegimeVector(
        ticker=ticker, as_of=datetime.utcnow(),
        trend=RegimeDimension(
            value=regime_label, freshness_seconds=0.0,
            source="regime", health="green",
        ),
        volatility_state=RegimeDimension(
            value="elevated", freshness_seconds=0.0,
            source="regime", health="green",
        ),
        iv_rank=RegimeDimension(
            value=None, freshness_seconds=None,
            source="iv_regime", health="yellow",
        ),
        iv_regime=RegimeDimension(
            value=None, freshness_seconds=None,
            source="iv_regime", health="yellow",
        ),
        intraday_regime=RegimeDimension(
            value=regime_label, freshness_seconds=0.0,
            source="intraday", health="green",
        ),
        gamma_state=RegimeDimension(
            value=None, freshness_seconds=None,
            source="gex", health="yellow",
        ),
        macro_regime=RegimeDimension(
            value=None, freshness_seconds=None,
            source="macro", health="yellow",
        ),
        health="yellow",
    )

    cluster = retrieve_analogs(
        ticker=ticker, regime_vector=transient_rv,
        pattern=regime_label, horizon=horizon, k=50,
        sector_fallback=True,
    )

    concerns: List[str] = []
    supporting: List[str] = []
    if cluster.sector_fallback_used:
        concerns.append(
            "sector fallback used — same-ticker precedent was thin"
        )

    n = int(cluster.cohort_size)
    mean_pct = float(cluster.outcome_distribution.get("mean") or 0.0)

    if n == 0:
        # Distinguishes "pgvector unavailable / embedding failed" from
        # "pgvector returned a too-small cohort". Stage-20a's third state
        # — abstain on absent evidence — applies here. A reject would
        # silently kill every brain hypothesis whenever the vector store
        # is down.
        concerns.append("no analogs available (vector store offline?)")
        return OpportunityCommitteeVote(
            agent="opportunity_analog", stance=STANCE_ABSTAIN,
            confidence=0.30,
            supporting_factors=supporting, concerns=concerns,
        )
    if n < 3:
        concerns.append(
            f"cohort_size={n} below minimum precedent threshold (3)"
        )
        return OpportunityCommitteeVote(
            agent="opportunity_analog", stance=STANCE_REJECT,
            confidence=0.70,
            supporting_factors=supporting, concerns=concerns,
        )

    if n >= 8:
        base = 0.80
        supporting.append(f"strong precedent: cohort_size={n}")
    else:
        base = 0.55
        supporting.append(f"weak precedent: cohort_size={n}")

    # Tilt confidence by the cohort mean. +3% mean → +0.10, -3% → -0.10.
    tilt = max(-0.10, min(0.10, mean_pct / 30.0))
    if mean_pct > 0:
        supporting.append(f"mean_return={mean_pct:.2f}% supports thesis")
    elif mean_pct < 0:
        concerns.append(f"mean_return={mean_pct:.2f}% drags confidence")
    confidence = max(0.20, min(0.95, base + tilt))

    return OpportunityCommitteeVote(
        agent="opportunity_analog", stance=STANCE_SUPPORT,
        confidence=round(confidence, 4),
        supporting_factors=supporting, concerns=concerns,
    )


def agent_opportunity_devils_advocate(
    hypothesis: "OpportunityHypothesis",
    context: Dict[str, Any],
) -> OpportunityCommitteeVote:
    """Devil's-advocate reviewer — adapts ``agent_devils_advocate``.

    Builds the standard agents-context shape from the hypothesis +
    committee context, calls the existing red-team agent, then projects
    its AgentVote stance onto the committee trichotomy:

        STANCE_SELL (against a long hypothesis) → "reject"
        STANCE_BUY (against a short hypothesis) → "reject"
        STANCE_ABSTAIN with confidence >= 0.65 → "reject"
        STANCE_ABSTAIN with lower confidence  → "abstain"
        STANCE_HOLD / STANCE_BUY / STANCE_SELL aligned with direction
                                              → "support"
    """
    from backend.bot.agents import (
        STANCE_ABSTAIN as _AGENT_ABSTAIN,
        STANCE_BUY as _AGENT_BUY,
        STANCE_HOLD as _AGENT_HOLD,
        STANCE_SELL as _AGENT_SELL,
        agent_devils_advocate,
    )

    direction = (hypothesis.direction or "").lower()
    if direction in ("long_call", "long_straddle"):
        action = "BUY_CALL"
        side = "long"
    elif direction == "long_put":
        action = "BUY_PUT"
        side = "short"   # long_put expresses short-direction edge
    elif direction == "iron_condor":
        action = "BUY"
        side = "neutral"
    else:
        action = "BUY"
        side = "neutral"

    snap = context.get("snapshot") or {}
    # OpportunityHypothesis.regime_state is a dict per opportunity_brain schema.
    _rs = hypothesis.regime_state
    if isinstance(_rs, dict):
        regime_state = str(_rs.get("trend") or _rs.get("intraday") or "unknown").lower()
    else:
        regime_state = str(_rs or "unknown").lower()
    sv = context.get("simulator_verdict") or {}
    # Translate intraday regime into the devils-advocate trend slot.
    if regime_state in ("panic", "capitulation", "trending_down"):
        trend = "bearish"
    elif regime_state in ("squeeze", "trending_up"):
        trend = "bullish"
    else:
        trend = "choppy"

    inner_ctx: Dict[str, Any] = {
        "action": action,
        "ticker": hypothesis.ticker,
        "snapshot": snap,
        "analytics": {
            "regime": {"trend": trend},
            "features": {
                "iv_rank": snap.get("iv_rank"),
                "vix": snap.get("vix"),
                "earnings_days": snap.get("earnings_days"),
            },
        },
        "features": {
            "iv_rank": snap.get("iv_rank"),
            "vix": snap.get("vix"),
            "earnings_days": snap.get("earnings_days"),
        },
        "portfolio_risk": {
            "drawdown_pct": (
                context.get("account").drawdown_pct
                if context.get("account") is not None
                and hasattr(context.get("account"), "drawdown_pct")
                else 0.0
            ),
        },
        "simulator_verdict": sv,
    }

    inner = agent_devils_advocate(inner_ctx)
    concerns = []
    supporting = []
    # The red-team's reasoning string is the operator-facing line.
    if inner.stance == _AGENT_ABSTAIN:
        if inner.confidence >= 0.65:
            concerns.append(inner.reasoning)
            return OpportunityCommitteeVote(
                agent="opportunity_devils_advocate",
                stance=STANCE_REJECT,
                confidence=round(inner.confidence, 4),
                supporting_factors=supporting, concerns=concerns,
            )
        concerns.append(inner.reasoning)
        return OpportunityCommitteeVote(
            agent="opportunity_devils_advocate",
            stance=STANCE_ABSTAIN,
            confidence=round(inner.confidence, 4),
            supporting_factors=supporting, concerns=concerns,
        )

    # Directional stance — supportive when aligned with hypothesis side.
    aligned = (
        (side == "long" and inner.stance == _AGENT_BUY)
        or (side == "short" and inner.stance == _AGENT_SELL)
        or (side == "neutral" and inner.stance == _AGENT_HOLD)
        or inner.stance == _AGENT_HOLD
    )
    if aligned:
        supporting.append(inner.reasoning)
        return OpportunityCommitteeVote(
            agent="opportunity_devils_advocate",
            stance=STANCE_SUPPORT,
            confidence=round(max(0.40, inner.confidence), 4),
            supporting_factors=supporting, concerns=concerns,
        )
    # Inner agent is voting AGAINST the hypothesis direction → reject.
    concerns.append(inner.reasoning)
    return OpportunityCommitteeVote(
        agent="opportunity_devils_advocate",
        stance=STANCE_REJECT,
        confidence=round(max(0.50, inner.confidence), 4),
        supporting_factors=supporting, concerns=concerns,
    )


# ── blender ─────────────────────────────────────────────────────────────


def _axis_dislocation(devils: OpportunityCommitteeVote) -> float:
    """Devil's-advocate stance → dislocation axis. Support means no
    contrarian flags fired so the regime IS dislocated as the Brain
    claims; reject means the red-team disagrees with the dislocation
    read; abstain is in-between."""
    if devils.stance == STANCE_SUPPORT:
        return 1.0
    if devils.stance == STANCE_REJECT:
        return 0.0
    return 0.30


def _axis_precedent(analog: OpportunityCommitteeVote) -> float:
    """Analog stance + confidence → precedent axis."""
    if analog.stance == STANCE_SUPPORT:
        return float(analog.confidence)
    if analog.stance == STANCE_REJECT:
        return 0.20
    return 0.40


def _axis_risk(risk: OpportunityCommitteeVote) -> float:
    """Risk stance + confidence → risk axis (1.0 = safe)."""
    if risk.stance == STANCE_SUPPORT:
        return float(risk.confidence)
    if risk.stance == STANCE_REJECT:
        return 0.20
    return 0.50


def _axis_timing(hypothesis: "OpportunityHypothesis") -> float:
    """Conviction proxies timing — the Brain's confidence in the
    immediacy of the setup. Already in [0, 1]."""
    return float(hypothesis.conviction or 0.5)


def review_opportunity(
    hypothesis: "OpportunityHypothesis",
    context: Dict[str, Any],
) -> OpportunityCommitteeResult:
    """Drive the 3 reviewers and blend their outputs.

    Hard reject from Risk or Analog overrides the composite — those two
    reviewers' veto is binding. Otherwise the composite chooses between
    EXECUTE / SIZE_DOWN / REJECT off the operator's thresholds.
    """
    risk = agent_opportunity_risk(hypothesis, context)
    analog = agent_opportunity_analog(hypothesis, context)
    devils = agent_opportunity_devils_advocate(hypothesis, context)

    dislocation = _axis_dislocation(devils)
    precedent = _axis_precedent(analog)
    risk_axis = _axis_risk(risk)
    timing = _axis_timing(hypothesis)

    composite = (
        0.25 * dislocation
        + 0.25 * precedent
        + 0.30 * risk_axis
        + 0.20 * timing
    )

    # Hard-reject override: a single binding veto trumps composite.
    hard_rejects = [v for v in (risk, analog) if v.stance == STANCE_REJECT]
    if hard_rejects:
        reasons = []
        for v in hard_rejects:
            if v.concerns:
                reasons.append(f"{v.agent}: {v.concerns[0]}")
        rec_reason = "; ".join(reasons) or "hard reject from risk/analog reviewer"
        recommendation = "REJECT"
    elif composite >= _EXECUTE_THRESHOLD:
        recommendation = "EXECUTE"
        rec_reason = f"composite={composite:.2f} >= {_EXECUTE_THRESHOLD:.2f}"
    elif composite >= _SIZE_DOWN_THRESHOLD:
        recommendation = "SIZE_DOWN"
        rec_reason = (
            f"composite={composite:.2f} in "
            f"[{_SIZE_DOWN_THRESHOLD:.2f}, {_EXECUTE_THRESHOLD:.2f})"
        )
    else:
        recommendation = "REJECT"
        rec_reason = f"composite={composite:.2f} < {_SIZE_DOWN_THRESHOLD:.2f}"

    return OpportunityCommitteeResult(
        votes=[risk, analog, devils],
        dislocation_score=round(dislocation, 4),
        historical_precedent_score=round(precedent, 4),
        risk_score=round(risk_axis, 4),
        timing_score=round(timing, 4),
        composite_score=round(composite, 4),
        recommendation=recommendation,
        rec_reason=rec_reason,
    )


__all__ = [
    "OpportunityCommitteeVote",
    "OpportunityCommitteeResult",
    "STANCE_SUPPORT",
    "STANCE_ABSTAIN",
    "STANCE_REJECT",
    "agent_opportunity_risk",
    "agent_opportunity_analog",
    "agent_opportunity_devils_advocate",
    "review_opportunity",
]
