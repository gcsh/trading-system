"""AI Co-Pilot endpoints — plain-English narration + one-tap autonomy.

This is the brain behind the beginner-friendly cockpit. It turns the engine's
raw state (status, positions, P&L, day plan) into sentences a non-expert can
read, scores how the 30-day paper trial is going, and exposes single calls to
flip autonomous trading on/off and (re)start the trial with a chosen balance.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from backend.api.routes.portfolio import performance as portfolio_performance
from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.config import load_config, save_config

from backend.config import SETTINGS, TUNABLES, anthropic_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/copilot", tags=["copilot"])

TRIAL_DAYS = TUNABLES.trial_days

# Map raw engine actions to friendly verbs/phrases.
_ACTION_PHRASES = {
    "BUY_STOCK": "bought shares of",
    "SELL_STOCK": "sold",
    "BUY_CALL": "bought a bullish call option on",
    "BUY_PUT": "bought a protective put option on",
    "BULL_CALL_SPREAD": "opened a bullish call spread on",
    "BUY_STRADDLE": "opened a volatility straddle on",
    "IRON_CONDOR": "opened a range-bound income trade on",
    "SELL_COVERED_CALL": "sold a covered call on",
    "SELL_CSP": "sold a cash-secured put on",
    "RATIO_SPREAD": "opened a ratio spread on",
    "COLLAR": "added a protective collar on",
    "HOLD": "decided to wait on",
}

_STATUS_PHRASES = {
    "submitted": "and the trade went through",
    "hold": "— no clear setup yet",
    "low_confidence": "— but the signal wasn't strong enough to act",
    "signal_only": "— a setup appeared, but autonomous trading is off so I didn't act",
    "rejected": "— but my risk rules blocked it",
    "too_small": "— but the position would've been too small to bother",
    "already_held": "— I already own it, so I left it alone",
    "failed": "— but the order didn't fill",
}


def _paper_executor(request: Request) -> Optional[PaperExecutor]:
    ex = getattr(request.app.state.engine, "executor", None)
    return ex if isinstance(ex, PaperExecutor) else None


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v:+.1f}%"


def _humanize_holdings(positions: List[dict]) -> str:
    if not positions:
        return "You're holding cash right now — no open positions."
    parts = []
    for p in positions[:6]:
        qty = p.get("quantity", 0)
        tk = p.get("ticker", "?")
        if p.get("kind") == "stock":
            upl = p.get("unrealized_pnl_pct")
            tail = f" ({_pct(upl)})" if isinstance(upl, (int, float)) else ""
            parts.append(f"{qty:g} share(s) of {tk}{tail}")
        else:
            parts.append(f"an options position on {tk}")
    return "You currently own: " + "; ".join(parts) + "."


def _spy_benchmark(since: Optional[date]) -> Optional[float]:
    """Return SPY's % return since `since` (or ~30d) using cached candles."""
    try:
        from backend.bot.backtest import fetch_candles

        df = fetch_candles("SPY", period="3mo", interval="1d")
        if df.empty or "Close" not in df.columns:
            return None
        closes = df["Close"].astype(float)
        idxs = [t.date() for t in df.index]
        start_px = float(closes.iloc[0])
        if since is not None:
            for i, d in enumerate(idxs):
                if d >= since:
                    start_px = float(closes.iloc[i])
                    break
        end_px = float(closes.iloc[-1])
        if start_px <= 0:
            return None
        return round((end_px - start_px) / start_px * 100, 2)
    except Exception:
        logger.exception("SPY benchmark failed")
        return None


def _trial_start(config: dict) -> Optional[date]:
    raw = config.get("trial_start")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except Exception:
        return None


