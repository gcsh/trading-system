"""Trade history endpoints. P&L is persisted on each row by the engine."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from backend.db import session_scope
from backend.models.trade import Trade

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("/list")
async def list_trades(limit: int = 200, include_resets: bool = False,
                          include_synthetic: bool = False) -> List[dict]:
    """List trades. Hides ``closed_by_reset`` and synthetic
    ``historical_replay`` rows by default so the Trades page only shows
    real account activity. Set the corresponding flag to inspect the
    full audit trail."""
    with session_scope() as session:
        stmt = select(Trade)
        if not include_resets:
            stmt = stmt.where(Trade.status != "closed_by_reset")
        if not include_synthetic:
            stmt = stmt.where(Trade.signal_source != "historical_replay")
        rows = (
            session.execute(stmt.order_by(Trade.timestamp.desc()).limit(limit))
            .scalars()
            .all()
        )
        return [r.to_dict() for r in rows]


@router.get("/summary")
async def summary() -> dict:
    # Pull the values we need INSIDE the session scope — once the block exits the
    # ORM instances are detached and attribute access raises DetachedInstanceError
    # (only reachable when trades exist, so it hid behind empty data).
    #
    # Excludes ``closed_by_reset`` (Bug fix 2026-06-02): administrative
    # resets from ``soft_reset`` get pnl=0 and inflate stats.
    #
    # Issue 11d — dual surface. The historical "trade_count" filtered out
    # synthetic ``historical_replay`` rows but never told the UI which it
    # was, so reading ``trade_count`` blind made it impossible to know if
    # the discrepancy with the synthetic-included view was a bug or a
    # filter. We now return BOTH counts explicitly:
    #   live_trade_count   — real engine fills + paper trades
    #   total_trade_count  — including synthetic historical_replay rows
    # ``trade_count`` is kept as an alias for backwards compatibility.
    with session_scope() as session:
        live_rows = (
            session.execute(
                select(Trade)
                .where(Trade.status != "closed_by_reset")
                .where(Trade.signal_source != "historical_replay")
            )
            .scalars()
            .all()
        )
        live_count = len(live_rows)
        total_count = session.query(Trade).filter(
            Trade.status != "closed_by_reset"
        ).count()
        closed = [r.pnl for r in live_rows if r.pnl is not None]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p < 0]
    total_pnl = sum(closed)
    avg_gain = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(closed) if closed else 0.0
    return {
        "live_trade_count": live_count,
        "total_trade_count": total_count,
        # deprecated alias — remove in next major
        "trade_count": live_count,
        "closed_count": len(closed),
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "avg_gain": round(avg_gain, 2),
        "avg_loss": round(avg_loss, 2),
    }


# Declared after /summary so the literal route wins over the int matcher.
@router.get("/{trade_id}")
async def get_trade(trade_id: int) -> dict:
    with session_scope() as session:
        row = session.get(Trade, trade_id)
        if row is None:
            raise HTTPException(status_code=404, detail="trade not found")
        return row.to_dict()


@router.get("/{trade_id}/detail")
async def trade_detail(trade_id: int) -> dict:
    """Everything the UI needs to render a trade-detail drawer in one round
    trip: the trade itself, its decision-log context (regime/grade/analytics),
    the linked execution row, and the realized outcome if closed."""
    import json as _json

    from backend.models.decision_log import DecisionLog
    from backend.models.execution_log import ExecutionLog

    with session_scope() as session:
        row = session.get(Trade, trade_id)
        if row is None:
            raise HTTPException(status_code=404, detail="trade not found")
        trade = row.to_dict()

        # Decision-log row — the analytics context recorded at signal time.
        decision_row = session.execute(
            select(DecisionLog).where(DecisionLog.trade_id == trade_id)
        ).scalar_one_or_none()
        decision = None
        features = None
        if decision_row is not None:
            decision = {
                "id": decision_row.id,
                "timestamp": decision_row.timestamp.isoformat() if decision_row.timestamp else None,
                "regime_trend": decision_row.regime_trend,
                "regime_volatility": decision_row.regime_volatility,
                "regime_gamma": decision_row.regime_gamma,
                "regime_label": decision_row.regime_label,
                "grade": decision_row.grade,
                "win_probability": decision_row.win_probability,
                "status": decision_row.status,
                "outcome_pnl": decision_row.outcome_pnl,
                "outcome_status": decision_row.outcome_status,
            }
            try:
                features = _json.loads(decision_row.features_json or "{}")
            except Exception:
                features = None

        # Execution-log row — slippage telemetry, if we recorded one.
        execution_row = session.execute(
            select(ExecutionLog).where(ExecutionLog.trade_id == trade_id)
        ).scalar_one_or_none()
        execution = None
        if execution_row is not None:
            execution = {
                "expected_price": execution_row.expected_price,
                "fill_price": execution_row.fill_price,
                "slippage_bps": execution_row.slippage_bps,
                "is_adverse": execution_row.is_adverse,
                "side": execution_row.side,
            }

    # Replay invariants against this single row so the UI can show a per-trade
    # "audit clean / has violations" pill in the drawer.
    from backend.bot.audit import (
        AuditViolation,
        check_instrument_matches_action,
        check_option_has_required_fields,
        check_strike_is_snapped,
    )
    audit_violations: list = []
    for fn, args in (
        (check_instrument_matches_action, (trade.get("action"), trade.get("instrument"))),
        (check_option_has_required_fields,
          (trade.get("action"), trade.get("instrument"), trade.get("strike"),
           trade.get("expiration"))),
    ):
        try:
            fn(*args)
        except AuditViolation as v:
            audit_violations.append({"name": v.name, "message": str(v)})
    if trade.get("instrument") in ("option", "spread") and trade.get("strike"):
        try:
            check_strike_is_snapped(trade["strike"])
        except AuditViolation as v:
            audit_violations.append({"name": v.name, "message": str(v),
                                      "strike": trade.get("strike")})

    return {
        "trade": trade,
        "decision": decision,
        "features": features,
        "execution": execution,
        "audit": {"ok": not audit_violations, "violations": audit_violations},
    }
