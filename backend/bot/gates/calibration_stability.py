"""Stage-11.8 Calibration Stability — std-dev of rolling Brier/ECE.

A model can have a great *average* Brier/ECE while bouncing wildly across
time windows (calibrated in trend regimes, miscalibrated in chop, etc.).
Stage-1.5's two calibration gates (``brier_ok``, ``calibration_error_ok``)
only check the pooled mean — this module adds the *stability* dimension.

Approach:

  1. Walk the closed-label sequence chronologically.
  2. Cut into non-overlapping windows of N consecutive labels.
  3. Compute Brier + ECE per window.
  4. Report population std-dev of those window scores.
  5. Two new gates check that std-dev stays below a band:
       • ``brier_stability_ok``              std(Brier) ≤ 0.05
       • ``calibration_error_stability_ok``  std(ECE)   ≤ 0.04

The thresholds are intentionally tighter than the per-window means — a
small drift across windows is acceptable; a wide one means the model
learned a regime-specific quirk and the calibration label can't be trusted.

This module is **read-only and pure** — given a label list, it returns
a deterministic stability report. It does not write to the DB. The metrics
summary builder calls ``stability_summary()`` to plumb the two new fields
into ``/metrics/summary.data`` so the existing gates pipeline picks them up.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from backend.bot.labeling import TradeLabel
from backend.bot.metrics import brier_score, calibration_error

logger = logging.getLogger(__name__)


# ── window-level stability report ────────────────────────────────────────


@dataclass
class WindowMetric:
    index: int
    n: int
    start: Optional[str]
    end: Optional[str]
    brier: Optional[float]
    ece: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StabilityReport:
    n_windows: int
    window_size: int
    closed_labels: int
    brier_mean: Optional[float]
    brier_std: Optional[float]
    brier_min: Optional[float]
    brier_max: Optional[float]
    ece_mean: Optional[float]
    ece_std: Optional[float]
    ece_min: Optional[float]
    ece_max: Optional[float]
    windows: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sortable_ts(label: TradeLabel) -> str:
    return label.timestamp or ""


def _calibrated_labels(labels: Sequence[TradeLabel]) -> List[TradeLabel]:
    """Only labels with BOTH a recorded prediction and a known outcome can
    contribute to Brier/ECE."""
    return [l for l in labels
              if l.win is not None and l.win_probability is not None]


def compute_stability(labels: Sequence[TradeLabel],
                        *,
                        window_size: int = 30,
                        min_windows: int = 3) -> StabilityReport:
    """Chunk the calibrated labels into ``window_size``-sized blocks and
    compute Brier + ECE per block. Reports population std-dev across blocks.

    Returns a fully-populated ``StabilityReport`` even when only one window
    is possible (std-dev is ``None`` until ``min_windows`` are present so
    downstream gates fall through to ``insufficient_data``).
    """
    closed = sorted(_calibrated_labels(labels), key=_sortable_ts)
    n_closed = len(closed)

    windows: List[WindowMetric] = []
    for idx, start in enumerate(range(0, n_closed - window_size + 1, window_size)):
        chunk = closed[start: start + window_size]
        preds = [float(l.win_probability) for l in chunk]
        outs = [int(l.win) for l in chunk]
        windows.append(WindowMetric(
            index=idx, n=len(chunk),
            start=chunk[0].timestamp if chunk else None,
            end=chunk[-1].timestamp if chunk else None,
            brier=brier_score(preds, outs),
            ece=calibration_error(preds, outs),
        ))

    brier_vals = [w.brier for w in windows if w.brier is not None]
    ece_vals = [w.ece for w in windows if w.ece is not None]

    def _agg(vals: List[float]) -> Dict[str, Optional[float]]:
        if len(vals) < min_windows:
            return {"mean": None, "std": None, "min": None, "max": None}
        return {
            "mean": round(sum(vals) / len(vals), 4),
            "std": round(statistics.pstdev(vals), 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
        }

    b = _agg(brier_vals)
    e = _agg(ece_vals)
    return StabilityReport(
        n_windows=len(windows), window_size=window_size,
        closed_labels=n_closed,
        brier_mean=b["mean"], brier_std=b["std"],
        brier_min=b["min"], brier_max=b["max"],
        ece_mean=e["mean"], ece_std=e["std"],
        ece_min=e["min"], ece_max=e["max"],
        windows=[w.to_dict() for w in windows],
    )


def stability_summary(labels: Sequence[TradeLabel],
                        *,
                        window_size: int = 30,
                        min_windows: int = 3) -> Dict[str, Any]:
    """Compact view that plugs into ``/metrics/summary.data``. Returns only
    the scalars the gates need plus context — keeps payload small."""
    rpt = compute_stability(labels, window_size=window_size,
                                min_windows=min_windows)
    return {
        "brier_stability_std": rpt.brier_std,
        "calibration_error_stability_std": rpt.ece_std,
        "stability_n_windows": rpt.n_windows,
        "stability_window_size": rpt.window_size,
        "brier_stability_mean": rpt.brier_mean,
        "calibration_error_stability_mean": rpt.ece_mean,
    }