@router.get("/briefing")
async def briefing(request: Request) -> dict:
    """Everything the cockpit needs in one call: plain-English story, money,
    holdings, autonomy state, the 30-day trial tracker, and a go-live score."""
    engine = request.app.state.engine
    with session_scope() as session:
        config = load_config(session)

    status = engine.status
    autonomous = bool(config.get("auto_execute")) and bool(status.running)
    interval = int(config.get("live_interval_sec", 30) or 30)
    tickers = config.get("tickers", []) or []

    ex = _paper_executor(request)
    acct = ex.get_account_state() if ex else {}
    positions = ex.positions() if ex else []
    perf = await portfolio_performance()

    equity = float(acct.get("portfolio_value", 0.0))
    start_cash = float(acct.get("starting_cash", 0.0)) or 5000.0
    total_ret_pct = ((equity - start_cash) / start_cash * 100) if start_cash else 0.0

    # ---- Trial tracker --------------------------------------------------
    tstart = _trial_start(config)
    if tstart is None:
        days_in = 0
        days_left = TRIAL_DAYS
    else:
        days_in = (datetime.now(timezone.utc).date() - tstart).days
        days_left = max(0, TRIAL_DAYS - days_in)

    # ---- Benchmark vs SPY ----------------------------------------------
    spy_ret = _spy_benchmark(tstart)
    beat_spy = spy_ret is not None and total_ret_pct > spy_ret

    # ---- Plain-English narration ---------------------------------------
    lines: List[str] = []
    if autonomous:
        lines.append(
            f"🟢 I'm trading on my own. I scan {len(tickers)} stock(s) about every "
            f"{interval} seconds and act when I find a strong setup — no action needed from you."
        )
    elif config.get("auto_execute") and not status.running:
        lines.append("🟡 Autonomous trading is armed but paused. Press Start (or flip the switch) and I'll begin.")
    else:
        lines.append("⏸️ I'm in watch-only mode. I'll find and explain setups, but I won't place any trades until you switch me on.")

    regime = status.market_regime or "unclear"
    strat = (status.active_strategy or "adaptive").replace("_", " ")
    plan = status.day_plan or {}
    reason = plan.get("reason")
    market_line = f"The overall market looks **{regime}** right now, so I'm favoring my **{strat}** approach."
    if reason:
        market_line += f" ({reason})"
    lines.append(market_line)

    money_line = (
        f"Your practice account is worth **{_money(equity)}** (you started with {_money(start_cash)}), "
        f"which is **{_pct(total_ret_pct)}**. Today's change: **{_money(perf.get('pnl_today', 0.0))}**."
    )
    lines.append(money_line)
    lines.append(_humanize_holdings(positions))

    if spy_ret is not None:
        if beat_spy:
            lines.append(f"📈 You're **beating** the market — just holding the S&P 500 (SPY) would be {_pct(spy_ret)}, and you're at {_pct(total_ret_pct)}.")
        else:
            lines.append(f"📉 Right now you're **trailing** the simple option of just holding the S&P 500 (SPY) at {_pct(spy_ret)}. That's the honest benchmark to beat.")

    # Most recent meaningful action
    last_action = None
    for ev in reversed(status.recent_signals or []):
        if ev.get("status") in ("submitted", "signal_only"):
            verb = _ACTION_PHRASES.get(ev.get("action", ""), "looked at")
            tail = _STATUS_PHRASES.get(ev.get("status", ""), "")
            last_action = f"Most recently I {verb} {ev.get('ticker','?')} {tail}."
            break
    if last_action:
        lines.append(last_action)

    # ---- Go-live readiness scorecard -----------------------------------
    closed = perf.get("closed_count", 0)
    win_rate = perf.get("win_rate", 0.0)
    total_pnl = perf.get("total_pnl", 0.0)
    max_dd = perf.get("max_drawdown_pct", 0.0)
    checks = [
        {
            "label": "Tested long enough",
            "pass": days_in >= 20,
            "detail": f"{days_in} of ~20+ days into the {TRIAL_DAYS}-day trial",
        },
        {
            "label": "Enough trades to judge",
            "pass": closed >= 10,
            "detail": f"{closed} of 10+ completed trades",
        },
        {
            "label": "Actually made money",
            "pass": total_pnl > 0,
            "detail": f"net practice P&L is {_money(total_pnl)}",
        },
        {
            "label": "Beat just holding SPY",
            "pass": bool(beat_spy),
            "detail": (f"you {_pct(total_ret_pct)} vs SPY {_pct(spy_ret)}" if spy_ret is not None else "SPY benchmark unavailable"),
        },
        {
            "label": "Kept losses in check",
            "pass": (max_dd < 15.0) and (win_rate >= 0.45 if closed else False),
            "detail": f"worst drop {max_dd:.1f}%, win rate {win_rate*100:.0f}%",
        },
    ]
    score = sum(1 for c in checks if c["pass"])
    if score >= 5:
        verdict = "You've met every checkpoint — you could consider going live with a small amount you can afford to lose."
    elif score >= 3:
        verdict = "Promising, but keep paper trading — a couple of checkpoints aren't met yet."
    else:
        verdict = "Too early to go live. Let the AI keep practicing and gathering results."

    ai_cfg = config.get("ai") or {}
    return {
        "autonomous": autonomous,
        "armed": bool(config.get("auto_execute")),
        "running": bool(status.running),
        "interval_sec": interval,
        "ai_available": bool(anthropic_key()),
        "brain_enabled": bool(ai_cfg.get("brain_enabled")),
        "brain_web_research": bool(ai_cfg.get("brain_web_research")),
        "meta_enabled": bool(ai_cfg.get("meta_enabled")),
        "tickers": tickers,
        "equity": round(equity, 2),
        "starting_cash": round(start_cash, 2),
        "total_return_pct": round(total_ret_pct, 2),
        "pnl_today": perf.get("pnl_today", 0.0),
        "market_regime": regime,
        "active_strategy": status.active_strategy,
        "positions": positions,
        "benchmark_spy_pct": spy_ret,
        "beat_spy": beat_spy,
        "narrative": lines,
        "trial": {
            "start": config.get("trial_start"),
            "days_in": days_in,
            "days_left": days_left,
            "total_days": TRIAL_DAYS,
            "active": tstart is not None,
        },
        "readiness": {"score": score, "max": len(checks), "checks": checks, "verdict": verdict},
        "performance": perf,
    }


