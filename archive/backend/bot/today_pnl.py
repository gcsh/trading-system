"""Unified today-P&L helper — the single source of truth for "what is today
worth?"

Three surfaces used to compute today's P&L differently:

* ``/portfolio/performance.pnl_today`` ran an intraday-snapshot swing.
* ``/bot/status.daily_pnl`` returned the engine's in-memory cycle counter
  (which resets to 0 every restart).
* ``/today/summary`` didn't exist; a DB-snapshot sum returned 0.

When the three disagreed the cockpit displayed three different "today" numbers
side-by-side. This module collapses them onto one definition so every caller
gets the same answer.

Definition
----------
``realized_today``    Sum of ``Trade.pnl`` for rows where
                      ``status == 'closed'`` AND
                      ``date(timestamp) == today (UTC)`` AND
                      ``source_kind IN ('live', NULL)`` (synthetic backfill
                      rows are excluded — they don't reflect live P&L).

``unrealized_today``  Live mark-to-market of open paper positions minus the
                      cost basis snapshot at start-of-day. Computed from
                      ``PaperPosition`` + last-known marks. NULL-safe — a
                      missing live mark falls back to entry mid (zero MTM
                      contribution) rather than poisoning the total.

``total_today``       ``realized_today + unrealized_today``.

The helper is read-only and never writes. Callers wrap it in their own
``session_scope`` (or use the shared ``compute_today_pnl_with_session`` helper
that opens one for them)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func, or_

from backend.db import session_scope
from backend.models.paper import PaperPosition
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


def _utc_today_start() -> datetime:
    """Start-of-day in UTC. We use UTC throughout the DB so this matches
    ``Trade.timestamp`` (server timestamp on insert)."""
    today = datetime.utcnow().date()
    return datetime.combine(today, time.min)


def _realized_today(session) -> float:
    """Sum of closed-trade pnl for trades that closed today (live rows only).

    The ``source_kind`` filter mirrors the learning-layer convention: live
    rows are either explicitly tagged ``'live'`` or pre-migration ``NULL``;
    ``synthetic_backfill`` rows are excluded so a replay run never inflates
    the live "today P&L"."""
    start = _utc_today_start()
    total = session.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
        Trade.status == "closed",
        Trade.timestamp >= start,
        or_(
            Trade.source_kind == "live",
            Trade.source_kind.is_(None),
        ),
    ).scalar()
    return float(total or 0.0)


def _unrealized_today(session) -> float:
    """Net MTM of open positions vs their entry cost basis.

    We compare current mark (best available — stored_iv-based reprice or
    last-known mark in ``PaperPosition.meta['current_price']``) against entry
    ``avg_cost``. When a live mark isn't available we treat unrealized as 0
    rather than guessing — this keeps the total trustworthy on a degraded
    quote feed."""
    rows = session.query(PaperPosition).all()
    total = 0.0
    for row in rows:
        meta = {}
        if row.meta:
            try:
                meta = json.loads(row.meta) if isinstance(row.meta, str) else (row.meta or {})
            except Exception:
                meta = {}
        if (row.kind or "stock") == "stock":
            # Stock: use stored mark if present, else skip (no live price
            # → no defensible unrealized estimate).
            current = meta.get("current_price") or meta.get("mark")
            if current is None:
                continue
            try:
                current_f = float(current)
            except (TypeError, ValueError):
                continue
            qty = float(row.quantity or 0)
            avg_cost = float(row.avg_cost or 0)
            total += (current_f - avg_cost) * qty
        elif (row.kind or "") == "option":
            # Option: row.avg_cost is per-contract (premium × 100, signed).
            # ``stored_iv``-reprice mid lives on meta['mark'] / ['mark_per_share']
            # when the MTM cycle has run; without it we skip rather than
            # guess.
            current = (
                meta.get("mark_per_share")
                or meta.get("mark")
                or meta.get("current_price")
            )
            if current is None:
                continue
            try:
                current_per_share = float(current)
            except (TypeError, ValueError):
                continue
            contracts = float(row.quantity or 0)    # signed; long > 0, short < 0
            entry_per_share = abs(float(row.avg_cost or 0)) / 100.0
            # PnL per share × 100 × contracts (signed).
            total += (current_per_share - entry_per_share) * 100.0 * contracts
    return float(total)


def compute_today_pnl(session) -> Dict[str, float]:
    """Return today's realized + unrealized P&L from the canonical source.

    Callers pass an open SQLAlchemy session so this helper participates in
    their existing read transaction. Always returns three floats, never
    raises on individual-row failures (warn-logs instead so a bad position
    doesn't blank the whole UI tile)."""
    try:
        realized = _realized_today(session)
    except Exception:
        logger.warning("realized_today compute failed", exc_info=True)
        realized = 0.0
    try:
        unrealized = _unrealized_today(session)
    except Exception:
        logger.warning("unrealized_today compute failed", exc_info=True)
        unrealized = 0.0
    return {
        "realized_today": round(float(realized), 2),
        "unrealized_today": round(float(unrealized), 2),
        "total_today": round(float(realized + unrealized), 2),
    }


def compute_today_pnl_with_session() -> Dict[str, float]:
    """Convenience entry point — opens its own session.

    Use this from routes that don't already have a session in hand."""
    with session_scope() as session:
        return compute_today_pnl(session)
