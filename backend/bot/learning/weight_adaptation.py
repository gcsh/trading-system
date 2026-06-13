"""MITS Phase 18.D — Online Agent Weight Adaptation (Advisory).

Compute a rolling adaptive multiplier per council agent from the 18.A
calibration scorecard. Down-weight agents whose Brier score is high or
whose hit-rate is below 0.5; up-weight agents that calibrate cleanly.

THE OUTPUT IS ADVISORY ONLY. By default the engine ignores this module
entirely. Two flags gate behavior:

  * ``TUNABLES.adaptive_weights_advisory_enabled`` — when True, the
    nightly scheduler job persists one ``agent_weight_history`` row per
    agent so the operator can review the proposals.

  * ``TUNABLES.adaptive_weights_apply_enabled`` — when True AND there
    is at least one row per agent in ``agent_weight_history``, the
    engine reads the latest persisted weight for each agent at
    consensus time. Until this flag is on, the engine uses the legacy
    per-vote weights emitted by each agent module.

Bayesian shrinkage keeps the multiplier near 1.0 when the closed-trade
sample is thin:

    target_multiplier = 1 + ((-2*brier + 0.5) * (n / (n + 50)))

    n=0  -> multiplier = 1.0 (no change)
    n=50 -> half the brier-derived delta
    n→∞  -> full brier-derived delta

Hard clamps then enforce:

    weight_proposed = clip(base * adaptive_multiplier,
                            0.5 * base, 1.5 * base)

so a noisy calibration can never pull an agent below 0.5x base or push
above 1.5x. ``min_n`` is the hard floor below which we refuse to adapt
at all (multiplier := 1.0, confidence_level := "insufficient_data").

Replay invariant: when the engine replays a past decision via
``backend.bot.decision.replay.replay_consensus_from_provenance``, it
reconstructs votes from ``agent_outputs_json`` — which carries the
PERSISTED vote weight, not the live adaptive weight. So flipping
adaptive_weights_apply_enabled on does NOT change replay drift on
past decisions; only future cycles see the new weights.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, desc, select

from backend.bot.learning.attribution import (
    DEFAULT_WINDOW_DAYS,
    KNOWN_AGENTS,
    compute_agent_calibration,
    compute_axis_calibration,
)
from backend.config import TUNABLES
from backend.db import session_scope


logger = logging.getLogger(__name__)


# ── Public knobs (config-driven; no magic numbers in the math) ────────


# MITS Phase 18-FU Stream D (Gap 6) — TTL on the per-cycle weight
# application log. The log grows ~1 row per consensus cycle when the
# apply flag is on (~96/day at 10-min cycles, 16h sessions). 30 days
# is the rolling forensic window — old enough to explain "last
# month's consensus drift", short enough that the table stays small.
# The nightly prune (``prune_weight_application_log``) at 22:50 ET
# wipes rows older than this window.
WEIGHT_APPLICATION_LOG_TTL_DAYS = 30


DEFAULT_MIN_N = 30
DEFAULT_PRIOR_N = 50          # Bayesian shrinkage prior strength
DEFAULT_MAX_BOOST = 1.5       # clamp upper bound (× base_weight)
DEFAULT_MAX_PENALTY = 0.5     # clamp lower bound (× base_weight)
# Confidence-level thresholds — drive the operator-facing label that
# accompanies every proposal.
CONFIDENCE_HIGH_N = 200
CONFIDENCE_MEDIUM_N = 80
BRIER_GOOD_THRESHOLD = 0.20
BRIER_BAD_THRESHOLD = 0.30
HIT_RATE_GOOD_THRESHOLD = 0.55
HIT_RATE_BAD_THRESHOLD = 0.45


# ── Canonical base weights per agent ──────────────────────────────────
#
# AGENT_FUNCS at backend/bot/agents/__init__.py:1669-1680 carries
# ``(name, role, fn)`` tuples — there is no ``base_weight`` field in the
# registry. Each agent emits ``AgentVote.weight`` dynamically (0.3-1.2
# depending on conviction). For Phase 18.D the canonical "base" is the
# AgentVote dataclass default (1.0). The adaptive multiplier scales that
# 1.0 reference, then the engine multiplies the agent's per-vote
# conviction weight by the adaptive scalar at consumption time.
#
# Hard-coded here so the advisor always lists EVERY agent — even when
# the calibration scorecard returns no rows for one — instead of
# silently dropping it.
AGENT_BASE_WEIGHTS: Dict[str, float] = {
    "market": 1.0,
    "microstructure": 1.0,
    "macro": 1.0,
    "portfolio_risk": 1.0,
    "mechanical_trend": 1.0,
    "thesis_health": 1.0,
    "simulator": 1.0,
    "devils_advocate": 1.0,
}


# ── Dataclasses (round-trippable) ─────────────────────────────────────


@dataclass
class AgentWeightProposal:
    agent: str
    base_weight: float
    current_weight: float              # what engine is actually using right now
    weight_proposed: float
    adaptive_multiplier: float         # inner multiplier BEFORE clamping
    n_closed: int
    hit_rate: Optional[float] = None
    brier_score: Optional[float] = None
    ece: Optional[float] = None
    discrimination: Optional[float] = None
    confidence_level: str = "insufficient_data"
    rationale: str = ""
    computed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WeightAdaptationReport:
    proposals: List[AgentWeightProposal] = field(default_factory=list)
    window_days: int = DEFAULT_WINDOW_DAYS
    min_n: int = DEFAULT_MIN_N
    advisory_enabled: bool = False
    apply_enabled: bool = False
    computed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposals": [p.to_dict() for p in self.proposals],
            "window_days": self.window_days,
            "min_n": self.min_n,
            "advisory_enabled": self.advisory_enabled,
            "apply_enabled": self.apply_enabled,
            "computed_at": self.computed_at,
        }


# ── Math (pure functions; no DB access) ───────────────────────────────


def _confidence_level(n_closed: int, brier: Optional[float],
                       min_n: int) -> str:
    """Operator-facing label.

    * ``insufficient_data`` — n_closed < min_n; we make NO adjustment.
    * ``low`` — n_closed >= min_n but brier is None (no directional bets).
    * ``medium`` — n_closed >= CONFIDENCE_MEDIUM_N.
    * ``high`` — n_closed >= CONFIDENCE_HIGH_N.

    The label drives UI emphasis only; the math is identical across
    levels (the clamps + shrinkage are the safety belt).
    """
    if n_closed < min_n:
        return "insufficient_data"
    if brier is None:
        return "low"
    if n_closed >= CONFIDENCE_HIGH_N:
        return "high"
    if n_closed >= CONFIDENCE_MEDIUM_N:
        return "medium"
    return "low"


def _adaptive_multiplier(n_closed: int, brier: Optional[float],
                          min_n: int = DEFAULT_MIN_N,
                          prior_n: int = DEFAULT_PRIOR_N) -> float:
    """Bayesian-shrunk multiplier.

        target_multiplier = 1 + ((-2*brier + 0.5) * (n / (n + prior_n)))

    Below ``min_n`` we return exactly 1.0 (no adaptation). Brier=None
    (agent never made a directional bet in the window) also returns 1.0.

    Edge cases:
      * brier=0.25 (random-guess baseline) → delta = 0 → multiplier = 1.0
      * brier=0.0 + n=200 → delta = +0.5, shrink = 200/250 = 0.8
        → multiplier ≈ 1.4
      * brier=0.5 + n=200 → delta = -0.5, shrink = 0.8 → multiplier ≈ 0.6
      * brier=0.0 + n=10 (below floor) → multiplier = 1.0 (no change)
    """
    if n_closed < min_n:
        return 1.0
    if brier is None:
        return 1.0
    shrink = float(n_closed) / float(n_closed + prior_n)
    delta = (-2.0 * float(brier)) + 0.5     # signed brier-derived edge
    return 1.0 + (delta * shrink)


def _clamp_weight(base: float, multiplier: float,
                    max_boost: float = DEFAULT_MAX_BOOST,
                    max_penalty: float = DEFAULT_MAX_PENALTY) -> float:
    """Apply the [0.5*base, 1.5*base] safety clamp."""
    raw = float(base) * float(multiplier)
    lo = float(base) * float(max_penalty)
    hi = float(base) * float(max_boost)
    return max(lo, min(hi, raw))


def _rationale(*, agent: str, n: int, brier: Optional[float],
                hit_rate: Optional[float], multiplier: float,
                base: float, proposed: float,
                confidence_level: str, min_n: int) -> str:
    """Plain-English string for the operator-review surface."""
    if confidence_level == "insufficient_data":
        return (
            f"{agent}: only {n} closed directional votes in window "
            f"(need {min_n}). Keeping multiplier=1.0 — no adaptation."
        )
    parts: List[str] = [
        f"{agent}: n_closed={n}",
    ]
    if hit_rate is not None:
        parts.append(f"hit_rate={hit_rate:.3f}")
    if brier is not None:
        parts.append(f"brier={brier:.3f}")
    parts.append(
        f"multiplier={multiplier:.3f} (clamped) → "
        f"weight {base:.3f} → {proposed:.3f}."
    )
    # Direction summary so the operator can read the proposal at a glance.
    if brier is not None:
        if brier < BRIER_GOOD_THRESHOLD and (
            hit_rate is not None and hit_rate >= HIT_RATE_GOOD_THRESHOLD
        ):
            parts.append("Calibration is GOOD — up-weight recommended.")
        elif brier > BRIER_BAD_THRESHOLD or (
            hit_rate is not None and hit_rate <= HIT_RATE_BAD_THRESHOLD
        ):
            parts.append("Calibration is WEAK — down-weight recommended.")
        else:
            parts.append("Calibration is NEUTRAL — minor adjustment only.")
    parts.append(f"confidence_level={confidence_level}.")
    return " ".join(parts)


# ── Public API ────────────────────────────────────────────────────────


def compute_weight_proposals(
    *, window_days: int = DEFAULT_WINDOW_DAYS,
    min_n: int = DEFAULT_MIN_N,
    agent_calibrations: Optional[List[Any]] = None,
    axis_calibrations: Optional[List[Any]] = None,
) -> WeightAdaptationReport:
    """Read 18.A calibration scorecards and derive per-agent weight
    proposals. Pure read; no DB writes.

    ``agent_calibrations`` / ``axis_calibrations`` are injected by tests
    so the math can be exercised without touching the live ledger. In
    production they default to the 18.A live computation.
    """
    if agent_calibrations is None:
        agent_calibrations = compute_agent_calibration(
            window_days=window_days, min_n=min_n,
        )
    if axis_calibrations is None:
        try:
            axis_calibrations = compute_axis_calibration(
                window_days=window_days,
            )
        except Exception:
            logger.debug("axis_calibration load failed", exc_info=True)
            axis_calibrations = []

    # Index the axis discrimination by axis name so we can attach the
    # per-agent discrimination signal where it exists (market_structure
    # → market agent, options → microstructure, etc).
    axis_discrim: Dict[str, Optional[float]] = {}
    for ax in axis_calibrations or []:
        name = getattr(ax, "axis", None)
        if name:
            axis_discrim[name] = getattr(ax, "discrimination", None)

    # Map each council agent to the axis whose discrimination best
    # reflects its votes (mirrors _AGENT_AXIS_MAP in agents/__init__.py).
    agent_axis_link: Dict[str, str] = {
        "market": "market_structure",
        "macro": "macro",
        "microstructure": "options",
        "mechanical_trend": "technical",
        "simulator": "simulator",
    }

    # Index agent_calibrations by agent name for lookup.
    cal_index: Dict[str, Any] = {}
    for c in agent_calibrations or []:
        name = getattr(c, "agent", None)
        if name:
            cal_index[name] = c

    computed_at = datetime.utcnow().isoformat()
    current_weights = get_current_weights()
    proposals: List[AgentWeightProposal] = []
    for agent in KNOWN_AGENTS:
        base = float(AGENT_BASE_WEIGHTS.get(agent, 1.0))
        current = float(current_weights.get(agent, base))
        cal = cal_index.get(agent)
        n_closed = int(getattr(cal, "n_closed", 0) or 0) if cal else 0
        hit_rate = getattr(cal, "hit_rate", None) if cal else None
        brier = getattr(cal, "brier_score", None) if cal else None
        ece_val = getattr(cal, "ece", None) if cal else None
        disc = axis_discrim.get(agent_axis_link.get(agent, ""), None)

        multiplier = _adaptive_multiplier(
            n_closed=n_closed, brier=brier, min_n=min_n,
        )
        proposed = _clamp_weight(base, multiplier)
        confidence_level = _confidence_level(
            n_closed=n_closed, brier=brier, min_n=min_n,
        )
        rationale = _rationale(
            agent=agent, n=n_closed, brier=brier, hit_rate=hit_rate,
            multiplier=multiplier, base=base, proposed=proposed,
            confidence_level=confidence_level, min_n=min_n,
        )

        proposals.append(AgentWeightProposal(
            agent=agent,
            base_weight=round(base, 6),
            current_weight=round(current, 6),
            weight_proposed=round(proposed, 6),
            adaptive_multiplier=round(multiplier, 6),
            n_closed=n_closed,
            hit_rate=(
                round(float(hit_rate), 4) if hit_rate is not None else None
            ),
            brier_score=(
                round(float(brier), 4) if brier is not None else None
            ),
            ece=(round(float(ece_val), 4) if ece_val is not None else None),
            discrimination=(
                round(float(disc), 4) if disc is not None else None
            ),
            confidence_level=confidence_level,
            rationale=rationale,
            computed_at=computed_at,
        ))

    return WeightAdaptationReport(
        proposals=proposals,
        window_days=window_days,
        min_n=min_n,
        advisory_enabled=bool(getattr(
            TUNABLES, "adaptive_weights_advisory_enabled", False,
        )),
        apply_enabled=bool(getattr(
            TUNABLES, "adaptive_weights_apply_enabled", False,
        )),
        computed_at=computed_at,
    )


def get_current_weights(
    *,
    log_application: bool = False,
    cycle_id: Optional[str] = None,
    decision_provenance_id: Optional[int] = None,
    composite_quality_at_apply: Optional[float] = None,
) -> Dict[str, float]:
    """Return the weight the engine is actually using right now.

    Logic:
      * If ``TUNABLES.adaptive_weights_apply_enabled`` is False (default),
        return ``AGENT_BASE_WEIGHTS`` verbatim — the engine ignores
        ``agent_weight_history`` entirely. This is the safe default.
      * Else, return the LATEST persisted ``weight_active`` per agent
        from ``agent_weight_history``. Agents with no row in the table
        fall back to ``AGENT_BASE_WEIGHTS`` (so a never-seen agent
        defaults to its canonical base, not zero).

    Pure read; never raises (DB failures fall back to base weights).

    MITS Phase 18-FU Stream D (Gap 6) — when ``log_application`` is True
    AND the function returns an adaptive set (apply_enabled on +
    history rows exist), ONE row is written to ``weight_application_log``
    capturing ``cycle_id``, the latest ``agent_weight_history_id`` consulted,
    the JSON snapshot of weights consumed, and the operator-provided
    ``decision_provenance_id`` / ``composite_quality_at_apply`` context.

    Callers that are NOT engine cycles (e.g. the advisor's
    ``compute_weight_proposals`` invocation) leave ``log_application`` at
    its False default so we never log spurious rows. The log row writer
    failures are swallowed (logged at debug) — the weight read is the
    contract, the log is observability.
    """
    base = dict(AGENT_BASE_WEIGHTS)
    if not bool(getattr(
        TUNABLES, "adaptive_weights_apply_enabled", False,
    )):
        return base
    # Local import keeps the module side-effect-free until the engine
    # actually opts in to apply.
    try:
        from backend.models.agent_weight_history import AgentWeightHistory
        with session_scope() as s:
            # Pull the most recent row per agent — small table, simple
            # python aggregation is fine.
            rows = s.execute(
                select(AgentWeightHistory)
                .where(AgentWeightHistory.agent.in_(list(KNOWN_AGENTS)))
                .order_by(desc(AgentWeightHistory.computed_at))
            ).scalars().all()
            seen: Dict[str, float] = {}
            # ``source_history_id`` is the FIRST row encountered in the
            # desc-by-computed_at scan: that's the latest batch's head.
            # We use it as the audit pointer back to the persisted
            # history row that drove this cycle's apply.
            source_history_id: Optional[int] = None
            for row in rows:
                if source_history_id is None:
                    try:
                        source_history_id = int(row.id)
                    except (TypeError, ValueError):
                        source_history_id = None
                if row.agent in seen:
                    continue
                try:
                    seen[row.agent] = float(row.weight_active)
                except (TypeError, ValueError):
                    continue
            # Merge: base provides the fallback for any agent missing
            # from the history table entirely.
            out = dict(base)
            out.update(seen)
        # Side-effect log (Gap 6). Only fires when an adaptive set was
        # actually returned (``seen`` is non-empty) AND the caller is a
        # real cycle (``log_application=True``). The advisor's read does
        # NOT log — it just queries the live weights to fill the
        # ``current_weight`` field on each proposal.
        if log_application and seen:
            _write_weight_application_log(
                cycle_id=cycle_id,
                agent_weight_history_id=source_history_id,
                weight_set=out,
                decision_provenance_id=decision_provenance_id,
                composite_quality_at_apply=composite_quality_at_apply,
            )
        return out
    except Exception:
        logger.debug("get_current_weights failed; falling back to base",
                       exc_info=True)
        return base


# ── Gap 6: per-cycle weight application log helpers ──────────────────


def _write_weight_application_log(
    *,
    cycle_id: Optional[str],
    agent_weight_history_id: Optional[int],
    weight_set: Dict[str, float],
    decision_provenance_id: Optional[int],
    composite_quality_at_apply: Optional[float],
) -> Optional[int]:
    """Append one ``weight_application_log`` row capturing the cycle
    context + the exact ``{agent: weight}`` map the engine consumed.

    Returns the new row's id on success, None on failure. Never raises
    — observability writes must not topple a real cycle.
    """
    try:
        from backend.models.weight_application_log import (
            WeightApplicationLog,
        )
        with session_scope() as s:
            row = WeightApplicationLog(
                applied_at=datetime.utcnow(),
                cycle_id=str(cycle_id) if cycle_id else None,
                agent_weight_history_id=(
                    int(agent_weight_history_id)
                    if agent_weight_history_id is not None else None
                ),
                weight_set_json=json.dumps(
                    {k: float(v) for k, v in weight_set.items()},
                    default=str,
                ),
                decision_provenance_id=(
                    int(decision_provenance_id)
                    if decision_provenance_id is not None else None
                ),
                composite_quality_at_apply=(
                    float(composite_quality_at_apply)
                    if composite_quality_at_apply is not None else None
                ),
            )
            s.add(row)
            s.flush()
            return int(row.id)
    except Exception:
        logger.debug(
            "weight_application_log write failed", exc_info=True,
        )
        return None


def apply_weights_for_cycle(
    cycle_id: Optional[str],
    *,
    decision_provenance_id: Optional[int] = None,
    composite_quality_at_apply: Optional[float] = None,
) -> Dict[str, float]:
    """Engine-facing helper: equivalent to ``get_current_weights()`` but
    with ``log_application=True``. Use from inside the consensus loop
    when 18.D's apply flag is on so the per-cycle forensic trail gets
    written.

    The plain ``get_current_weights()`` remains available for the
    advisor's read path (where logging would be spurious). Splitting
    the two keeps the log writer hidden behind an explicit signal of
    intent.
    """
    return get_current_weights(
        log_application=True,
        cycle_id=cycle_id,
        decision_provenance_id=decision_provenance_id,
        composite_quality_at_apply=composite_quality_at_apply,
    )


def prune_weight_application_log(
    *, ttl_days: int = WEIGHT_APPLICATION_LOG_TTL_DAYS,
) -> int:
    """Delete weight_application_log rows older than ``ttl_days``.

    Returns the number of rows deleted. Designed to be called nightly
    from the scheduler so the table stays bounded at ~96 rows/day ×
    ttl_days. Never raises (deletion failures are logged).
    """
    if ttl_days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=int(ttl_days))
    try:
        from backend.models.weight_application_log import (
            WeightApplicationLog,
        )
        with session_scope() as s:
            result = s.execute(
                delete(WeightApplicationLog).where(
                    WeightApplicationLog.applied_at < cutoff,
                )
            )
            deleted = int(result.rowcount or 0)
            s.flush()
            return deleted
    except Exception:
        logger.debug(
            "prune_weight_application_log failed", exc_info=True,
        )
        return 0


def latest_weight_application_rows(
    *, limit: int = 100, agent: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the most recent ``weight_application_log`` rows, optionally
    filtered to those whose persisted ``weight_set_json`` mentions
    ``agent`` (cheap substring match — the JSON is small).

    Used by the ``/learning/observability/weight-applications`` route.
    Pure read; never raises (returns empty list on DB failure).
    """
    try:
        from backend.models.weight_application_log import (
            WeightApplicationLog,
        )
        with session_scope() as s:
            q = select(WeightApplicationLog).order_by(
                desc(WeightApplicationLog.applied_at)
            ).limit(int(max(1, min(500, limit))))
            rows = s.execute(q).scalars().all()
            out: List[Dict[str, Any]] = []
            needle = f'"{agent}"' if agent else None
            for r in rows:
                d = r.to_dict()
                if needle is not None:
                    blob = d.get("weight_set_json") or ""
                    if needle not in blob:
                        continue
                out.append(d)
            return out
    except Exception:
        logger.debug(
            "latest_weight_application_rows failed", exc_info=True,
        )
        return []


