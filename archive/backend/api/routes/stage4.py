"""Stage-4 endpoints — microstructure, cross-asset, event-risk."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from backend.bot.cross_asset import alignment_for, fetch_state, hedge_suggestion
from backend.bot.event_risk import active_events, can_trade, upcoming_events
from backend.bot.microstructure import assess_microstructure


# ── microstructure ────────────────────────────────────────────────────────


micro_router = APIRouter(prefix="/microstructure", tags=["microstructure"])


@micro_router.get("/{ticker}")
async def microstructure_for(ticker: str) -> dict:
    """Microstructure snapshot for ``ticker`` from available data. Uses the
    live yfinance candles + (best-effort) quote."""
    bars: list = []
    avg_volume = 0.0
    bid = ask = bid_size = ask_size = 0.0
    try:
        import yfinance as yf
        t = yf.Ticker(ticker.upper())
        df = t.history(period="5d", interval="5m")
        if df is not None and not df.empty:
            recent = df.tail(80)
            bars = [{"open": float(r["Open"]), "high": float(r["High"]),
                      "low": float(r["Low"]), "close": float(r["Close"]),
                      "volume": float(r["Volume"])}
                     for _, r in recent.iterrows()]
            avg_volume = float(df["Volume"].mean() or 0)
        try:
            info = getattr(t, "fast_info", None)
            if info:
                bid = float(getattr(info, "last_bid", 0) or 0)
                ask = float(getattr(info, "last_ask", 0) or 0)
        except Exception:
            pass
    except Exception:
        pass
    snap = assess_microstructure(
        ticker=ticker, bars=bars, avg_volume=avg_volume,
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
    )
    return snap.to_dict()


# ── cross-asset ───────────────────────────────────────────────────────────


cross_router = APIRouter(prefix="/cross-asset", tags=["cross-asset"])


@cross_router.get("/state")
async def cross_asset_state(force: bool = Query(False)) -> dict:
    return fetch_state(force=force).to_dict()


@cross_router.get("/alignment/{ticker_trend}")
async def cross_asset_alignment(ticker_trend: str) -> dict:
    """``ticker_trend`` is the per-ticker regime trend (bullish/bearish/choppy)
    — the caller fetched it via /analytics/{ticker} or /regime/."""
    return alignment_for(ticker_regime_trend=ticker_trend)


@cross_router.get("/hedge")
async def cross_asset_hedge(net_beta: float = Query(1.0)) -> dict:
    return hedge_suggestion(net_beta=net_beta)


# ── event-risk ────────────────────────────────────────────────────────────


event_router = APIRouter(prefix="/event-risk", tags=["event-risk"])


@event_router.get("/calendar")
async def event_calendar(within_days: int = Query(14, ge=1, le=90),
                            tickers: Optional[str] = Query(None)) -> dict:
    ticker_list = [t.strip().upper() for t in (tickers or "").split(",") if t.strip()]
    events = upcoming_events(within_days=within_days, tickers=ticker_list)
    return {"events": [e.to_dict() for e in events], "count": len(events)}


@event_router.get("/active")
async def event_active(pre_minutes: int = Query(30, ge=0, le=240),
                          post_minutes: int = Query(30, ge=0, le=240)) -> dict:
    events = active_events(window_minutes_before=pre_minutes,
                              window_minutes_after=post_minutes)
    return {"active": [e.to_dict() for e in events], "count": len(events),
             "auto_hold": len(events) > 0}


@event_router.get("/can-trade/{ticker}")
async def event_can_trade(ticker: str, pre_minutes: int = Query(30, ge=0, le=240),
                              post_minutes: int = Query(30, ge=0, le=240),
                              earnings_hold_days: int = Query(1, ge=0, le=7)) -> dict:
    perm = can_trade(ticker, pre_minutes=pre_minutes,
                       post_minutes=post_minutes,
                       earnings_hold_days=earnings_hold_days)
    return perm.to_dict()
