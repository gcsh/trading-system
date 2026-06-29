"""Numeric promotion gates — Stage-1.5 explicit thresholds.

Every gate is a single ``GateCheck`` with a numeric threshold and a clear
verdict (pass/fail/insufficient_data). The full evaluation runs against the
live metrics surface so the gate verdict is always reproducible and visible.

Used by:
  • ``/audit/health`` extension — surfaces gate status alongside reconciliation
  • Stage transitions — Stage N must pass its gates before Stage N+1 starts
  • Eventual canary → scaled-live promotion

The thresholds live here, not in scattered config — one screen tells you the
entire institutional contract the bot must satisfy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── gate definitions ────────────────────────────────────────────────────────


@dataclass
class GateCheck:
    """One numeric gate. Verdict is computed by ``evaluate``."""
    name: str
    threshold: float
    direction: str          # "lte" → metric must be ≤ threshold; "gte" → ≥
    metric_path: str        # dotted path into the metrics dict
    minimum_sample: int = 0
    description: str = ""

    def evaluate(self, metrics: Dict[str, Any], sample_size: Optional[int] = None) -> Dict[str, Any]:
        value = _lookup(metrics, self.metric_path)
        if value is None:
            return {"name": self.name, "verdict": "insufficient_data",
                     "value": None, "threshold": self.threshold,
                     "reason": f"metric '{self.metric_path}' not available"}
        if self.minimum_sample and sample_size is not None and sample_size < self.minimum_sample:
            return {"name": self.name, "verdict": "insufficient_data",
                     "value": value, "threshold": self.threshold,
                     "reason": f"only {sample_size} samples; need ≥ {self.minimum_sample}"}
        ok = (value <= self.threshold) if self.direction == "lte" else (value >= self.threshold)
        return {"name": self.name, "verdict": "pass" if ok else "fail",
                 "value": value, "threshold": self.threshold,
                 "direction": self.direction, "description": self.description}


def _lookup(d: Dict[str, Any], path: str) -> Optional[float]:
    """Drill into a nested dict by dotted path. Returns ``None`` on miss."""
    cur: Any = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, (int, float)):
        return float(cur)
    return None


# ── the gate catalog (the numeric contract) ─────────────────────────────────


CATALOG: List[GateCheck] = [
    GateCheck(
        name="brier_ok",
        threshold=0.22,
        direction="lte",
        metric_path="data.brier",
        minimum_sample=100,
        description="Calibrated win-probability quality: Brier ≤ 0.22 "
                     "(< 0.25 coin-flip baseline). Stage-5 ML gate.",
    ),
    GateCheck(
        name="calibration_error_ok",
        threshold=0.05,
        direction="lte",
        metric_path="data.calibration_error",
        minimum_sample=100,
        description="Expected calibration error ≤ 0.05 — predicted prob and "
                     "actual hit rate must agree within 5 percentage points.",
    ),
    GateCheck(
        name="sharpe_floor",
        threshold=1.2,
        direction="gte",
        metric_path="data.sharpe",
        minimum_sample=60,
        description="Annualized Sharpe ≥ 1.2 over ≥ 60 days. Stage-2 TCA gate.",
    ),
    GateCheck(
        name="max_drawdown_ceiling",
        threshold=0.15,
        direction="lte",
        metric_path="data.max_drawdown_pct",
        minimum_sample=30,
        description="Max drawdown ≤ 15% over trailing 90 days.",
    ),
    GateCheck(
        name="win_rate_floor",
        threshold=0.45,
        direction="gte",
        metric_path="data.win_rate",
        minimum_sample=100,
        description="Win rate ≥ 45% over ≥ 100 closed trades in primary cohort.",
    ),
    GateCheck(
        name="profit_factor_floor",
        threshold=1.5,
        direction="gte",
        metric_path="data.profit_factor",
        minimum_sample=100,
        description="Profit factor ≥ 1.5 (gross wins ≥ 1.5× gross losses).",
    ),
    GateCheck(
        name="expectancy_positive",
        threshold=0.0,
        direction="gte",
        metric_path="data.expectancy",
        minimum_sample=30,
        description="Expected $ P&L per trade must be > 0.",
    ),
    # Stage-11.8 stability gates — pooled calibration can hide regime-specific
    # drift. Require the std of rolling Brier/ECE across windows to stay
    # bounded; ≥ 3 windows of ≥ 30 closed trades = ≥ 90 labelled trades.
    GateCheck(
        name="brier_stability_ok",
        threshold=0.05,
        direction="lte",
        metric_path="data.brier_stability_std",
        minimum_sample=90,
        description="Std-dev of rolling Brier across windows ≤ 0.05 — a low "
                     "mean Brier with high std means the model is regime-fragile.",
    ),
    GateCheck(
        name="calibration_error_stability_ok",
        threshold=0.04,
        direction="lte",
        metric_path="data.calibration_error_stability_std",
        minimum_sample=90,
        description="Std-dev of rolling ECE across windows ≤ 0.04 — caps "
                     "regime-to-regime miscalibration drift.",
    ),
]


# ── evaluation ──────────────────────────────────────────────────────────────


def evaluate_gates(metrics_summary: Dict[str, Any]) -> Dict[str, Any]:
    """Run the full catalog against a ``/metrics/summary``-shaped payload.

    Returns ``{gates: [...], pass_count, fail_count, insufficient_count,
    overall: pass | fail | insufficient_data}``. ``overall`` is ``pass`` only
    when EVERY non-insufficient gate passes.
    """
    quality = metrics_summary.get("label_quality") or {}
    sample_size = int(quality.get("closed") or 0)

    results: List[Dict[str, Any]] = []
    for gate in CATALOG:
        results.append(gate.evaluate(metrics_summary, sample_size=sample_size))

    n_pass = sum(1 for r in results if r["verdict"] == "pass")
    n_fail = sum(1 for r in results if r["verdict"] == "fail")
    n_insuff = sum(1 for r in results if r["verdict"] == "insufficient_data")
    if n_fail > 0:
        overall = "fail"
    elif n_pass == 0:
        overall = "insufficient_data"
    else:
        overall = "pass"

    return {
        "gates": results,
        "pass_count": n_pass,
        "fail_count": n_fail,
        "insufficient_count": n_insuff,
        "overall": overall,
        "closed_trades": sample_size,
    }
