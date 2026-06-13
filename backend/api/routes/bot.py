"""Bot lifecycle endpoints: start, stop, status."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/bot", tags=["bot"])


@router.post("/start")
async def start(request: Request) -> dict:
    engine = request.app.state.engine
    # Rebuild executor in case the user changed the broker in the UI.
    from backend.db import session_scope
    from backend.main import _build_executor
    from backend.models.config import load_config

    engine.executor = _build_executor()
    with session_scope() as session:
        cfg = load_config(session)
    interval = float(cfg.get("live_interval_sec", 30) or 30)
    engine.start_live_loop(interval_sec=interval)
    return {
        "running": engine.status.running,
        "strategy": engine.status.active_strategy,
        "broker": engine.executor.__class__.__name__,
        "interval_sec": interval,
    }


@router.post("/stop")
async def stop(request: Request) -> dict:
    engine = request.app.state.engine
    engine.stop()
    return {"running": engine.status.running}


@router.get("/status")
async def status(request: Request) -> dict:
    engine = request.app.state.engine
    # Issue 11c — daily_pnl was the engine's in-memory cycle counter (resets
    # to 0 on every restart and never matched the dashboard tile). Use the
    # unified today-P&L helper so /bot/status agrees with /portfolio and
    # /today/summary.
    daily_pnl = float(engine.status.daily_pnl or 0.0)
    try:
        from backend.bot.today_pnl import compute_today_pnl_with_session
        _tp = compute_today_pnl_with_session()
        daily_pnl = float(_tp.get("total_today") or 0.0)
    except Exception:
        # Fall back to the legacy cycle counter rather than blanking the tile.
        pass
    return {
        "running": engine.status.running,
        "strategy": engine.status.active_strategy,
        "market_regime": getattr(engine.status, "market_regime", None),
        "intraday_regime": getattr(engine.status, "intraday_regime", "normal"),
        "day_plan": getattr(engine.status, "day_plan", None),
        "daily_pnl": daily_pnl,
        "cycles": engine.status.cycles,
        "last_cycle_at": engine.status.last_cycle_at,
        "recent_signals": engine.status.recent_signals[-20:],
        "broker": engine.executor.__class__.__name__ if engine.executor else None,
        "live_loop_running": (
            engine._live_task is not None and not engine._live_task.done()
        ),
    }


@router.post("/run-cycle")
async def run_cycle(request: Request) -> dict:
    """Manual one-shot trigger for the engine loop. Useful for debugging."""
    engine = request.app.state.engine
    events = engine.run_cycle()
    return {"events": events}


@router.post("/force-trade")
async def force_trade(request: Request, payload: dict | None = None) -> dict:
    """Bypass confidence + auto-exec gates and submit the best signal *now*.

    Optional payload: ``{"ticker": "AAPL"}`` to focus on one ticker only.

    Use this when you want to confirm the executor + broker are wired up by
    forcing at least one paper order through. Risk-manager checks still apply
    (buying power, daily loss, max positions).
    """
    from backend.bot.risk import AccountState, RiskManager
    from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY
    from backend.bot.strategies.base import Action
    from backend.db import session_scope
    from backend.models.config import load_config

    engine = request.app.state.engine
    with session_scope() as session:
        config = load_config(session)
    target_ticker = (payload or {}).get("ticker")
    tickers = [target_ticker] if target_ticker else (config.get("tickers") or [])
    if not tickers:
        return {"error": "no tickers configured"}

    risk = RiskManager(config)
    account_dict = engine.executor.get_account_state()
    account = AccountState(
        buying_power=float(account_dict.get("buying_power", 0.0)),
        portfolio_value=float(account_dict.get("portfolio_value", 0.0)),
        open_positions=int(account_dict.get("open_positions", 0)),
        daily_pnl=float(engine.status.daily_pnl),
    )
    is_paper = (
        config.get("paper_mode", True)
        or (config.get("broker") or "").startswith("local_paper")
        or (config.get("broker") or "").startswith("alpaca_paper")
    )

    best_overall = None
    for ticker in tickers:
        snap = engine.market_data.snapshot(ticker)
        data = snap.data
        for strategy in STRATEGY_REGISTRY.values():
            try:
                sig = strategy.analyze(ticker, data)
            except Exception:
                continue
            if sig.action == Action.HOLD:
                continue
            if best_overall is None or sig.confidence > best_overall[0].confidence:
                best_overall = (sig, data, ticker)

    if best_overall is None:
        return {"error": "no actionable signal across any strategy/ticker"}

    sig, data, ticker = best_overall
    price = float(data.get("price", 0.0))
    side = "BUY" if sig.action.value.startswith("BUY") else "SELL"
    decision = risk.evaluate(side, price, account, trade_style="swing", is_paper=is_paper)
    if not decision.approved:
        return {"error": f"risk rejected: {decision.reason}", "signal": {
            "ticker": ticker, "action": sig.action.value, "confidence": sig.confidence,
            "reason": sig.reason,
        }}

    plan = engine.build_order_plan(sig, decision.quantity, price)
    plan["stop_loss_price"] = plan.get("stop_loss_price") or decision.stop_loss_price
    plan["take_profit_price"] = plan.get("take_profit_price") or decision.take_profit_price
    order = engine._submit_order(sig, decision.quantity, price, plan=plan)
    filled_qty = plan.get("quantity", decision.quantity)
    trade_id = None
    if order.success:
        trade_id = engine._persist_trade(
            sig, filled_qty, price, order.paper,
            status="open", plan=plan, snapshot=data,
        )
        # Mirror the production cycle's execution-telemetry wire — force-trade
        # orders MUST log to execution_log so the Authority Spine's
        # EXECUTION pillar populates. Silent until 2026-05-31; fixed here.
        try:
            from backend.bot.execution_intel import log_execution
            fill_price = float(getattr(order, "raw", {}).get("price") or price)
            log_execution(
                ticker=ticker, side=side,
                quantity=float(filled_qty),
                expected_price=float(price),
                fill_price=fill_price,
                trade_id=trade_id,
            )
        except Exception:
            pass
    event = {
        "timestamp": engine.status.last_cycle_at,
        "ticker": ticker,
        "action": sig.action.value,
        "confidence": round(sig.confidence, 3),
        "reason": f"[forced] {sig.reason}",
        "strategy": sig.strategy,
        "status": "submitted" if order.success else "failed",
        "order_id": order.order_id,
        "paper": order.paper,
        "quantity": round(float(filled_qty), 4),
        "price": round(price, 2),
        "instrument": plan.get("instrument"),
        "option_type": plan.get("option_type"),
        "strike": plan.get("strike"),
        "expiration": plan.get("expiration"),
    }
    engine._emit(event)
    return {"success": order.success, "event": event, "order_id": order.order_id}