@router.post("/autonomy")
async def set_autonomy(request: Request, payload: dict) -> dict:
    """Flip fully-autonomous paper trading on or off in one call.

    ``{"on": true}`` enables auto-execute and starts the live loop;
    ``{"on": false}`` disables auto-execute and stops trading.
    """
    on = bool(payload.get("on"))
    engine = request.app.state.engine
    with session_scope() as session:
        config = load_config(session)
        config["auto_execute"] = on
        if on:
            # Keep autonomous trading safely on the local paper broker.
            config.setdefault("broker", "local_paper")
            config["paper_mode"] = True
            if not config.get("trial_start"):
                config["trial_start"] = datetime.now(timezone.utc).date().isoformat()
        save_config(session, config)
        interval = float(config.get("live_interval_sec", 30) or 30)

    if on:
        engine.start_live_loop(interval_sec=interval)
    else:
        engine.stop()

    return {
        "autonomous": on and engine.status.running,
        "running": engine.status.running,
        "auto_execute": on,
        "interval_sec": interval,
    }


# Which strategies are "proven" for each market regime (textbook pairings).
REGIME_STRATEGIES = {
    "trending_up": ["macd_momentum", "trend_pullback", "macd"],
    "trending_down": ["rsi_mean_reversion", "gap_fill", "vwap_reversion"],
    "choppy": ["rsi_mean_reversion", "vwap_reversion", "gap_fill"],
    "unknown": ["macd_momentum", "rsi_mean_reversion", "vwap_reversion", "trend_pullback"],
}


