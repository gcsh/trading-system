"""Performance metrics endpoints — Stage-1 evaluation surface.

Surfaces honest aggregates over the live decision-log + trade history:
  • ``/metrics/summary``         — single TradeMetrics over the whole history
  • ``/metrics/by-strategy``     — per-strategy breakdown
  • ``/metrics/by-grade``        — per-grade win rate + sample size
  • ``/metrics/by-regime``       — per-regime cohort
  • ``/metrics/calibration``     — reliability diagram data
  • ``/metrics/walkforward``     — rolling out-of-sample windows
  • ``/metrics/labels``          — raw labels + quality audit (debug)

All endpoints return ``{"data": ..., "label_quality": ...}`` so the UI can
show "insufficient data" prominently when the sample is too thin to trust.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from backend.bot.evaluation import walk_forward_evaluate
from backend.bot.labeling import TradeLabel, build_labels, label_quality
from backend.bot.metrics import (
    calibration_curve,
    expectancy,
    profit_factor,
    summarize,
    win_rate,
)
from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.snapshot import PortfolioSnapshot
from backend.models.trade import Trade

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/metrics", tags=["metrics"])


# ── data access ─────────────────────────────────────────────────────────────


def _load_labels(limit: int = 5000,
                    include_synthetic: bool = True) -> List[TradeLabel]:
    """Pull trades + decision-log rows and join them into labels. Extracts
    every ORM attribute inside the session to avoid DetachedInstanceError.

    **Excludes ``closed_by_reset``** (Bug fix 2026-06-02): when the operator
    runs ``soft_reset`` the orphan open trades get re-labeled
    ``closed_by_reset`` with ``pnl=0``. They are administrative housekeeping
    rows, not real trades — counting them as closes inflates the trade
    count and forces win_rate to 0% / Brier to nonsense values.
    """
    with session_scope() as session:
        stmt = (
            select(Trade)
            .where(Trade.status != "closed_by_reset")
        )
        if not include_synthetic:
            stmt = stmt.where(Trade.signal_source != "historical_replay")
        trade_rows = session.execute(
            stmt.order_by(desc(Trade.timestamp)).limit(limit)
        ).scalars().all()
        trades = [r.to_dict() for r in trade_rows]
        # DecisionLog has no signal_source column today (P2.1 limitation).
        # When live_only is requested, filter via the synthetic decision
        # marker we DO have: ``status='historical_replay_closed'``. That
        # status is set exclusively by the backfill writer (see
        # backend/bot/backfill/historical_replay.py).
        dec_stmt = select(DecisionLog)
        if not include_synthetic:
            dec_stmt = dec_stmt.where(
                DecisionLog.status != "historical_replay_closed"
            )
        decision_rows = session.execute(
            dec_stmt.order_by(desc(DecisionLog.timestamp)).limit(limit)
        ).scalars().all()
        decisions = [r.to_dict() for r in decision_rows]
    return build_labels(trades, decisions)


def _equity_curve(limit: int = 2000) -> List[float]:
    """Portfolio value over time for Sharpe / drawdown computation."""
    with session_scope() as session:
        rows = session.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.timestamp).limit(limit)
        ).scalars().all()
        return [float(r.portfolio_value or 0.0) for r in rows]


def _trade_records_for_summarize(labels: List[TradeLabel]) -> List[Dict[str, Any]]:
    """Adapt labels to the shape ``summarize()`` expects."""
    return [
        {"pnl": l.pnl, "win_probability": l.win_probability,
          "status": "closed" if l.win is not None else "open"}
        for l in labels
    ]


# ── endpoints ───────────────────────────────────────────────────────────────


def build_summary(limit: int = 5000,
                     live_only: bool = True) -> dict:
    """Pure summary builder — same shape ``/metrics/summary`` returns. Lets
    other routers (``/gates/status``) consume the summary in-process without
    re-entering the FastAPI request lifecycle.

    ``live_only=True`` (default) excludes the synthetic historical-replay
    corpus. The adaptive grade gate reads this summary to decide how
    strict to be; the synthetic outcomes carry probabilities from the
    heuristic ranker during replay, not from the live brain, so letting
    them drive the gate poisons the loop."""
    from backend.bot.gates.calibration_stability import stability_summary

    labels = _load_labels(limit=limit, include_synthetic=not live_only)
    curve = _equity_curve()
    metrics = summarize(_trade_records_for_summarize(labels), equity_curve=curve)
    data = metrics.to_dict()
    # Stage-11.8 — plumb calibration stability scalars so the gates
    # pipeline can pick them up without a second DB read.
    try:
        data.update(stability_summary(labels))
    except Exception:
        pass
    return {
        "data": data,
        "label_quality": label_quality(labels),
        "equity_points": len(curve),
    }


@router.get("/summary")
async def metrics_summary(limit: int = Query(5000, ge=10, le=20000),
                            include_synthetic: bool = Query(False)) -> dict:
    """Live-only by default; set ``include_synthetic=true`` to see the
    full corpus (useful for explaining the cohort/calibration pages)."""
    return build_summary(limit=limit, live_only=not include_synthetic)


def _by_cohort(labels: List[TradeLabel], key_fn) -> Dict[str, Dict[str, Any]]:
    """Bucket labels by cohort key and compute the same set of stats for each."""
    buckets: Dict[str, List[TradeLabel]] = defaultdict(list)
    for l in labels:
        buckets[str(key_fn(l) or "—")].append(l)
    out: Dict[str, Dict[str, Any]] = {}
    for key, items in buckets.items():
        pnls = [l.pnl for l in items if l.pnl is not None]
        out[key] = {
            "count": len(items),
            "closed": len(pnls),
            "win_rate": win_rate(pnls),
            "expectancy": expectancy(pnls),
            "profit_factor": (profit_factor(pnls) if pnls else None),
            "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
        }
    return out


@router.get("/by-strategy")
async def metrics_by_strategy(limit: int = Query(5000, ge=10, le=20000),
                                  include_synthetic: bool = Query(False)) -> dict:
    """Live-only by default — same reasoning as ``/portfolio/by-strategy``:
    a strategy P&L view that includes the historical-replay corpus would
    misrepresent the operator's account performance."""
    labels = _load_labels(limit=limit, include_synthetic=include_synthetic)
    return {
        "data": _by_cohort(labels, lambda l: l.strategy),
        "label_quality": label_quality(labels),
    }


