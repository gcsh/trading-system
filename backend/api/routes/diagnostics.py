"""Cycle diagnostics — show *why* trades did or didn't fire.

Calls every strategy on the latest snapshot for each configured ticker and
returns the per-strategy verdict + the risk-manager's decision. The UI
renders this as a table so the user can see at a glance what the bot is
seeing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.bot.risk import AccountState, RiskManager
from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY, get_strategy
from backend.bot.strategies.base import Action
from backend.db import session_scope
from backend.models.config import load_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/kg-fallback")
async def kg_fallback_stats() -> dict:
    """MITS Phase 12.2 — consumer-side knowledge-graph fallback
    observability. Reports how often each cohort source served reads:

      * cell           — exact (ticker, pattern, regime, vol_state) hit
      * pattern_regime — pooled (pattern, regime) parent
      * pattern        — pooled global (pattern) parent
      * local_thin     — local hit but N<MIN_N_LOCAL and no parent N
      * none           — no posterior available

    `fallback_rate` = 1 - cell/calls. Higher = consumers are reading
    parent pools more often than local cells — expected during corpus
    cold-start, should converge toward zero as the corpus matures.
    """
    try:
        from backend.bot.corpus.knowledge_graph import get_fallback_stats
        return get_fallback_stats()
    except Exception as e:
        return {"error": str(e), "calls": 0}


@router.post("/kg-fallback/reset")
async def kg_fallback_reset() -> dict:
    """Reset the in-process kg-fallback counters."""
    try:
        from backend.bot.corpus.knowledge_graph import reset_fallback_stats
        reset_fallback_stats()
        return {"status": "reset"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/cycle")
async def cycle(request: Request) -> dict:
    """Run every strategy on the current snapshot of every ticker. No orders."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return {"error": "engine not available"}
    with session_scope() as session:
        config = load_config(session)
    tickers: List[str] = config.get("tickers", []) or []
    risk = RiskManager(config)
    threshold = float(config.get("min_confidence", 0.6) or 0.6)
    account_dict = engine.executor.get_account_state()
    account = AccountState(
        buying_power=float(account_dict.get("buying_power", 0.0)),
        portfolio_value=float(account_dict.get("portfolio_value", 0.0)),
        open_positions=int(account_dict.get("open_positions", 0)),
        daily_pnl=float(engine.status.daily_pnl),
    )

    diagnostics: List[dict] = []
    for ticker in tickers:
        snap = engine.market_data.snapshot(ticker)
        data = snap.data
        per_strategy: List[dict] = []
        for name, strategy in STRATEGY_REGISTRY.items():
            try:
                sig = strategy.analyze(ticker, data)
            except Exception as exc:
                per_strategy.append({"name": name, "action": "ERROR", "confidence": 0.0, "reason": str(exc)})
                continue
            per_strategy.append({
                "name": name,
                "action": sig.action.value,
                "confidence": round(sig.confidence, 3),
                "reason": sig.reason[:200] if sig.reason else "",
                "would_act": sig.action != Action.HOLD and sig.confidence >= threshold,
            })

        # Best non-HOLD signal across strategies, for risk evaluation.
        best = max(
            (s for s in per_strategy if s["action"] not in ("HOLD", "ERROR")),
            key=lambda s: s["confidence"],
            default=None,
        )
        risk_decision = None
        if best:
            price = float(data.get("price") or 0)
            side = "BUY" if best["action"].startswith("BUY") else "SELL"
            decision = risk.evaluate(side, price, account, trade_style="swing")
            risk_decision = {
                "approved": decision.approved,
                "reason": decision.reason,
                "quantity": decision.quantity,
                "stop_loss_price": decision.stop_loss_price,
                "take_profit_price": decision.take_profit_price,
            }

        diagnostics.append({
            "ticker": ticker,
            "snapshot": {
                "price": data.get("price"),
                "rsi": data.get("rsi"),
                "macd": data.get("macd"),
                "macd_signal": data.get("macd_signal"),
                "ma50": data.get("ma50"),
                "ma200": data.get("ma200"),
                "vix": data.get("vix"),
                "adx": data.get("adx"),
                "news_score": data.get("news_score"),
                "iv_rank": data.get("iv_rank"),
            },
            "strategies": per_strategy,
            "best": best,
            "risk_decision": risk_decision,
            "source_errors": getattr(snap, "source_errors", []),
        })

    # Summary counts so the UI can show "of 7 tickers, 3 have actionable signals".
    actionable_count = sum(1 for d in diagnostics if d["best"] and d["best"]["would_act"])
    return {
        "tickers_scanned": len(tickers),
        "actionable_count": actionable_count,
        "auto_execute": bool(config.get("auto_execute", False)),
        "min_confidence": threshold,
        "diagnostics": diagnostics,
    }


@router.get("/strategy/{strategy_name}")
async def test_strategy(
    strategy_name: str,
    request: Request,
    tickers: Optional[str] = Query(None, description="comma-separated tickers; default = configured tickers"),
) -> dict:
    """Run a single strategy against the current snapshot of each ticker. No orders.

    Useful for evaluating "what would this strategy do right now?" before
    selecting it as the active strategy.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not available")
    if strategy_name == "adaptive":
        from backend.bot.strategies.adaptive import AdaptiveStrategy

        strategy = AdaptiveStrategy()
    else:
        try:
            strategy = get_strategy(strategy_name)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"unknown strategy: {strategy_name}")

    with session_scope() as session:
        config = load_config(session)
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    else:
        ticker_list = config.get("tickers", []) or []
    threshold = float(config.get("min_confidence", 0.6) or 0.6)

    results: List[dict] = []
    for ticker in ticker_list:
        snap = engine.market_data.snapshot(ticker)
        data = snap.data
        try:
            sig = strategy.analyze(ticker, data)
        except Exception as exc:
            results.append({
                "ticker": ticker,
                "action": "ERROR",
                "confidence": 0.0,
                "reason": str(exc),
                "would_act": False,
            })
            continue
        results.append({
            "ticker": ticker,
            "action": sig.action.value,
            "confidence": round(sig.confidence, 3),
            "reason": sig.reason[:240] if sig.reason else "",
            "would_act": sig.action != Action.HOLD and sig.confidence >= threshold,
            "stop_loss": getattr(sig, "stop_loss", None),
            "take_profit": getattr(sig, "take_profit", None),
            "snapshot_price": data.get("price"),
        })

    actionable = sum(1 for r in results if r["would_act"])
    return {
        "strategy": strategy_name,
        "tickers_scanned": len(ticker_list),
        "actionable_count": actionable,
        "min_confidence": threshold,
        "results": results,
    }


@router.post("/seed-demo")
async def seed_demo_route(payload: Optional[dict] = None) -> dict:
    """Insert (or clear) a deterministic demo trade set — for E2E / trying the
    deep-links. Disabled unless ``TB_ALLOW_DEMO_SEED=1`` so it can never mutate a
    real deployment by accident. ``{"clear": true}`` removes the demo rows.
    """
    import os

    if os.getenv("TB_ALLOW_DEMO_SEED") != "1":
        raise HTTPException(status_code=403, detail="demo seeding disabled (set TB_ALLOW_DEMO_SEED=1)")

    from backend.bot.seed import clear_demo, seed_demo

    if (payload or {}).get("clear"):
        with session_scope() as session:
            removed = clear_demo(session)
        return {"cleared": removed}
    with session_scope() as session:
        ids = seed_demo(session, force=bool((payload or {}).get("force")))
    return {"seeded": ids, "count": len(ids)}