def _detect_regime() -> Dict[str, Any]:
    """Read SPY to label the current market regime in plain English."""
    from backend.bot.backtest import fetch_candles

    df = fetch_candles("SPY", period="6mo", interval="1d")
    if df.empty or "Close" not in df.columns:
        return {"key": "unknown", "label": "Unclear", "description": "I couldn't read the market right now (data unavailable).", "metrics": {}}
    close = df["Close"].astype(float)
    px = float(close.iloc[-1])
    ma50 = float(close.rolling(50, min_periods=1).mean().iloc[-1])
    ma200 = float(close.rolling(min(200, len(close)), min_periods=1).mean().iloc[-1])
    vol20 = float(close.pct_change().tail(20).std() * (252 ** 0.5)) if len(close) > 20 else 0.0
    if px > ma50 > ma200:
        key, label = "trending_up", "Trending up 📈"
        desc = "The market is in a steady uptrend — price is above its 50- and 200-day averages. Momentum strategies tend to shine here."
    elif px < ma50 < ma200:
        key, label = "trending_down", "Trending down 📉"
        desc = "The market is in a downtrend — price is below its averages. Time to be defensive and favor mean-reversion."
    else:
        key, label = "choppy", "Choppy / sideways ↔️"
        desc = "No clear trend — the market is chopping sideways. Range/mean-reversion strategies usually do best."
    return {
        "key": key, "label": label, "description": desc,
        "metrics": {"price": round(px, 2), "ma50": round(ma50, 2), "ma200": round(ma200, 2), "volatility_pct": round(vol20 * 100, 1)},
    }


def _sample_sims(ticker: str, names: List[str], period: str = "6mo") -> Dict[str, dict]:
    """Backtest each strategy on one ticker, sharing the candle fetch."""
    from backend.bot.backtest import (
        _candles_and_series, _resolve_strategy, compute_indicators,
        fetch_candles, simulate_strategy,
    )

    df = fetch_candles(ticker, period=period, interval="1d")
    if df.empty or "Close" not in df.columns:
        return {}
    ind = compute_indicators(df)
    candles, series, closes, timestamps = _candles_and_series(df, ind)
    out: Dict[str, dict] = {}
    for name in names:
        try:
            strat = _resolve_strategy(name)
        except Exception:
            continue
        out[name] = simulate_strategy(strat, ticker, df, ind, closes, timestamps)
    return out


@router.get("/recommend")
async def recommend(request: Request, ticker: str = "SPY") -> dict:
    """Strategy Scout: regime → candidate strategies → sample backtests → verdict.

    Returns every stage so the UI can reveal the bot's reasoning step by step.
    """
    with session_scope() as session:
        config = load_config(session)
    cfg_tickers = [t.upper() for t in (config.get("tickers") or [])]
    ticker = ticker.upper()
    # Sample universe: the chosen ticker + a few configured names (dedup, cap 5).
    samples: List[str] = []
    for t in [ticker] + cfg_tickers + ["SPY", "AAPL", "NVDA"]:
        if t not in samples:
            samples.append(t)
    samples = samples[:5]

    regime = _detect_regime()
    candidates = REGIME_STRATEGIES.get(regime["key"], REGIME_STRATEGIES["unknown"])

    # Backtest every candidate across the sample stocks (fetch shared per stock).
    per_stock: Dict[str, Dict[str, dict]] = {}
    for s in samples:
        per_stock[s] = _sample_sims(s, candidates)

    results: List[dict] = []
    for name in candidates:
        rets, alphas, wins, trades, beat = [], [], [], 0, 0
        rows = []
        for s in samples:
            sim = per_stock.get(s, {}).get(name)
            if not sim:
                continue
            rets.append(sim["total_return_pct"])
            alphas.append(sim["alpha_pct"])
            trades += sim["num_trades"]
            if sim["num_trades"] > 0 and sim["win_rate"] is not None:
                wins.append(sim["win_rate"])
            if sim["alpha_pct"] > 0:
                beat += 1
            rows.append({"ticker": s, "return_pct": sim["total_return_pct"], "alpha_pct": sim["alpha_pct"], "win_rate": sim["win_rate"], "trades": sim["num_trades"]})
        n = max(1, len(rets))
        results.append({
            "strategy": name,
            "avg_return_pct": round(sum(rets) / n, 2) if rets else 0.0,
            "avg_alpha_pct": round(sum(alphas) / n, 2) if alphas else 0.0,
            "avg_win_rate": round(sum(wins) / len(wins), 3) if wins else None,
            "total_trades": trades,
            "beat_bh_count": beat,
            "samples_tested": len(rows),
            "per_stock": rows,
        })

    # Rank: strategies that actually traded first, then by alpha over buy-&-hold.
    results.sort(key=lambda r: (r["total_trades"] > 0, r["avg_alpha_pct"], r["avg_return_pct"]), reverse=True)
    best = results[0] if results else None

    # Verdict
    if best is None or best["total_trades"] == 0:
        verdict = {"good": False, "headline": "No strong match yet", "detail": "None of the regime-matched strategies produced enough trades on these stocks to recommend. Stick with the adaptive selector for now.", "confidence": 0}
    else:
        good = best["avg_alpha_pct"] > 0 and best["avg_return_pct"] > 0
        conf = int(min(100, (best["beat_bh_count"] / max(1, best["samples_tested"])) * 100))
        nice = best["strategy"].replace("_", " ")
        if good:
            headline = f"Try {nice} for this market"
            detail = (f"In a {regime['label'].split(' ')[0].lower()} market like now, {nice} averaged "
                      f"{best['avg_return_pct']:+.1f}% across {best['samples_tested']} sample stocks and beat buy-and-hold on "
                      f"{best['beat_bh_count']} of them. That's a {conf}% hit rate vs just holding.")
        else:
            headline = f"{nice} is the best of a weak bunch"
            detail = (f"{nice} ranked highest for this {regime['label'].split(' ')[0].lower()} market, but it only averaged "
                      f"{best['avg_return_pct']:+.1f}% and didn't reliably beat buy-and-hold. I'd keep paper-testing before trusting it.")
        verdict = {"good": good, "headline": headline, "detail": detail, "confidence": conf}

    return {
        "ticker": ticker,
        "samples": samples,
        "regime": regime,
        "candidates": results,
        "best": best,
        "verdict": verdict,
    }


