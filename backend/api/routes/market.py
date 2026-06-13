"""Market overview endpoints: index prices, VIX, regime, breadth.

Single endpoint ``/market/overview`` is what the UI's Market page calls every
few seconds. We pull intraday bars from yfinance because it's free and works
without keys; if the user has Finnhub configured, we use that for the latest
quote (faster, more accurate).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query, Request

from backend.bot.data.finnhub import FinnhubClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/market", tags=["market"])

# Tiny shared cache so rapid live-price polling from multiple chart widgets
# doesn't hammer yfinance and trip its rate limiter.
from backend.config import TUNABLES

_LAST_CACHE: Dict[str, Tuple[float, dict]] = {}
_LAST_TTL = TUNABLES.live_price_ttl

INDICES = ["SPY", "QQQ", "IWM", "DIA"]
SECTORS = [
    ("XLK", "Technology"),
    ("XLF", "Financials"),
    ("XLE", "Energy"),
    ("XLV", "Healthcare"),
    ("XLY", "Consumer Disc."),
    ("XLP", "Consumer Stap."),
    ("XLI", "Industrials"),
    ("XLU", "Utilities"),
]

_finnhub = FinnhubClient()


def _yf_intraday(ticker: str, interval: str = "5m", period: str = "1d") -> List[dict]:
    """Return list of {t, price} for the day. Empty on failure."""
    try:
        import yfinance as yf

        df = yf.download(
            ticker, period=period, interval=interval, progress=False, auto_adjust=False
        )
        if df is None or df.empty:
            return []
        # Handle MultiIndex columns yfinance sometimes returns
        if hasattr(df.columns, "get_level_values"):
            try:
                close = df["Close"]
                if hasattr(close, "columns"):
                    close = close.iloc[:, 0]
            except Exception:
                close = df.iloc[:, 0]
        else:
            close = df["Close"]
        out: List[dict] = []
        for ts, value in close.items():
            try:
                out.append({"t": ts.isoformat(), "price": float(value)})
            except Exception:
                continue
        return out
    except Exception:
        logger.exception("yf intraday for %s failed", ticker)
        return []


def _quote(ticker: str) -> Optional[dict]:
    """Single most-recent quote, finnhub-first then yfinance fallback."""
    if _finnhub.available:
        fq = _finnhub.quote(ticker)
        if fq and fq.price:
            return {
                "price": fq.price,
                "prev_close": fq.prev_close,
                "change_pct": ((fq.price - fq.prev_close) / fq.prev_close * 100)
                if fq.prev_close
                else 0.0,
                "source": "finnhub",
            }
    # Fall back to yfinance daily.
    try:
        import yfinance as yf

        df = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            try:
                close = df["Close"]
                if hasattr(close, "columns"):
                    close = close.iloc[:, 0]
            except Exception:
                close = df.iloc[:, 0]
        else:
            close = df["Close"]
        price = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else price
        return {
            "price": price,
            "prev_close": prev,
            "change_pct": ((price - prev) / prev * 100) if prev else 0.0,
            "source": "yfinance",
        }
    except Exception:
        return None


def _live_price(ticker: str) -> dict:
    """Freshest available price + timestamp, for the chart's live line.

    Order of preference: Finnhub real-time quote (if a key is set) →
    most-recent 1-minute yfinance bar → last daily close. Cached ~3s.
    """
    now = time.monotonic()
    hit = _LAST_CACHE.get(ticker)
    if hit and (now - hit[0]) < _LAST_TTL:
        return hit[1]

    out: Optional[dict] = None
    if _finnhub.available:
        fq = _finnhub.quote(ticker)
        if fq and fq.price:
            out = {"price": round(float(fq.price), 2), "t": datetime.now(timezone.utc).isoformat(), "source": "finnhub"}

    if out is None:
        try:
            import yfinance as yf

            df = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=False)
            if df is None or df.empty:
                df = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                if hasattr(df.columns, "get_level_values"):
                    try:
                        df.columns = df.columns.get_level_values(0)
                    except Exception:
                        pass
                out = {
                    "price": round(float(df["Close"].iloc[-1]), 2),
                    "t": df.index[-1].isoformat(),
                    "source": "yfinance",
                }
        except Exception:
            logger.warning("live price fetch failed for %s", ticker)

    if out is None:
        out = {"price": 0.0, "t": None, "source": "none"}
    out["ticker"] = ticker
    _LAST_CACHE[ticker] = (now, out)
    return out


@router.get("/last/{ticker}")
async def last(ticker: str) -> dict:
    """Lightweight freshest-price endpoint polled by the live chart line."""
    return _live_price(ticker.upper())


@router.get("/validate/{ticker}")
async def validate(ticker: str) -> dict:
    """Cross-check our primary feed (yfinance) against an independent source
    (Nasdaq) so the user can confirm the data isn't self-validated."""
    from backend.bot.data.validate import cross_validate

    return cross_validate(ticker.upper())


