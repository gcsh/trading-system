"""MITS Phase 18-FU Stream D (Gap 10) — Learning Impact Measurement.

Before Stream D shipped, operator-facing learning surfaces could tell
you WHAT the advisor recommended and WHEN it was applied — but never
"did applying it move the needle?". This module answers that question
honestly: pre/post-event windows over decision_provenance + Trade
outcomes, with a strict insufficient_sample guard so the operator never
sees a fake "+3% lift" computed off two trades.

The four metrics:
  * ``submission_rate``    — fraction of decision_provenance rows in the
                              window that ultimately produced a Trade
                              (trade_id is not None). Stand-in for
                              "did the system act?"
  * ``composite_mean``      — mean of decision_quality_score_json.composite
                              over rows that carry it.
  * ``hit_rate``            — fraction of closed Trades with pnl > 0 in
                              the window (closed_by_reset excluded, same
                              eligibility rules as 18.A attribution).
  * ``mean_pnl_pct``        — mean realized pct return (same formula as
                              ``_realized_pct`` in attribution.py).

Honesty rules baked into the report:
  * ``is_significant`` stays 0 unless ``min(n_closed_before, n_closed_after)``
    >= ``MIN_N_FOR_SIGNIFICANCE``. With the current 2-closures-in-14d
    sparsity this will almost always be 0 — and the ``note`` will say
    "insufficient_sample_size_for_significance" out loud.
  * Same window length on both sides (default 7d each) so the delta
    isn't biased by asymmetric coverage.
  * When the event has no priors at all (apply flag just flipped, no
    history), ``metrics_before`` is None for every key. The delta is
    not computed and ``note`` carries "no_pre_event_window".

This module DOES NOT WRITE TO TUNABLES. The output is observability
only — a learning_impact row per event_id × event_type per nightly
recompute. The operator interprets it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.bot.learning.attribution import (
    _is_closed_trade,
    _is_eligible_signal_source,
    _realized_pct,
)
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.learning_impact import (
    ALLOWED_EVENT_TYPES,
    EVENT_TYPE_POLICY_APPLY,
    EVENT_TYPE_WEIGHT_APPLY,
    EVENT_TYPE_WEIGHT_ROLLBACK,
    LearningImpact,
)
from backend.models.trade import Trade


logger = logging.getLogger(__name__)


# ── Public knobs (config-driven; no magic numbers in the math) ────────


DEFAULT_BEFORE_WINDOW_DAYS = 7
DEFAULT_AFTER_WINDOW_DAYS = 7
# Below this n_closed floor we REFUSE to claim significance. With the
# current data sparsity this will almost always trip; the ``note`` field
# tells the operator why.
MIN_N_FOR_SIGNIFICANCE = 20
# Absolute composite delta below this magnitude is treated as "noise
# floor — no signal" even when n clears the sample floor. Keeps the
# operator from chasing 0.3-point composite drifts that mean nothing.
NOISE_FLOOR_COMPOSITE_DELTA = 1.0

NOTE_INSUFFICIENT = "insufficient_sample_size_for_significance"
NOTE_NO_PRE_WINDOW = "no_pre_event_window"
NOTE_NO_POST_WINDOW = "no_post_event_window"
NOTE_EVENT_NOT_FOUND = "event_row_not_found"
NOTE_OK = "windows_computed"


# ── Dataclasses (round-trippable) ─────────────────────────────────────


@dataclass
class WindowMetrics:
    """One window's measured metrics. Every field is Optional so the
    operator can see exactly which numbers were computable from the
    sample — the dataclass never inserts zeros to fill gaps."""
    n_decisions: int
    n_closed: int
    submission_rate: Optional[float]
    composite_mean: Optional[float]
    hit_rate: Optional[float]
    mean_pnl_pct: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LearningImpactReport:
    """The audit row format the impact_measurement primitive returns.

    ``is_significant`` is the only opinionated boolean. Everything else
    is raw measurement. The ``note`` carries the operator-facing
    "why None" so the cockpit never shows blank fields without
    explanation.
    """
    learning_event_type: str
    event_id: int
    event_timestamp: datetime
    before_window_days: int = DEFAULT_BEFORE_WINDOW_DAYS
    after_window_days: int = DEFAULT_AFTER_WINDOW_DAYS
    metrics_before: Optional[WindowMetrics] = None
    metrics_after: Optional[WindowMetrics] = None
    delta: Dict[str, Optional[float]] = field(default_factory=dict)
    is_significant: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "learning_event_type": self.learning_event_type,
            "event_id": int(self.event_id),
            "event_timestamp": (
                self.event_timestamp.isoformat()
                if self.event_timestamp else None
            ),
            "before_window_days": int(self.before_window_days),
            "after_window_days": int(self.after_window_days),
            "metrics_before": (
                self.metrics_before.to_dict()
                if self.metrics_before else None
            ),
            "metrics_after": (
                self.metrics_after.to_dict()
                if self.metrics_after else None
            ),
            "delta": dict(self.delta),
            "is_significant": bool(self.is_significant),
            "note": self.note,
        }


# ── Internal helpers ──────────────────────────────────────────────────


def _safe_decode(blob: Optional[str]) -> Optional[Dict[str, Any]]:
    if not blob:
        return None
    try:
        out = json.loads(blob)
        return out if isinstance(out, dict) else None
    except (TypeError, ValueError):
        return None


def _composite_from_dqs(blob: Optional[str]) -> Optional[float]:
    """Extract the composite score from a ``decision_quality_score_json``
    blob. Mirrors the read in routes/decision.py:_composite_bin_label
    so cohort numbers tie out across surfaces."""
    dqs = _safe_decode(blob)
    if not dqs:
        return None
    raw = dqs.get("composite")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _compute_window_metrics(
    *, start: datetime, end: datetime,
) -> WindowMetrics:
    """Measure the four metrics over the [start, end) window. Pure read;
    swallows DB failures and returns the empty-window shape."""
    if start >= end:
        return WindowMetrics(
            n_decisions=0,
            n_closed=0,
            submission_rate=None,
            composite_mean=None,
            hit_rate=None,
            mean_pnl_pct=None,
        )
    try:
        with session_scope() as s:
            prov_rows = s.execute(
                select(DecisionProvenance)
                .where(DecisionProvenance.decision_timestamp >= start)
                .where(DecisionProvenance.decision_timestamp < end)
            ).scalars().all()

            n_decisions = len(prov_rows)
            n_with_trade = 0
            composites: List[float] = []
            trade_ids: List[int] = []
            for row in prov_rows:
                comp = _composite_from_dqs(
                    row.decision_quality_score_json,
                )
                if comp is not None:
                    composites.append(comp)
                if row.trade_id is not None:
                    n_with_trade += 1
                    try:
                        trade_ids.append(int(row.trade_id))
                    except (TypeError, ValueError):
                        continue

            n_closed = 0
            wins = 0
            pnl_pcts: List[float] = []
            if trade_ids:
                for t in s.execute(
                    select(Trade).where(Trade.id.in_(trade_ids))
                ).scalars().all():
                    if not _is_closed_trade(t):
                        continue
                    if not _is_eligible_signal_source(t):
                        continue
                    pct = _realized_pct(t)
                    if pct is None:
                        continue
                    n_closed += 1
                    pnl_pcts.append(float(pct))
                    if (t.pnl or 0.0) > 0:
                        wins += 1

            submission_rate: Optional[float] = (
                round(n_with_trade / n_decisions, 4)
                if n_decisions > 0 else None
            )
            composite_mean: Optional[float] = (
                round(sum(composites) / len(composites), 4)
                if composites else None
            )
            hit_rate: Optional[float] = (
                round(wins / n_closed, 4) if n_closed > 0 else None
            )
            mean_pnl_pct: Optional[float] = (
                round(sum(pnl_pcts) / len(pnl_pcts), 4)
                if pnl_pcts else None
            )
            return WindowMetrics(
                n_decisions=int(n_decisions),
                n_closed=int(n_closed),
                submission_rate=submission_rate,
                composite_mean=composite_mean,
                hit_rate=hit_rate,
                mean_pnl_pct=mean_pnl_pct,
            )
    except Exception:
        logger.debug("_compute_window_metrics failed", exc_info=True)
        return WindowMetrics(
            n_decisions=0,
            n_closed=0,
            submission_rate=None,
            composite_mean=None,
            hit_rate=None,
            mean_pnl_pct=None,
        )


def _delta_dict(
    before: Optional[WindowMetrics], after: Optional[WindowMetrics],
) -> Dict[str, Optional[float]]:
    """Compute after - before for each scalar field. None on either side
    yields None for that key — we never invent zeros to look complete."""
    keys = (
        "submission_rate", "composite_mean", "hit_rate", "mean_pnl_pct",
    )
    out: Dict[str, Optional[float]] = {k: None for k in keys}
    if before is None or after is None:
        return out
    for k in keys:
        b = getattr(before, k, None)
        a = getattr(after, k, None)
        if b is None or a is None:
            continue
        try:
            out[k] = round(float(a) - float(b), 4)
        except (TypeError, ValueError):
            continue
    return out


def _is_significant(
    delta: Dict[str, Optional[float]],
    before: Optional[WindowMetrics],
    after: Optional[WindowMetrics],
) -> Tuple[bool, str]:
    """Apply the sample-size floor + noise floor. Returns (flag, note)."""
    if before is None or after is None:
        return False, NOTE_NO_PRE_WINDOW if before is None else NOTE_NO_POST_WINDOW
    if before.n_closed < MIN_N_FOR_SIGNIFICANCE:
        return False, NOTE_INSUFFICIENT
    if after.n_closed < MIN_N_FOR_SIGNIFICANCE:
        return False, NOTE_INSUFFICIENT
    comp_delta = delta.get("composite_mean")
    if comp_delta is None:
        return False, NOTE_INSUFFICIENT
    if abs(comp_delta) < NOISE_FLOOR_COMPOSITE_DELTA:
        return False, NOTE_OK
    return True, NOTE_OK


def _resolve_event_timestamp(
    event_type: str, event_id: int,
) -> Optional[datetime]:
    """Look up the event's wall-clock timestamp from the source table.
    Returns None when the event_type is unknown or the row doesn't
    exist."""
    if event_type == EVENT_TYPE_WEIGHT_APPLY:
        try:
            from backend.models.weight_application_log import (
                WeightApplicationLog,
            )
            with session_scope() as s:
                row = s.get(WeightApplicationLog, int(event_id))
                return row.applied_at if row else None
        except Exception:
            logger.debug(
                "resolve_event_timestamp/weight_apply failed",
                exc_info=True,
            )
            return None
    if event_type == EVENT_TYPE_WEIGHT_ROLLBACK:
        # Rollback events live on learning_rollback_log (18.E ledger).
        # We reuse the same wall-clock field name (``created_at``).
        try:
            from backend.models.learning_rollback_log import (
                LearningRollbackLog,
            )
            with session_scope() as s:
                row = s.get(LearningRollbackLog, int(event_id))
                return getattr(row, "created_at", None) if row else None
        except Exception:
            logger.debug(
                "resolve_event_timestamp/rollback failed", exc_info=True,
            )
            return None
    if event_type == EVENT_TYPE_POLICY_APPLY:
        # Policy apply events live on policy_tunings rows whose
        # ``applied_at`` is non-NULL — that's the wall-clock the operator
        # flipped the threshold.
        try:
            from backend.models.policy_tuning import PolicyTuning
            with session_scope() as s:
                row = s.get(PolicyTuning, int(event_id))
                return (
                    getattr(row, "applied_at", None) if row else None
                )
        except Exception:
            logger.debug(
                "resolve_event_timestamp/policy_apply failed",
                exc_info=True,
            )
            return None
    return None


# ── Public API ────────────────────────────────────────────────────────


def compute_impact(
    event_type: str,
    event_id: int,
    *,
    before_window_days: int = DEFAULT_BEFORE_WINDOW_DAYS,
    after_window_days: int = DEFAULT_AFTER_WINDOW_DAYS,
    event_timestamp: Optional[datetime] = None,
) -> LearningImpactReport:
    """Compute a single before/after impact report for one learning event.

    ``event_timestamp`` is an override hook for tests — production
    callers leave it None and the helper resolves the wall-clock from
    the source table. When the event row can't be found and no override
    was passed, the report carries ``note=event_row_not_found`` and
    every metric is None.
    """
    if event_type not in ALLOWED_EVENT_TYPES:
        return LearningImpactReport(
            learning_event_type=str(event_type),
            event_id=int(event_id),
            event_timestamp=datetime.utcnow(),
            note="unknown_event_type",
        )
    ts = event_timestamp or _resolve_event_timestamp(event_type, event_id)
    if ts is None:
        return LearningImpactReport(
            learning_event_type=event_type,
            event_id=int(event_id),
            event_timestamp=datetime.utcnow(),
            note=NOTE_EVENT_NOT_FOUND,
        )

    before_days = int(max(1, before_window_days))
    after_days = int(max(1, after_window_days))
    before_start = ts - timedelta(days=before_days)
    after_end = ts + timedelta(days=after_days)

    before = _compute_window_metrics(start=before_start, end=ts)
    after = _compute_window_metrics(start=ts, end=after_end)
    delta = _delta_dict(before, after)
    significant, note = _is_significant(delta, before, after)

    return LearningImpactReport(
        learning_event_type=event_type,
        event_id=int(event_id),
        event_timestamp=ts,
        before_window_days=before_days,
        after_window_days=after_days,
        metrics_before=before,
        metrics_after=after,
        delta=delta,
        is_significant=bool(significant),
        note=note,
    )


def _iter_recent_events(
    *, days_back: int,
) -> List[Tuple[str, int, datetime]]:
    """Enumerate learning events from the last ``days_back`` days across
    the three source tables. Returns a list of
    (event_type, event_id, event_timestamp) — used by the nightly
    impact recompute."""
    if days_back <= 0:
        return []
    cutoff = datetime.utcnow() - timedelta(days=int(days_back))
    out: List[Tuple[str, int, datetime]] = []
    # Weight apply events (Gap 6 log).
    try:
        from backend.models.weight_application_log import (
            WeightApplicationLog,
        )
        with session_scope() as s:
            rows = s.execute(
                select(WeightApplicationLog)
                .where(WeightApplicationLog.applied_at >= cutoff)
                .order_by(desc(WeightApplicationLog.applied_at))
            ).scalars().all()
            for r in rows:
                if r.applied_at is None:
                    continue
                out.append(
                    (EVENT_TYPE_WEIGHT_APPLY, int(r.id), r.applied_at),
                )
    except Exception:
        logger.debug("iter_recent_events/weight_apply failed", exc_info=True)
    # Policy apply events (policy_tunings.applied_at non-null).
    try:
        from backend.models.policy_tuning import PolicyTuning
        with session_scope() as s:
            rows = s.execute(
                select(PolicyTuning)
                .where(PolicyTuning.applied_at.is_not(None))
                .where(PolicyTuning.applied_at >= cutoff)
                .order_by(desc(PolicyTuning.applied_at))
            ).scalars().all()
            for r in rows:
                if r.applied_at is None:
                    continue
                out.append(
                    (EVENT_TYPE_POLICY_APPLY, int(r.id), r.applied_at),
                )
    except Exception:
        logger.debug("iter_recent_events/policy_apply failed", exc_info=True)
    # Weight rollback events (learning_rollback_log).
    try:
        from backend.models.learning_rollback_log import LearningRollbackLog
        with session_scope() as s:
            rows = s.execute(
                select(LearningRollbackLog)
                .where(LearningRollbackLog.created_at >= cutoff)
                .order_by(desc(LearningRollbackLog.created_at))
            ).scalars().all()
            for r in rows:
                ts = getattr(r, "created_at", None)
                if ts is None:
                    continue
                out.append(
                    (EVENT_TYPE_WEIGHT_ROLLBACK, int(r.id), ts),
                )
    except Exception:
        logger.debug(
            "iter_recent_events/rollback failed", exc_info=True,
        )
    return out


def compute_all_recent_impacts(
    *,
    days_back: int = 30,
    before_window_days: int = DEFAULT_BEFORE_WINDOW_DAYS,
    after_window_days: int = DEFAULT_AFTER_WINDOW_DAYS,
) -> List[LearningImpactReport]:
    """Run ``compute_impact`` over every learning event in the recent
    ``days_back`` window. Returns the report list — the nightly job
    persists them via ``persist_impact_reports`` after this returns.

    With all safety flags OFF (the default) the underlying ledgers are
    empty, so this returns ``[]`` and the cockpit shows an empty state.
    """
    events = _iter_recent_events(days_back=days_back)
    reports: List[LearningImpactReport] = []
    for event_type, event_id, ts in events:
        try:
            rpt = compute_impact(
                event_type, event_id,
                before_window_days=before_window_days,
                after_window_days=after_window_days,
                event_timestamp=ts,
            )
            reports.append(rpt)
        except Exception:
            logger.debug(
                "compute_impact failed for (%s, %d)",
                event_type, event_id, exc_info=True,
            )
            continue
    return reports


def persist_impact_reports(
    reports: List[LearningImpactReport],
) -> Dict[str, Any]:
    """Append one ``learning_impact`` row per report. Append-only — we
    keep a forever history of impact computations so the operator can
    re-read "what did we measure at the time?" months later.

    Returns ``{ok, written, computed_at}``. Never raises.
    """
    written = 0
    try:
        with session_scope() as s:
            now = datetime.utcnow()
            for rpt in reports:
                payload = rpt.to_dict()
                metrics_before = payload.get("metrics_before")
                metrics_after = payload.get("metrics_after")
                row = LearningImpact(
                    computed_at=now,
                    learning_event_type=rpt.learning_event_type,
                    event_id=int(rpt.event_id),
                    event_timestamp=rpt.event_timestamp,
                    before_window_days=int(rpt.before_window_days),
                    after_window_days=int(rpt.after_window_days),
                    metrics_before_json=(
                        json.dumps(metrics_before, default=str)
                        if metrics_before is not None else None
                    ),
                    metrics_after_json=(
                        json.dumps(metrics_after, default=str)
                        if metrics_after is not None else None
                    ),
                    delta_json=json.dumps(payload.get("delta") or {}, default=str),
                    is_significant=1 if rpt.is_significant else 0,
                    note=rpt.note,
                )
                s.add(row)
                written += 1
            s.flush()
    except Exception:
        logger.exception("persist_impact_reports failed")
        return {"ok": False, "written": written}
    return {
        "ok": True,
        "written": written,
        "computed_at": datetime.utcnow().isoformat(),
    }


def latest_impact_rows(
    *,
    event_type: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return the most recent ``learning_impact`` rows, optionally filtered
    by ``event_type``. Used by the ``/learning/observability/impact``
    route."""
    if event_type is not None and event_type not in ALLOWED_EVENT_TYPES:
        return []
    try:
        with session_scope() as s:
            q = select(LearningImpact).order_by(
                desc(LearningImpact.computed_at)
            ).limit(int(max(1, min(200, limit))))
            if event_type is not None:
                q = q.where(LearningImpact.learning_event_type == event_type)
            rows = s.execute(q).scalars().all()
            return [r.to_dict() for r in rows]
    except Exception:
        logger.debug("latest_impact_rows failed", exc_info=True)
        return []