@router.post("/apply-strategy")
async def apply_strategy(request: Request, payload: dict) -> dict:
    """Set the bot's active strategy (used by Strategy Scout's 'approve' step)."""
    name = (payload or {}).get("strategy")
    if not name:
        return {"error": "no strategy provided"}
    with session_scope() as session:
        config = load_config(session)
        config["strategy"] = name
        save_config(session, config)
    return {"strategy": name, "applied": True}


@router.post("/brain")
async def set_brain(request: Request, payload: dict) -> dict:
    """Turn the fully-autonomous AI Brain on/off (and optional web research).

    ``{"enabled": true, "web_research": false}``. The brain only acts when a
    valid ANTHROPIC_API_KEY is configured; otherwise the engine keeps using the
    rule strategies and ``ai_available`` is reported false.
    """
    enabled = bool((payload or {}).get("enabled"))
    web = bool((payload or {}).get("web_research"))
    with session_scope() as session:
        config = load_config(session)
        ai = dict(config.get("ai") or {})
        ai["brain_enabled"] = enabled
        ai["brain_web_research"] = web
        config["ai"] = ai
        save_config(session, config)
    return {
        "brain_enabled": enabled,
        "brain_web_research": web,
        "ai_available": bool(anthropic_key()),
    }


@router.post("/meta")
async def set_meta(request: Request, payload: dict) -> dict:
    """Toggle the Meta-AI strategist (Claude-driven approve/veto + size modifier
    on every trade). Only effective when a key is configured."""
    enabled = bool((payload or {}).get("enabled"))
    with session_scope() as session:
        config = load_config(session)
        ai = dict(config.get("ai") or {})
        ai["meta_enabled"] = enabled
        config["ai"] = ai
        save_config(session, config)
    return {"meta_enabled": enabled, "ai_available": bool(anthropic_key())}


@router.get("/ai-status")
async def ai_status() -> dict:
    """Whether a usable Anthropic key is configured (env or saved via UI)."""
    return {"ai_available": bool(anthropic_key())}


