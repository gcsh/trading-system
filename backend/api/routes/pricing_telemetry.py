"""P4.1 — Pricing telemetry: what fraction of fills used real chain data?"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.trade import Trade

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/telemetry")
async def telemetry(
    hours: int = Query(168, ge=1, le=24 * 30,
                            description="Window in hours, default 7 days"),
) -> Dict[str, Any]:
    """Returns the breakdown of Trade.pricing_source over the window,
    excluding the synthetic backfill corpus."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with session_scope() as session:
        rows = list(session.execute(
            select(Trade)
            .where(Trade.timestamp >= cutoff)
            .where(Trade.status != "closed_by_reset")
            .where(Trade.signal_source != "historical_replay")
            .order_by(desc(Trade.timestamp))
        ).scalars().all())
        snap = [(r.timestamp, r.strategy, r.pricing_source,
                  r.accounting_version, r.instrument) for r in rows]

    source_counts = Counter(r[2] or "unknown" for r in snap)
    by_strategy: Dict[str, Counter] = {}
    for ts, strategy, src, ver, inst in snap:
        key = strategy or "—"
        by_strategy.setdefault(key, Counter())[src or "unknown"] += 1
    accounting_versions = Counter(r[3] or 1 for r in snap)
    by_instrument: Dict[str, Counter] = {}
    for ts, strategy, src, ver, inst in snap:
        key = inst or "—"
        by_instrument.setdefault(key, Counter())[src or "unknown"] += 1
    return {
        "window_hours": hours,
        "total_trades": len(snap),
        "by_source": dict(source_counts),
        "by_strategy_source": {
            k: dict(v) for k, v in by_strategy.items()
        },
        "by_instrument_source": {
            k: dict(v) for k, v in by_instrument.items()
        },
        "by_accounting_version": dict(accounting_versions),
    }
