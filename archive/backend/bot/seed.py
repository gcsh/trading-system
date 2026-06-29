"""Deterministic demo data for tests + on-demand exploration.

`seed_demo` inserts a fixed, known set of paper trades (wins, losses, an open
position, a closed option) so the Trades page, summary stats and the per-trade
drill-in can be exercised against EXACT values — the foundation for real data
testing instead of whatever the live market happens to produce.

Idempotent: it stamps each demo row with ``signal_source="demo_seed"`` and skips
if demo rows already exist. ``clear_demo`` removes only the demo rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models.trade import Trade

DEMO_SOURCE = "demo_seed"

# (ticker, action, qty, price, strategy, confidence, pnl, status, instrument, extra)
_DEMO_TRADES = [
    ("AAPL", "BUY_STOCK", 10, 180.0, "trend_pullback", 0.72, 240.0, "closed", "stock", {}),
    ("TSLA", "BUY_STOCK", 5, 250.0, "macd_momentum", 0.66, -120.0, "closed", "stock", {}),
    ("NVDA", "BUY_STOCK", 8, 120.0, "ai_brain", 0.81, 560.0, "closed", "stock", {}),
    ("SPY", "BUY_CALL", 2, 4.50, "zero_dte_scalp", 0.6, -90.0, "closed", "option",
     {"option_type": "call", "strike": 755.0, "expiration": "2026-06-19", "contracts": 2}),
    ("QQQ", "BUY_STOCK", 6, 470.0, "ai_brain", 0.7, None, "open", "stock", {}),
]


def has_demo(session: Session) -> bool:
    return session.execute(
        select(Trade.id).where(Trade.signal_source == DEMO_SOURCE).limit(1)
    ).first() is not None


def clear_demo(session: Session) -> int:
    result = session.execute(delete(Trade).where(Trade.signal_source == DEMO_SOURCE))
    session.commit()
    return result.rowcount or 0


def seed_demo(session: Session, *, force: bool = False) -> List[int]:
    """Insert the demo trades. Returns the new row ids. No-op if already seeded."""
    if has_demo(session) and not force:
        return []
    base = datetime.utcnow() - timedelta(days=len(_DEMO_TRADES))
    ids: List[int] = []
    for i, (ticker, action, qty, price, strategy, conf, pnl, status, instrument, extra) in enumerate(_DEMO_TRADES):
        row = Trade(
            timestamp=base + timedelta(days=i, hours=1),
            ticker=ticker,
            action=action,
            quantity=float(qty),
            price=float(price),
            strategy=strategy,
            signal_source=DEMO_SOURCE,
            confidence=conf,
            reason=f"[{strategy}] demo trade for {ticker} — deterministic seed for testing.",
            paper=1,
            pnl=pnl,
            status=status,
            instrument=instrument,
            option_type=extra.get("option_type"),
            strike=extra.get("strike"),
            expiration=extra.get("expiration"),
            contracts=extra.get("contracts"),
            detail_json=json.dumps({"signal_reason": f"demo {strategy} entry on {ticker}", "seed": True}),
        )
        session.add(row)
        session.flush()
        ids.append(row.id)
    session.commit()
    return ids


if __name__ == "__main__":  # pragma: no cover - manual seeding
    from backend.db import init_db, session_scope

    init_db()
    with session_scope() as s:
        new_ids = seed_demo(s, force=False)
    print(f"seeded demo trades: {new_ids or 'already present'}")