@router.post("/ai-key")
async def set_ai_key(request: Request, payload: dict) -> dict:
    """Save the user's Anthropic API key (used at runtime — no restart needed).

    Stored in the local bot config and never returned to the browser. A blank
    key is ignored so this can't accidentally disconnect an existing key.
    """
    key = ((payload or {}).get("key") or "").strip()
    if key:
        with session_scope() as session:
            config = load_config(session)
            config["anthropic_api_key"] = key
            save_config(session, config)
    return {"ai_available": bool(anthropic_key())}


def _chat_context(request: Request) -> str:
    """Compact live snapshot of the bot for the chat copilot to ground answers."""
    engine = request.app.state.engine
    with session_scope() as session:
        config = load_config(session)
    status = engine.status
    ex = _paper_executor(request)
    acct = ex.get_account_state() if ex else {}
    positions = ex.positions() if ex else []

    lines: List[str] = []
    lines.append(
        f"Account value {_money(float(acct.get('portfolio_value', 0.0)))}, "
        f"cash {_money(float(acct.get('cash', acct.get('buying_power', 0.0))))}."
    )
    auton = "ON" if (config.get("auto_execute") and status.running) else "off"
    lines.append(
        f"Autonomy: {auton}; active approach: {status.active_strategy or 'adaptive'}; "
        f"market regime: {status.market_regime or 'unknown'}."
    )
    if positions:
        parts = [f"{(p.get('quantity') or 0):g} {p.get('ticker', '?')}" for p in positions[:8]]
        lines.append("Open positions: " + "; ".join(parts) + ".")
    else:
        lines.append("Open positions: none (all cash).")
    recent = [
        f"{e.get('action')} {e.get('ticker')} ({e.get('status')})"
        for e in (status.recent_signals or [])[-5:]
    ]
    if recent:
        lines.append("Recent bot actions: " + ", ".join(recent) + ".")
    lines.append("Watchlist: " + ", ".join(config.get("tickers") or []) + ".")
    return "\n".join(lines)


@router.post("/chat")
async def chat(request: Request, payload: dict) -> dict:
    """Live chat with the AI copilot, grounded in the bot's current state."""
    from backend.bot.ai.chat import available, chat_reply

    message = ((payload or {}).get("message") or "").strip()
    history = (payload or {}).get("history") or []
    if not message:
        return {"reply": "Ask me anything about your bot, a stock, or how trading works.",
                "available": available()}
    context = _chat_context(request)
    reply = chat_reply(message, history=history, context=context)
    return {"reply": reply, "available": available()}


@router.post("/start-trial")
async def start_trial(request: Request, payload: Optional[dict] = None) -> dict:
    """Reset the paper account to a chosen balance and (re)start the trial clock."""
    body = payload or {}
    # Sanity guard — reject obviously-wrong starting balances that would blow up
    # the trial baseline. Operator can override with `confirm=True`.
    _max = float(getattr(TUNABLES, "trial_starting_equity", 5000.0)) * 2.0
    _starting = float(body.get("starting_cash", 0) or 0)
    if _starting > _max and not body.get("confirm", False):
        raise HTTPException(
            status_code=400,
            detail=f"starting_cash={_starting} exceeds 2x trial baseline ({_max}); pass confirm=True to override",
        )
    starting = float(body.get("starting_cash", 5000.0) or 5000.0)
    engine = request.app.state.engine
    ex = _paper_executor(request)
    if ex is None:
        return {"error": "active broker is not local_paper"}

    ex.starting_cash = starting
    acct = ex.reset(starting_cash=starting)
    with session_scope() as session:
        config = load_config(session)
        config["paper_cash_override"] = starting
        config["trial_start"] = datetime.now(timezone.utc).date().isoformat()
        save_config(session, config)
        # Wipe the old equity curve so a fresh trial starts clean (stale low
        # snapshots otherwise distort % / Sharpe / drawdown forever).
        from backend.models.snapshot import PortfolioSnapshot

        session.query(PortfolioSnapshot).delete()

    return {"starting_cash": starting, "trial_start": config["trial_start"], "account": acct}
