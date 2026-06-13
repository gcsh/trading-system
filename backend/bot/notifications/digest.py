"""End-of-day digest builder.

Fires at 16:30 ET on weekdays. The digest is a single Telegram message
summarizing the trading day:

  • # trades fired today, win/loss split
  • realized P&L today + open-position unrealized P&L estimate
  • open positions (compact one-line each)
  • top 3 alerts of the day by severity

Always renders something — a quiet day shows "no trades · 0 alerts" so
the operator can confirm the digest job itself is alive.

Side effects: none. Reads the SQLite tables; the route layer ships the
output via the notifier.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.bot.notifications.formatters import (
    TARGET_CHARS,
    _esc,
    _fmt_money,
    _truncate,
)
from backend.db import session_scope
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


def _today_window() -> tuple[datetime, datetime]:
    """UTC window that brackets "today" (midnight → midnight).

    Returns naive UTC datetimes to match the storage convention.
    """
    now = datetime.utcnow()
    start = datetime.combine(now.date(), time(0, 0))
    return start, start + timedelta(days=1)


def _trades_today() -> List[Dict[str, Any]]:
    """Pull today's trades into plain dicts (session-detached)."""
    start, end = _today_window()
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Trade)
                .where(Trade.timestamp >= start)
                .where(Trade.timestamp < end)
                .order_by(Trade.timestamp.asc())
            ).scalars()
        )
        # Materialize into plain dicts so callers can use the data
        # outside the session_scope without DetachedInstanceError.
        return [{
            "ticker": t.ticker,
            "action": t.action,
            "pnl": float(t.pnl) if t.pnl is not None else None,
            "status": t.status,
            "timestamp": (t.timestamp.isoformat()
                            if t.timestamp else None),
        } for t in rows]


def _classify_pnl(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Bucket the day's closed trades into wins, losses, scratch."""
    wins = losses = scratches = 0
    realized = 0.0
    best: Optional[Dict[str, Any]] = None
    worst: Optional[Dict[str, Any]] = None
    for t in trades:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        realized += float(pnl)
        if pnl > 0:
            wins += 1
            if best is None or float(pnl) > float(best.get("pnl") or 0):
                best = t
        elif pnl < 0:
            losses += 1
            if worst is None or float(pnl) < float(worst.get("pnl") or 0):
                worst = t
        else:
            scratches += 1
    return {
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "realized": realized,
        "best": best,
        "worst": worst,
    }


def _open_positions() -> List[Dict[str, Any]]:
    """Pull the current open positions from the paper executor when active.

    Returns [] if the active broker isn't paper (we don't query Alpaca
    here — would need an HTTP call that could fail and stall the digest).
    """
    try:
        from backend.models.paper import PaperPosition
        with session_scope() as s:
            rows = list(s.query(PaperPosition).all())
            return [
                {
                    "ticker": p.ticker,
                    "quantity": float(p.quantity or 0),
                    "average_price": float(getattr(p, "avg_cost", 0) or 0),
                    "instrument": getattr(p, "kind", "stock") or "stock",
                }
                for p in rows
            ]
    except Exception:
        logger.debug("digest: open-positions lookup failed", exc_info=True)
        return []


def _recent_alerts(limit: int = 50) -> List[Dict[str, Any]]:
    """Pull the most recent alerts from the in-memory ALERT_CENTER."""
    try:
        from backend.bot.alerts import ALERT_CENTER
        return ALERT_CENTER.recent(limit=limit)
    except Exception:
        return []


def _top_alerts(rows: List[Dict[str, Any]], k: int = 3) -> List[Dict[str, Any]]:
    """Pick the K most-severe alerts (ties broken by recency)."""
    order = {
        "critical": 4, "danger": 3, "warning": 2,
        "success": 1, "info": 0,
    }
    ranked = sorted(
        rows,
        key=lambda r: (order.get((r.get("severity") or "info").lower(), 0),
                          r.get("timestamp") or ""),
        reverse=True,
    )
    return ranked[:k]


def build_eod_digest(*, snapshot_date: Optional[date] = None) -> str:
    """Compose the end-of-day digest string. Always returns valid HTML.

    ``snapshot_date`` is mostly here for tests; production callers leave
    it None and we use today's date.
    """
    snapshot_date = snapshot_date or date.today()
    trades = _trades_today()
    bucket = _classify_pnl(trades)
    positions = _open_positions()
    alerts = _recent_alerts()
    top = _top_alerts(alerts, k=3)

    fired_count = len(trades)
    realized_money = _fmt_money(bucket["realized"])

    head = (
        f"<b>End-of-day digest · {_esc(snapshot_date.isoformat())}</b>"
    )

    if fired_count == 0 and not positions and not alerts:
        return _truncate(
            f"{head}\n\n"
            "Quiet day: no trades, no open positions, no alerts.",
            target=TARGET_CHARS,
        )

    sections: List[str] = [head]

    # Trades + realized P&L.
    trade_line = (
        f"<b>Trades:</b> {fired_count} fired · "
        f"{bucket['wins']}W / {bucket['losses']}L / "
        f"{bucket['scratches']} scratch · "
        f"realized <b>{_esc(realized_money)}</b>"
    )
    sections.append(trade_line)

    # Best / worst.
    extras: List[str] = []
    if bucket["best"] is not None:
        extras.append(
            f"best: <b>{_esc(bucket['best']['ticker'])}</b> "
            f"{_esc(_fmt_money(bucket['best']['pnl']))}"
        )
    if bucket["worst"] is not None:
        extras.append(
            f"worst: <b>{_esc(bucket['worst']['ticker'])}</b> "
            f"{_esc(_fmt_money(bucket['worst']['pnl']))}"
        )
    if extras:
        sections.append(" · ".join(extras))

    # Open positions.
    if positions:
        pos_lines = []
        for p in positions[:10]:
            pos_lines.append(
                f"  <code>{_esc(p['ticker'])}</code> · "
                f"{_esc(p['instrument'])} · "
                f"qty {_esc(p['quantity'])} @ "
                f"{_esc(_fmt_money(p['average_price']))}"
            )
        if len(positions) > 10:
            pos_lines.append(f"  … (+{len(positions) - 10} more)")
        sections.append(
            f"<b>Open positions ({len(positions)}):</b>\n"
            + "\n".join(pos_lines)
        )
    else:
        sections.append("<b>Open positions:</b> none")

    # Top alerts.
    if top:
        alert_lines = []
        for a in top:
            sev = (a.get("severity") or "info").upper()
            title = _esc(a.get("title") or "(no title)")
            alert_lines.append(f"  <i>[{_esc(sev)}]</i> {title}")
        sections.append(
            f"<b>Top alerts:</b>\n" + "\n".join(alert_lines)
        )

    return _truncate("\n\n".join(sections), target=TARGET_CHARS)


__all__ = ["build_eod_digest"]