@router.get("/quote/{ticker}")
async def quote(ticker: str) -> dict:
    q = _quote(ticker.upper())
    return q or {"error": "no data"}


@router.get("/candles/{ticker}")
async def candles(ticker: str, period: str = "5d", interval: str = "5m") -> List[dict]:
    """OHLCV candles for a candlestick chart.

    ``period`` examples: ``1d``, ``5d``, ``1mo``, ``3mo``, ``1y``.
    ``interval`` examples: ``1m``, ``5m``, ``15m``, ``1h``, ``1d``.
    """
    try:
        import yfinance as yf

        df = yf.download(
            ticker.upper(),
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if df is None or df.empty:
            return []
        if hasattr(df.columns, "get_level_values"):
            # Flatten MultiIndex (occurs when ticker came back as a single-name).
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass
        out: List[dict] = []
        for ts, row in df.iterrows():
            try:
                out.append(
                    {
                        "t": ts.isoformat(),
                        "open": float(row.get("Open")),
                        "high": float(row.get("High")),
                        "low": float(row.get("Low")),
                        "close": float(row.get("Close")),
                        "volume": float(row.get("Volume") or 0),
                    }
                )
            except Exception:
                continue
        return out
    except Exception:
        logger.exception("candles fetch failed for %s", ticker)
        return []


@router.get("/search")
async def search(q: str = Query(..., min_length=1)) -> List[dict]:
    """Symbol search. Uses Finnhub when configured; falls back to a tiny
    built-in list for offline / no-key environments."""
    query = q.strip().upper()
    if not query:
        return []
    if _finnhub.available:
        payload = _finnhub._get("/search", {"q": query}) or {}
        results = payload.get("result") or []
        finnhub_out = [
            {
                "symbol": r.get("symbol") or r.get("displaySymbol"),
                "description": r.get("description"),
                "type": r.get("type"),
            }
            for r in results[:20]
            if r.get("symbol")
        ]
        if finnhub_out:
            return finnhub_out
    # Full-market fallback: SEC company list (~10k symbols) + common ETFs.
    from backend.bot.data import symbols as symbol_search

    return symbol_search.search(query, limit=20)


@router.get("/intraday/{ticker}")
async def intraday(ticker: str, interval: str = "5m", period: str = "1d") -> List[dict]:
    return _yf_intraday(ticker.upper(), interval=interval, period=period)


@router.get("/overview")
async def overview(request: Request) -> dict:
    """One bundled call: indices (price + intraday curve), sectors, VIX, regime."""
    engine = getattr(request.app.state, "engine", None)

    indices: List[dict] = []
    for symbol in INDICES:
        q = _quote(symbol) or {}
        curve = _yf_intraday(symbol, interval="5m", period="1d")
        indices.append({"ticker": symbol, **q, "curve": curve})

    sectors: List[dict] = []
    for symbol, name in SECTORS:
        q = _quote(symbol) or {}
        sectors.append({"ticker": symbol, "name": name, **q})

    vix_q = _quote("^VIX") or {}
    vix_value = float(vix_q.get("price") or 0.0)
    if vix_value >= 30:
        vix_label = "elevated"
    elif vix_value >= 20:
        vix_label = "moderate"
    else:
        vix_label = "calm"

    # Compute a quick breadth: % of indices/sectors with positive change.
    universe = indices + sectors
    advancers = sum(1 for x in universe if float(x.get("change_pct") or 0) > 0)
    breadth_pct = (advancers / len(universe)) * 100 if universe else 0.0

    market_status = "open"
    # Naive market-hours check: NY 9:30-16:00 weekdays. We treat anything else as closed.
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
        wd = now.weekday()
        hm = now.hour * 60 + now.minute
        if wd >= 5 or hm < 9 * 60 + 30 or hm >= 16 * 60:
            market_status = "closed"
    except Exception:
        pass

    return {
        "indices": indices,
        "sectors": sectors,
        "vix": {"value": vix_value, "label": vix_label, **vix_q},
        "breadth_pct": round(breadth_pct, 1),
        "advancers": advancers,
        "decliners": len(universe) - advancers,
        "regime": getattr(engine.status, "market_regime", None) if engine else None,
        "market_status": market_status,
    }
