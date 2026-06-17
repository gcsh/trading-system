"""Multi-source data cross-validation with consensus.

A single feed can't prove itself, and one reference (Nasdaq) has coverage gaps
(it doesn't carry NYSE-Arca ETFs like SPY). So we keep a *registry* of
independent reference providers, query every one that's available, and report
how many agree with our primary feed (yfinance). More agreeing sources =
higher confidence.

- No key needed (active now): **Nasdaq**.
- Free with a key (set the env var to activate): **Alpha Vantage**, **Twelve
  Data**, **Financial Modeling Prep**, **Polygon**, **Alpaca**. These also
  cover ETFs/SPY, closing Nasdaq's gap.

Each adapter returns ``{ "YYYY-MM-DD": close }`` or ``None``.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from backend.config import TUNABLES

_CACHE: Dict[str, Tuple[float, dict]] = {}
_TTL = TUNABLES.validation_cache_ttl


def _http_json(url: str, headers: Optional[dict] = None):
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=20, headers=headers or {})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as exc:
        logger.warning("validate http failed %s: %s", url.split("?")[0], exc)
        return None


def _http_text(url: str, headers: Optional[dict] = None) -> Optional[str]:
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=20, headers=headers or {})
        return r.text if r.status_code == 200 else None
    except Exception as exc:
        logger.warning("validate http failed %s: %s", url.split("?")[0], exc)
        return None


def _norm(d) -> str:
    return str(datetime.fromisoformat(str(d).replace("Z", "")).date()) if "T" in str(d) or "-" in str(d) else str(d)


# ── reference providers ──────────────────────────────────────────────────────

def _nasdaq(ticker: str, days: int) -> Optional[Dict[str, float]]:
    to, frm = date.today(), date.today() - timedelta(days=days)
    hdrs = {"Accept": "application/json", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    for asset in ("stocks", "etf"):
        j = _http_json(f"https://api.nasdaq.com/api/quote/{ticker}/historical?assetclass={asset}&fromdate={frm}&todate={to}&limit=9999", hdrs)
        rows = (((j or {}).get("data") or {}).get("tradesTable") or {}).get("rows")
        if rows:
            out = {}
            for r in rows:
                try:
                    out[str(__import__("pandas").to_datetime(r["date"]).date())] = float(str(r["close"]).replace("$", "").replace(",", ""))
                except Exception:
                    pass
            if out:
                return out
    return None


def _cboe(ticker: str, days: int) -> Optional[Dict[str, float]]:
    j = _http_json(f"https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{ticker}.json")
    rows = (j or {}).get("data") or []
    out = {}
    for r in rows:
        try:
            out[str(r["date"])] = float(r["close"])
        except Exception:
            pass
    return out or None


def _stockanalysis(ticker: str, days: int) -> Optional[Dict[str, float]]:
    j = _http_json(f"https://stockanalysis.com/api/symbol/e/{ticker}/history?range=6M&period=Daily")
    rows = (j or {}).get("data") or []
    out = {}
    for r in rows:
        try:
            out[str(r["t"])] = float(r["c"])
        except Exception:
            pass
    return out or None


def _marketwatch(ticker: str, days: int) -> Optional[Dict[str, float]]:
    to, frm = date.today(), date.today() - timedelta(days=days)
    url = (f"https://www.marketwatch.com/investing/fund/{ticker.lower()}/downloaddatapartial"
           f"?startdate={frm:%m/%d/%Y}%2000:00:00&enddate={to:%m/%d/%Y}%2023:59:59"
           f"&daterange=d30&frequency=p1d&csvdownload=true&downloadpartial=false&newdates=false")
    text = _http_text(url)
    if not text or "Date" not in text[:40]:
        return None
    out = {}
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        try:
            d = datetime.strptime(parts[0].strip(), "%m/%d/%Y").date().isoformat()
            out[d] = float(parts[4].strip().strip('"').replace(",", ""))
        except Exception:
            continue
    return out or None


def _coinbase(ticker: str, days: int) -> Optional[Dict[str, float]]:
    from backend.bot.market_profile import is_crypto

    if not is_crypto(ticker):
        return None
    j = _http_json(f"https://api.exchange.coinbase.com/products/{ticker.upper()}/candles?granularity=86400")
    if not isinstance(j, list):
        return None
    out = {}
    for row in j:  # [time, low, high, open, close, volume]
        try:
            out[str(datetime.utcfromtimestamp(row[0]).date())] = float(row[4])
        except Exception:
            pass
    return out or None


def _binance(ticker: str, days: int) -> Optional[Dict[str, float]]:
    from backend.bot.market_profile import is_crypto

    if not is_crypto(ticker):
        return None
    base = ticker.upper().split("-")[0]
    j = _http_json(f"https://api.binance.com/api/v3/klines?symbol={base}USDT&interval=1d&limit=120")
    if not isinstance(j, list):
        return None
    out = {}
    for k in j:  # [openTime, open, high, low, close, ...]
        try:
            out[str(datetime.utcfromtimestamp(k[0] / 1000).date())] = float(k[4])
        except Exception:
            pass
    return out or None


def _kraken(ticker: str, days: int) -> Optional[Dict[str, float]]:
    from backend.bot.market_profile import is_crypto

    if not is_crypto(ticker):
        return None
    base = ticker.upper().split("-")[0]
    base = "XBT" if base == "BTC" else base  # Kraken calls Bitcoin XBT
    j = _http_json(f"https://api.kraken.com/0/public/OHLC?pair={base}USD&interval=1440")
    result = (j or {}).get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), None)
    if not rows:
        return None
    out = {}
    for r in rows:  # [time, open, high, low, close, vwap, volume, count]
        try:
            out[str(datetime.utcfromtimestamp(int(r[0])).date())] = float(r[4])
        except Exception:
            pass
    return out or None


def _alphavantage(ticker: str, days: int) -> Optional[Dict[str, float]]:
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        return None
    j = _http_json(f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={ticker}&outputsize=compact&apikey={key}")
    ts = (j or {}).get("Time Series (Daily)") or {}
    return {d: float(v["4. close"]) for d, v in ts.items()} or None


def _twelvedata(ticker: str, days: int) -> Optional[Dict[str, float]]:
    key = os.getenv("TWELVEDATA_API_KEY")
    if not key:
        return None
    j = _http_json(f"https://api.twelvedata.com/time_series?symbol={ticker}&interval=1day&outputsize=80&apikey={key}")
    vals = (j or {}).get("values") or []
    return {_norm(v["datetime"]): float(v["close"]) for v in vals} or None


def _fmp(ticker: str, days: int) -> Optional[Dict[str, float]]:
    key = os.getenv("FMP_API_KEY")
    if not key:
        return None
    j = _http_json(f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}?serietype=line&apikey={key}")
    hist = (j or {}).get("historical") or []
    return {h["date"]: float(h["close"]) for h in hist} or None


def _polygon(ticker: str, days: int) -> Optional[Dict[str, float]]:
    key = os.getenv("POLYGON_API_KEY")
    if not key:
        return None
    to, frm = date.today(), date.today() - timedelta(days=days)
    j = _http_json(f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{frm}/{to}?adjusted=true&sort=asc&limit=5000&apiKey={key}")
    res = (j or {}).get("results") or []
    return {str(datetime.utcfromtimestamp(r["t"] / 1000).date()): float(r["c"]) for r in res} or None


def _alpaca(ticker: str, days: int) -> Optional[Dict[str, float]]:
    kid, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_API_SECRET")
    if not (kid and sec):
        return None
    frm = (date.today() - timedelta(days=days)).isoformat()
    j = _http_json(
        f"https://data.alpaca.markets/v2/stocks/{ticker}/bars?timeframe=1Day&start={frm}&limit=1000&adjustment=raw",
        {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec, "Accept": "application/json"},
    )
    bars = (j or {}).get("bars") or []
    return {_norm(b["t"]): float(b["c"]) for b in bars} or None


# name → (fetcher, needs_key). No-key sources run immediately; keyed ones
# activate when their env var is set (and add redundancy + extra coverage).
PROVIDERS: Dict[str, Tuple[Callable, bool]] = {
    "nasdaq": (_nasdaq, False),
    "cboe": (_cboe, False),
    "stockanalysis": (_stockanalysis, False),
    "marketwatch": (_marketwatch, False),
    "coinbase": (_coinbase, False),   # crypto-only (self-gates)
    "kraken": (_kraken, False),       # crypto-only (self-gates)
    "binance": (_binance, False),     # crypto-only (geo-blocked in US, self-gates)
    "alphavantage": (_alphavantage, True),
    "twelvedata": (_twelvedata, True),
    "fmp": (_fmp, True),
    "polygon": (_polygon, True),
    "alpaca": (_alpaca, True),
}


def cross_validate(ticker: str, lookback: int = TUNABLES.validation_lookback) -> dict:
    """Compare our primary feed to every available reference; report consensus."""
    ticker = ticker.upper()
    now = time.monotonic()
    hit = _CACHE.get(ticker)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    from backend.bot.backtest import fetch_candles

    primary = fetch_candles(ticker, period="3mo", interval="1d")
    if primary.empty:
        result = {"ticker": ticker, "status": "no_primary", "sources": [], "note": "primary feed returned no data."}
        _CACHE[ticker] = (now, result)
        return result

    primary_recent = {str(ts.date()): float(row["Close"]) for ts, row in primary.tail(60).iterrows()}

    sources: List[dict] = []
    needs_key: List[str] = []
    for name, (fetch, requires_key) in PROVIDERS.items():
        try:
            ref = fetch(ticker, 120)
        except Exception as exc:
            logger.warning("provider %s failed: %s", name, exc)
            ref = None
        if ref is None:
            if requires_key and not _has_key(name):
                needs_key.append(name)
            continue
        pairs = [(primary_recent[d], ref[d]) for d in primary_recent if d in ref]
        pairs = pairs[-lookback:]
        if not pairs:
            continue
        diffs = [abs(a - b) / b * 100 for a, b in pairs if b]
        max_d = round(max(diffs), 4)
        sources.append({
            "name": name, "bars": len(pairs),
            "mean_diff_pct": round(sum(diffs) / len(diffs), 4),
            "max_diff_pct": max_d, "agree": max_d < TUNABLES.validation_tolerance_pct,
            "last": round(pairs[-1][1], 2),
        })

    checked = len(sources)
    agree_count = sum(1 for s in sources if s["agree"])
    if checked == 0:
        note = (f"No independent source returned data for {ticker}. "
                f"Add a free key ({', '.join(needs_key) or 'Alpha Vantage / Polygon / Alpaca'}) to cross-check ETFs like SPY.")
        result = {"ticker": ticker, "status": "no_reference", "sources": [], "needs_key": needs_key, "note": note}
    else:
        result = {
            "ticker": ticker, "status": "ok", "primary": "yahoo",
            "checked": checked, "agree_count": agree_count, "agree": agree_count == checked,
            "sources": sources, "needs_key": needs_key,
            "note": (f"Independently confirmed by {agree_count}/{checked} source(s)."
                     if agree_count == checked else f"{checked - agree_count} source(s) diverge — investigate."),
        }
    _CACHE[ticker] = (now, result)
    return result


def _has_key(name: str) -> bool:
    return {
        "alphavantage": bool(os.getenv("ALPHAVANTAGE_API_KEY")),
        "twelvedata": bool(os.getenv("TWELVEDATA_API_KEY")),
        "fmp": bool(os.getenv("FMP_API_KEY")),
        "polygon": bool(os.getenv("POLYGON_API_KEY")),
        "alpaca": bool(os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_API_SECRET")),
    }.get(name, False)