def persist_weight_proposals(report: WeightAdaptationReport) -> int:
    """Append one row per proposal to ``agent_weight_history`` and
    return the number of rows written. Append-only — never updates
    existing rows.

    ``weight_active`` mirrors ``weight_proposed`` so the engine has a
    single column to read when ``apply_enabled`` is True. (We don't
    track "approved but not yet applied" separately for 18.D — the
    advisor's proposal IS what would activate. 18.E will layer
    operator-review state on top via ``operator_approved``.)
    """
    if not report or not report.proposals:
        return 0
    from backend.models.agent_weight_history import AgentWeightHistory

    # All rows in this batch share the SAME computed_at so the
    # latest_weight_rows scope-by-MAX(computed_at) query returns the
    # full batch atomically. We use a single timestamp derived once
    # at persist time (not the report.computed_at string) so DB
    # ordering matches wall-clock ordering across batches.
    batch_ts = datetime.utcnow()
    written = 0
    try:
        with session_scope() as s:
            for p in report.proposals:
                payload = {
                    "computed_at": batch_ts.isoformat(),
                    "window_days": report.window_days,
                    "min_n": report.min_n,
                    "agent": p.agent,
                    "n_closed": p.n_closed,
                    "hit_rate": p.hit_rate,
                    "brier_score": p.brier_score,
                    "ece": p.ece,
                    "discrimination": p.discrimination,
                    "confidence_level": p.confidence_level,
                    "adaptive_multiplier": p.adaptive_multiplier,
                    "base_weight": p.base_weight,
                    "current_weight": p.current_weight,
                    "weight_proposed": p.weight_proposed,
                    "rationale": p.rationale,
                    "advisory_enabled": report.advisory_enabled,
                    "apply_enabled": report.apply_enabled,
                }
                row = AgentWeightHistory(
                    computed_at=batch_ts,
                    agent=p.agent,
                    base_weight=float(p.base_weight),
                    weight_proposed=float(p.weight_proposed),
                    weight_active=float(p.weight_proposed),
                    adaptive_multiplier=float(p.adaptive_multiplier),
                    n_closed=int(p.n_closed or 0),
                    confidence_level=p.confidence_level,
                    rationale=p.rationale,
                    payload_json=json.dumps(payload, default=str),
                )
                s.add(row)
                written += 1
            s.flush()
    except Exception:
        logger.exception("persist_weight_proposals failed")
        return written
    return written


def latest_weight_rows(
    *, agent: Optional[str] = None, limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return the most recently computed batch.

    "Most recent batch" = rows sharing the maximum ``computed_at``
    timestamp matching the optional ``agent`` filter. Empty list when
    no rows exist yet.
    """
    from backend.models.agent_weight_history import AgentWeightHistory

    with session_scope() as s:
        q = select(AgentWeightHistory)
        if agent:
            q = q.where(AgentWeightHistory.agent == agent)
        head = s.execute(
            q.order_by(desc(AgentWeightHistory.computed_at)).limit(1)
        ).scalars().first()
        if head is None:
            return []
        rows = s.execute(
            q.where(AgentWeightHistory.computed_at == head.computed_at)
            .order_by(AgentWeightHistory.agent)
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]


def history_for_agent(agent: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Rolling history for a single agent — most recent first. Used by
    the cockpit history endpoint so the operator can see how an agent's
    proposed weight drifted across nightly recomputes."""
    if not agent:
        return []
    from backend.models.agent_weight_history import AgentWeightHistory

    with session_scope() as s:
        rows = s.execute(
            select(AgentWeightHistory)
            .where(AgentWeightHistory.agent == agent)
            .order_by(desc(AgentWeightHistory.computed_at))
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]
