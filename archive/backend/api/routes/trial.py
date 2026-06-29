"""Stage-17 — Promotion Readiness ("30-day trial") endpoint.

The right question after the 30-day paper trial isn't "are we up $500
or $1500?" — it's:
  • Do we have enough sample size for the calibration gates to be trusted?
  • Is the system *calibrated*? (Brier, ECE, stability std all within band)
  • Is expectancy positive over the trial window?

This endpoint surfaces those answers as a single, scannable readiness
verdict + per-gate progress.

Pure read over /gates/status + /metrics/summary. No DB writes.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/trial", tags=["trial"])


# Default 30-day paper trial began 2026-05-28 (recorded in memory). The
# operator can override via query.
_DEFAULT_TRIAL_START = date(2026, 5, 28)


def _days_into_trial(start: date) -> int:
    today = datetime.utcnow().date()
    return max(0, (today - start).days)


def _verdict(gate_results: List[Dict[str, Any]],
                closed_trades: int,
                days_in: int,
                min_trades: int) -> Dict[str, Any]:
    """The single 'ready / not ready' call. Three failure modes:
      • need_more_data — sample size below ``min_trades``
      • need_calibration — sample OK but ≥1 calibration gate failed
      • need_edge — calibration OK but expectancy / profit-factor failed
      • ready — every gate passed (or insufficient is acceptable for early-window gates)
    """
    calibration_gates = {"brier_ok", "calibration_error_ok",
                            "brier_stability_ok",
                            "calibration_error_stability_ok"}
    edge_gates = {"sharpe_floor", "max_drawdown_ceiling", "win_rate_floor",
                    "profit_factor_floor", "expectancy_positive"}

    by_name = {g["name"]: g for g in gate_results}
    failed_calibration = [n for n in calibration_gates
                            if by_name.get(n, {}).get("verdict") == "fail"]
    failed_edge = [n for n in edge_gates
                     if by_name.get(n, {}).get("verdict") == "fail"]
    insufficient = [n for n in (calibration_gates | edge_gates)
                       if by_name.get(n, {}).get("verdict") == "insufficient_data"]

    if closed_trades < min_trades:
        return {
            "status": "need_more_data",
            "headline": (f"Need {min_trades - closed_trades} more closed "
                            f"trades ({closed_trades} of {min_trades})"),
            "blockers": insufficient,
            "trades_to_go": min_trades - closed_trades,
        }
    if failed_calibration:
        return {
            "status": "need_calibration",
            "headline": "Calibration drift — model isn't honest yet",
            "blockers": failed_calibration,
            "trades_to_go": 0,
        }
    if failed_edge:
        return {
            "status": "need_edge",
            "headline": "Calibration OK but expectancy / edge gates failing",
            "blockers": failed_edge,
            "trades_to_go": 0,
        }
    if insufficient:
        return {
            "status": "ready_with_caveats",
            "headline": (f"Every passed gate is passing but {len(insufficient)} "
                            f"gates still need data"),
            "blockers": insufficient,
            "trades_to_go": 0,
        }
    return {
        "status": "ready",
        "headline": "All 9 gates passing — system is ready to promote",
        "blockers": [],
        "trades_to_go": 0,
    }


@router.get("/readiness")
async def readiness(min_trades: int = Query(100, ge=10, le=2000),
                       target_trades: int = Query(200, ge=20, le=5000),
                       trial_start: Optional[str] = Query(None),
                       trial_days: int = Query(30, ge=1, le=365),
                       ) -> dict:
    """Return the readiness snapshot for the paper-trial → live promotion."""
    from backend.api.routes.metrics import build_summary
    from backend.bot.gates import evaluate_gates

    summary = build_summary()
    gate_payload = evaluate_gates(summary)

    start = _DEFAULT_TRIAL_START
    if trial_start:
        try:
            start = date.fromisoformat(trial_start)
        except Exception:
            pass
    days_in = _days_into_trial(start)
    data = summary.get("data") or {}
    label_q = summary.get("label_quality") or {}
    closed_trades = int(label_q.get("closed") or 0)

    verdict = _verdict(gate_payload.get("gates") or [], closed_trades,
                          days_in, min_trades)

    # Highlight progress on each axis vs target.
    progress = {
        "sample_size": {
            "current": closed_trades,
            "minimum": min_trades,
            "target": target_trades,
            "min_pct": round(min(1.0, closed_trades / min_trades), 3),
            "target_pct": round(min(1.0, closed_trades / target_trades), 3),
        },
        "calibration": {
            "brier": data.get("brier"),
            "brier_target": 0.22,
            "ece": data.get("calibration_error"),
            "ece_target": 0.05,
            "brier_stability_std": data.get("brier_stability_std"),
            "brier_stability_target": 0.05,
            "ece_stability_std": data.get("calibration_error_stability_std"),
            "ece_stability_target": 0.04,
        },
        "edge": {
            "expectancy": data.get("expectancy"),
            "expectancy_target": 0.0,
            "profit_factor": data.get("profit_factor"),
            "profit_factor_target": 1.5,
            "win_rate": data.get("win_rate"),
            "win_rate_target": 0.45,
            "sharpe": data.get("sharpe"),
            "sharpe_target": 1.2,
            "max_drawdown_pct": data.get("max_drawdown_pct"),
            "max_drawdown_target": 0.15,
        },
    }

    return {
        "trial": {
            "start_date": start.isoformat(),
            "days_in": days_in,
            "days_total": trial_days,
            "days_remaining": max(0, trial_days - days_in),
            "complete": days_in >= trial_days,
        },
        "verdict": verdict,
        "progress": progress,
        "gates": gate_payload.get("gates") or [],
        "gates_summary": {
            "pass": gate_payload.get("pass_count"),
            "fail": gate_payload.get("fail_count"),
            "insufficient": gate_payload.get("insufficient_count"),
            "overall": gate_payload.get("overall"),
        },
    }
