"""Stage-16 — forward-outcome backfill for RegimeEpisodeSnapshot.

Every snapshot row carries empty ``fwd_1d_return`` / ``fwd_5d_return`` /
``fwd_trades_*`` columns at write time. This module walks closed trades
and credits each snapshot with the *forward* statistics that occurred in
the window immediately after it was captured.

Heuristic mapping:
  • A trade ``T_i`` opened at time ``t_i`` and closed with pnl ``p_i`` is
    credited to every snapshot ``s_k`` whose ``timestamp`` falls in
    ``[t_i - window_back, t_i]`` (i.e. snapshots taken shortly *before*
    the trade fired).
  • ``fwd_1d_return`` is filled from the cycle's portfolio equity move
    over the next 1 trading day (via PortfolioSnapshot pairs); ``fwd_5d``
    over 5 days. Both stay None when not enough equity history exists.

Idempotent — safe to call repeatedly. Only writes columns when the new
value differs from what's stored (so re-running doesn't churn the DB).

Pure / no API calls. Run from the scheduler nightly (or on demand).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.db import session_scope
from backend.models.regime_episode import RegimeEpisodeSnapshot
from backend.models.snapshot import PortfolioSnapshot
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# How far back from a trade's open timestamp do we look for snapshots that
# "should have foreseen it"? 60 minutes by default — every 15-min snapshot
# in the hour leading up to the trade gets credit.
_CREDIT_WINDOW_MIN = 60


def _equity_move_over(start_ts, days: int,
                          equity_rows: List[Dict[str, Any]]) -> Optional[float]:
    """Find the equity-curve points nearest to start_ts and start_ts+days,
    return the % move between them. Returns None when no later point exists."""
    target = start_ts + timedelta(days=days)
    # equity_rows is sorted by ts asc. Find the closest at-or-after start_ts
    # and at-or-after target.
    start_val = None
    end_val = None
    for r in equity_rows:
        if start_val is None and r["timestamp"] >= start_ts:
            start_val = r["portfolio_value"]
            continue
        if start_val is not None and r["timestamp"] >= target:
            end_val = r["portfolio_value"]
            break
    if not start_val or not end_val:
        return None
    if start_val == 0:
        return None
    return round((end_val - start_val) / start_val, 4)


def backfill_forward_outcomes(*,
                                  credit_window_min: int = _CREDIT_WINDOW_MIN,
                                  fwd_days: List[int] = (1, 5),
                                  limit_snapshots: int = 5000,
                                  ) -> Dict[str, Any]:
    """Walk every snapshot in the most recent ``limit_snapshots`` and
    backfill its forward stats.

    Returns ``{snapshots_scanned, snapshots_updated, trades_credited,
    equity_rows}`` so callers + the scheduler can log progress.
    """
    updated = 0
    credited_trades = 0

    try:
        with session_scope() as session:
            # Load snapshots oldest-first so the "next N days" lookup works.
            snaps = list(session.execute(
                select(RegimeEpisodeSnapshot)
                .order_by(RegimeEpisodeSnapshot.timestamp.asc())
                .limit(limit_snapshots)
            ).scalars().all())
            if not snaps:
                return {"snapshots_scanned": 0, "snapshots_updated": 0,
                          "trades_credited": 0, "equity_rows": 0}

            min_ts = snaps[0].timestamp - timedelta(minutes=credit_window_min)
            max_ts = snaps[-1].timestamp + timedelta(days=max(fwd_days) + 1)

            # Equity curve (used for fwd_Nd_return).
            equity = list(session.execute(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.timestamp >= min_ts)
                .where(PortfolioSnapshot.timestamp <= max_ts)
                .order_by(PortfolioSnapshot.timestamp.asc())
            ).scalars().all())
            equity_rows = [{"timestamp": e.timestamp,
                              "portfolio_value": float(e.portfolio_value or 0.0)}
                             for e in equity]

            # Closed trades inside the relevant window.
            trades = list(session.execute(
                select(Trade)
                .where(Trade.pnl.is_not(None))
                .where(Trade.timestamp >= min_ts)
                .where(Trade.timestamp <= max_ts)
                .order_by(Trade.timestamp.asc())
            ).scalars().all())
            # Project to plain dicts to drop the session dependency.
            trade_rows = [{"id": t.id, "timestamp": t.timestamp,
                              "pnl": float(t.pnl or 0.0),
                              "pnl_positive": float(t.pnl or 0.0) > 0}
                            for t in trades]

            # Index trades into 15-min buckets by ts so the credit window
            # lookup is O(W) per snapshot rather than O(N).
            buckets: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
            for t in trade_rows:
                key = int(t["timestamp"].timestamp() // 900)   # 15-min bin
                buckets[key].append(t)

            for snap in snaps:
                # Trades that fall in the credit window after this snapshot
                lo_key = int(snap.timestamp.timestamp() // 900)
                hi_key = int(
                    (snap.timestamp + timedelta(minutes=credit_window_min))
                    .timestamp() // 900
                ) + 1
                attributed: List[Dict[str, Any]] = []
                for k in range(lo_key, hi_key + 1):
                    for t in buckets.get(k, ()):
                        if snap.timestamp <= t["timestamp"] \
                                <= snap.timestamp + timedelta(minutes=credit_window_min):
                            attributed.append(t)

                fwd_trades_count = len(attributed)
                fwd_trades_wins = sum(1 for t in attributed if t["pnl_positive"])
                fwd_trades_pnl = round(sum(t["pnl"] for t in attributed), 2)

                fwd_returns: Dict[int, Optional[float]] = {
                    d: _equity_move_over(snap.timestamp, d, equity_rows)
                    for d in fwd_days
                }

                # Only write when something actually changed (idempotent).
                dirty = False
                if snap.fwd_trades_count != fwd_trades_count:
                    snap.fwd_trades_count = fwd_trades_count
                    dirty = True
                if snap.fwd_trades_wins != fwd_trades_wins:
                    snap.fwd_trades_wins = fwd_trades_wins
                    dirty = True
                if snap.fwd_trades_pnl != fwd_trades_pnl:
                    snap.fwd_trades_pnl = fwd_trades_pnl
                    dirty = True
                for d, v in fwd_returns.items():
                    col = f"fwd_{d}d_return"
                    if hasattr(snap, col) and getattr(snap, col) != v:
                        setattr(snap, col, v)
                        dirty = True
                if dirty:
                    updated += 1
                    credited_trades += fwd_trades_count

        return {
            "snapshots_scanned": len(snaps),
            "snapshots_updated": updated,
            "trades_credited": credited_trades,
            "equity_rows": len(equity_rows),
        }
    except Exception:
        logger.exception("backfill_forward_outcomes failed")
        return {"snapshots_scanned": 0, "snapshots_updated": 0,
                  "trades_credited": 0, "equity_rows": 0,
                  "error": "exception"}
