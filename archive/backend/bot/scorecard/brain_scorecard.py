"""MITS Phase 14.D — Brain composition scorecard.

Aggregates resolved ``BrainPrediction`` rows into the operator-facing
scorecard that powers ``/brain/scorecard``. Three headline numbers:

  * predicted_win_rate  — N-weighted mean of ``posterior_at_decision``
    on the last ``window_trades`` resolved predictions.
  * realized_win_rate   — fraction of those that resolved as ``win``.
  * calibration_gap_pp  — predicted minus realized, in percentage
    points. Positive = the brain was over-confident.

Plus invalidation hit-rate (how often the model's stated invalidation
condition tripped within the holding window) and a 10-bin calibration
table for the reliability plot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.brain_prediction import (
    BrainPrediction,
    OUTCOME_LOSS,
    OUTCOME_PENDING,
    OUTCOME_SCRATCH,
    OUTCOME_WIN,
)


CALIBRATION_BIN_COUNT = 10


@dataclass
class BrainScorecard:
    window_trades: int
    predicted_win_rate: float
    realized_win_rate: float
    calibration_gap_pp: float
    invalidation_hit_rate: float
    invalidation_saved_capital_rate: float
    calibration_bins: List[Dict[str, float]] = field(default_factory=list)
    # MITS Phase 15.E — per-axis correctness aggregation across the
    # five thesis components stamped on each BrainPrediction.
    per_axis_calibration: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_trades": self.window_trades,
            "predicted_win_rate": round(self.predicted_win_rate, 4),
            "realized_win_rate": round(self.realized_win_rate, 4),
            "calibration_gap_pp": round(self.calibration_gap_pp, 2),
            "invalidation_hit_rate": round(self.invalidation_hit_rate, 4),
            "invalidation_saved_capital_rate": round(
                self.invalidation_saved_capital_rate, 4),
            "calibration_bins": self.calibration_bins,
            "per_axis_calibration": self.per_axis_calibration,
        }


PER_AXIS_KEYS = ("regime", "technical", "options", "analog", "strategy")


def _bin_index(p: float) -> int:
    """Map p in [0,1] to a CALIBRATION_BIN_COUNT-wide bin index."""
    idx = int(p * CALIBRATION_BIN_COUNT)
    if idx >= CALIBRATION_BIN_COUNT:
        idx = CALIBRATION_BIN_COUNT - 1
    if idx < 0:
        idx = 0
    return idx


def _calibration_bins(
    samples: List[Dict[str, float]],
) -> List[Dict[str, float]]:
    """Return the per-bin (predicted_midpoint, realized_win_rate, n) table."""
    buckets: List[List[Dict[str, float]]] = [
        [] for _ in range(CALIBRATION_BIN_COUNT)
    ]
    for s in samples:
        p = s.get("predicted")
        if p is None:
            continue
        buckets[_bin_index(float(p))].append(s)
    out: List[Dict[str, float]] = []
    for i, b in enumerate(buckets):
        n = len(b)
        mid = (i + 0.5) / CALIBRATION_BIN_COUNT
        if n == 0:
            out.append({
                "bin_midpoint": round(mid, 3),
                "n": 0,
                "predicted_mean": round(mid, 3),
                "realized_win_rate": None,
            })
            continue
        pred_mean = sum(float(s["predicted"]) for s in b) / n
        wins = sum(1 for s in b if s.get("outcome") == OUTCOME_WIN)
        out.append({
            "bin_midpoint": round(mid, 3),
            "n": n,
            "predicted_mean": round(pred_mean, 4),
            "realized_win_rate": round(wins / n, 4),
        })
    return out


def build_brain_scorecard(
    *,
    surface: Optional[str] = None,
    window_trades: int = 50,
) -> BrainScorecard:
    """Pull the most recent ``window_trades`` resolved predictions and
    compute the scorecard. ``surface`` filters to one of
    {"analysis", "eod_analysis", "opportunity_brain"}.
    """
    window_trades = max(1, int(window_trades))
    with session_scope() as s:
        q = (
            select(BrainPrediction)
            .where(BrainPrediction.outcome.in_(
                (OUTCOME_WIN, OUTCOME_LOSS, OUTCOME_SCRATCH)))
            .order_by(desc(BrainPrediction.resolved_at))
            .limit(window_trades)
        )
        if surface:
            q = q.where(BrainPrediction.surface == surface)
        rows = s.execute(q).scalars().all()
        samples: List[Dict[str, float]] = []
        invalidation_hits = 0
        invalidation_savings = 0
        invalidation_known = 0
        per_axis: Dict[str, Dict[str, Any]] = {
            axis: {"correct": 0, "n": 0} for axis in PER_AXIS_KEYS
        }
        for r in rows:
            p = (r.posterior_at_decision
                  if r.posterior_at_decision is not None
                  else r.confidence_self_assessment)
            samples.append({
                "predicted": float(p) if p is not None else None,
                "outcome": r.outcome,
            })
            if r.invalidation_hit is not None:
                invalidation_known += 1
                if bool(r.invalidation_hit):
                    invalidation_hits += 1
                    if bool(r.invalidation_saved_capital):
                        invalidation_savings += 1
            for axis in PER_AXIS_KEYS:
                val = getattr(r, f"{axis}_call_correct", None)
                if val is not None:
                    per_axis[axis]["n"] += 1
                    if bool(val):
                        per_axis[axis]["correct"] += 1

    n = len(samples)
    weighted_pred = [s for s in samples if s["predicted"] is not None]
    predicted_wr = (
        sum(float(s["predicted"]) for s in weighted_pred) / len(weighted_pred)
        if weighted_pred else 0.0
    )
    realized_wr = (
        sum(1 for s in samples if s["outcome"] == OUTCOME_WIN) / n
        if n else 0.0
    )
    gap_pp = (predicted_wr - realized_wr) * 100.0
    inv_rate = (
        invalidation_hits / invalidation_known
        if invalidation_known else 0.0
    )
    inv_saved_rate = (
        invalidation_savings / invalidation_hits
        if invalidation_hits else 0.0
    )
    per_axis_out: Dict[str, Dict[str, Any]] = {}
    for axis, agg in per_axis.items():
        nn = agg["n"]
        per_axis_out[axis] = {
            "predicted_correct_rate": round(agg["correct"] / nn, 4) if nn else 0.0,
            "n": nn,
        }
    return BrainScorecard(
        window_trades=n,
        predicted_win_rate=predicted_wr,
        realized_win_rate=realized_wr,
        calibration_gap_pp=gap_pp,
        invalidation_hit_rate=inv_rate,
        invalidation_saved_capital_rate=inv_saved_rate,
        calibration_bins=_calibration_bins(weighted_pred),
        per_axis_calibration=per_axis_out,
    )


__all__ = [
    "BrainScorecard",
    "build_brain_scorecard",
    "CALIBRATION_BIN_COUNT",
    "PER_AXIS_KEYS",
]
