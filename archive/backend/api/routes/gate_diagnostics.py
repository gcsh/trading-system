"""Gate-stack diagnostics — P1.12.

Aggregates the last N cycles of decision_log rows by status so the
operator can answer "why didn't I trade today?" in seconds.

Without this surface, gate-poisoning bugs (like the 2026-06-03 incident
where the adaptive calibration gate locked at A+ and rejected every
signal as ``low_grade``) require a database query to diagnose. The
trial-monitoring UI calls this every cycle.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog

router = APIRouter(prefix="/gates", tags=["gates"])


@router.get("/stack")
async def gate_stack(
    hours: int = Query(24, ge=1, le=168,
                            description="Window in hours, default 24h."),
    include_synthetic: bool = Query(False),
) -> Dict[str, Any]:
    """Returns counts of decisions bucketed by status, plus the most
    recent rejected decisions for drill-down.

    Live-only by default — synthetic-replay rows would inflate the
    counts and obscure the live gate behavior.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with session_scope() as session:
        stmt = (
            select(DecisionLog)
            .where(DecisionLog.timestamp >= cutoff)
        )
        if not include_synthetic:
            stmt = stmt.where(
                DecisionLog.signal_source != "historical_replay"
            )
        rows = list(session.execute(
            stmt.order_by(desc(DecisionLog.timestamp))
        ).scalars().all())
        # Eager-extract while session is open.
        snap = [(r.timestamp, r.ticker, r.strategy, r.action,
                  r.status, r.confidence, r.signal_source) for r in rows]

    status_counts = Counter(r[4] for r in snap)
    submitted_count = status_counts.get("submitted", 0)
    closed_count = status_counts.get("closed", 0)
    # "Rejected" buckets = everything that didn't make it to an order.
    rejected_buckets = {
        status: count for status, count in status_counts.items()
        if status not in ("submitted", "closed", "historical_replay_closed",
                              "signal_only")
    }
    # Recent rejections for drill-down (cap at 30).
    recent_rejections = [
        {
            "timestamp": ts.isoformat() if ts else None,
            "ticker": ticker, "strategy": strategy, "action": action,
            "status": status, "confidence": confidence,
            "signal_source": src,
        }
        for ts, ticker, strategy, action, status, confidence, src in snap[:30]
        if status in rejected_buckets
    ]
    return {
        "window_hours": hours,
        "total_decisions": len(snap),
        "submitted": submitted_count,
        "closed": closed_count,
        "rejection_counts": rejected_buckets,
        "recent_rejections": recent_rejections,
        "include_synthetic": include_synthetic,
    }