@router.get("/by-grade")
async def metrics_by_grade(limit: int = Query(5000, ge=10, le=20000),
                              include_synthetic: bool = Query(False)) -> dict:
    labels = _load_labels(limit=limit, include_synthetic=include_synthetic)
    return {
        "data": _by_cohort(labels, lambda l: l.grade),
        "label_quality": label_quality(labels),
    }


@router.get("/by-regime")
async def metrics_by_regime(limit: int = Query(5000, ge=10, le=20000),
                                include_synthetic: bool = Query(False)) -> dict:
    labels = _load_labels(limit=limit, include_synthetic=include_synthetic)
    return {
        "data": _by_cohort(labels, lambda l: l.regime_trend),
        "label_quality": label_quality(labels),
    }


@router.get("/calibration")
async def metrics_calibration(n_bins: int = Query(10, ge=2, le=20),
                                 limit: int = Query(5000, ge=10, le=20000)) -> dict:
    from backend.bot.metrics import brier_score, calibration_error
    labels = _load_labels(limit=limit)
    pairs = [(l.win_probability, l.win) for l in labels
             if l.win_probability is not None and l.win is not None]
    preds = [p for p, _ in pairs]
    outs = [o for _, o in pairs]
    return {
        "data": calibration_curve(preds, outs, n_bins=n_bins),
        "sample_size": len(pairs),
        "brier": brier_score(preds, outs),
        "ece": calibration_error(preds, outs, n_bins=n_bins),
        "label_quality": label_quality(labels),
    }


@router.get("/walkforward")
async def metrics_walkforward(
    train_size: int = Query(100, ge=10, le=2000),
    test_size: int = Query(30, ge=5, le=1000),
    expanding: bool = Query(False),
    limit: int = Query(5000, ge=10, le=20000),
) -> dict:
    labels = _load_labels(limit=limit)
    result = walk_forward_evaluate(
        labels, train_size=train_size, test_size=test_size, expanding=expanding,
    )
    result["label_quality"] = label_quality(labels)
    return result


@router.get("/labels")
async def metrics_labels(limit: int = Query(200, ge=10, le=2000)) -> dict:
    """Raw labels — for debugging dataset shape. Capped at 2k."""
    labels = _load_labels(limit=limit)
    return {
        "labels": [l.to_dict() for l in labels[:limit]],
        "label_quality": label_quality(labels),
    }
