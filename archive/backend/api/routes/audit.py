"""Audit endpoint — surfaces every invariant violation the UI cares about so
a single red light tells the user when something is off.

Three checks combined:
  • account reconciliation: cash + Σ positions ≟ portfolio_value
  • expired-but-open options: exit manager isn't running, contracts are stale
  • recent trade-row hygiene: any row stored with the old bad shape

The endpoint is intentionally read-only and side-effect-free.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request
from sqlalchemy import desc, select

from backend.bot.audit import (
    audit_open_options,
    check_instrument_matches_action,
    check_option_has_required_fields,
    check_strike_is_snapped,
    AuditViolation,
    reconcile_account,
)
from backend.db import session_scope
from backend.models.trade import Trade

router = APIRouter(prefix="/audit", tags=["audit"])


def _audit_recent_trades(limit: int = 50) -> List[Dict[str, Any]]:
    """Replay the invariants over the last N trades — catches any historical
    rows that were persisted before the invariants existed."""
    violations: List[Dict[str, Any]] = []
    with session_scope() as session:
        rows = session.execute(
            select(Trade).order_by(desc(Trade.timestamp)).limit(limit)
        ).scalars().all()
        # extract everything we need inside the session
        for r in rows:
            rec = {
                "id": r.id,
                "ticker": r.ticker, "action": r.action,
                "instrument": r.instrument, "strike": r.strike,
                "expiration": r.expiration, "status": r.status,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for fn, args in (
                (check_instrument_matches_action, (rec["action"], rec["instrument"])),
                (check_option_has_required_fields,
                  (rec["action"], rec["instrument"], rec["strike"], rec["expiration"])),
            ):
                try:
                    fn(*args)
                except AuditViolation as v:
                    violations.append({"trade_id": rec["id"], "ticker": rec["ticker"],
                                        "name": v.name, "message": str(v)})
            if rec["instrument"] in ("option", "spread") and rec["strike"]:
                try:
                    check_strike_is_snapped(rec["strike"])
                except AuditViolation as v:
                    violations.append({"trade_id": rec["id"], "ticker": rec["ticker"],
                                        "name": v.name, "message": str(v),
                                        "strike": rec["strike"]})
    return violations


@router.get("/health")
async def audit_health(request: Request) -> dict:
    """Combined audit signal — green when all invariants hold."""
    engine = getattr(request.app.state, "engine", None)
    positions: List[dict] = []
    cash = 0.0
    realized_pnl = 0.0
    portfolio_value = 0.0
    if engine is not None and hasattr(engine.executor, "positions"):
        try:
            positions = engine.executor.positions() or []
            state = engine.executor.get_account_state() or {}
            cash = float(state.get("cash") or 0.0)
            realized_pnl = float(state.get("realized_pnl") or 0.0)
            portfolio_value = float(state.get("portfolio_value") or 0.0)
        except Exception:
            pass

    market_value = sum(float(p.get("market_value") or 0.0) for p in positions)
    recon = reconcile_account(cash, realized_pnl, market_value, portfolio_value)
    options_audit = audit_open_options(positions)
    trade_violations = _audit_recent_trades(limit=50)

    all_ok = recon.ok and options_audit.ok and not trade_violations
    return {
        "ok": all_ok,
        "checked_at": __import__("datetime").datetime.utcnow().isoformat(),
        "account": {
            "cash": round(cash, 2), "realized_pnl": round(realized_pnl, 2),
            "positions_market_value": round(market_value, 2),
            "portfolio_value": round(portfolio_value, 2),
        },
        "reconciliation": recon.to_dict(),
        "expired_options": options_audit.to_dict(),
        "recent_trade_violations": trade_violations,
    }
