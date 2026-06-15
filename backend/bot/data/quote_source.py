"""P3.3 — Hierarchical price source with freshness tags.

Unified quote resolver. Callers receive a ``Quote(price, source,
age_seconds)`` instead of a bare float, so downstream consumers can
make freshness-aware decisions (skip exits on a stale print, downgrade
the snapshot's data_quality, etc.).

Source order (each fallback fires only when the prior failed):
  1. ThetaData stock quote (when our subscription includes it; cheap
     local terminal call, age usually < 5s)
  2. Alpaca stock quote (when creds are configured)
  3. yfinance intraday 1m bar (last close)
  4. yfinance previous close (last resort — explicit "stale" tag)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    price: float
    # source ∈ {thetadata, alpaca, yfinance_intraday, yfinance_stale,
    #           yfinance_previous, unknown}. MITS Phase 17.A item #13
    # added ``yfinance_stale`` — emitted when the intraday 1m bar is
    # older than 5 minutes so downstream consumers (engine, exits) can
    # refuse to act on a print that doesn't represent the live tape.
    source: str
    age_seconds: Optional[float]

    def is_fresh(self, max_age: float = 60.0) -> bool:
        if self.age_seconds is None:
            return False
        return self.age_seconds <= max_age


def _thetadata_quote(ticker: str) -> Optional[Quote]:
    """Stock quote from the ThetaData terminal.

    2026-06-15 — enabled now that subscription is upgraded to
    ``Stock: STANDARD``. Hits the v3 snapshot endpoints:

      * ``/v3/stock/snapshot/trade?symbol=…`` — most recent print
        (price, timestamp). Preferred — actual transaction.
      * ``/v3/stock/snapshot/quote?symbol=…`` — NBBO. Fallback when
        no trade row (rare, but happens pre-open).

    Response is CSV by default:
        timestamp,symbol,sequence,size,condition,price
        2026-06-12T19:59:48.664,"SPY",95251360,60,1,742.450

    Falls through to None on any error → caller falls back to Alpaca,
    then yfinance.
    """
    try:
        from backend.config import TUNABLES
        import csv as _csv
        import io as _io
        import requests as _r
        from datetime import datetime as _dt, timezone as _tz

        port = int(getattr(TUNABLES, "thetadata_port", 25503))
        timeout = float(getattr(TUNABLES, "thetadata_timeout_sec", 4.0))
        base = f"http://127.0.0.1:{port}"
        sym = ticker.upper().strip()
        if not sym:
            return None

        def _parse_csv(body: str):
            if not body or not body.strip():
                return []
            reader = _csv.DictReader(_io.StringIO(body))
            return list(reader)

        def _age(ts_str: Optional[str]) -> Optional[float]:
            if not ts_str:
                return None
            try:
                # ThetaData emits naive timestamps in UTC (per their
                # docs); parse + tag UTC so the math is right.
                ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                now = _dt.now(_tz.utc)
                return max(0.0, (now - ts).total_seconds())
            except Exception:
                return None

        # 1) Last trade (preferred — actual transaction price).
        try:
            r = _r.get(f"{base}/v3/stock/snapshot/trade",
                          params={"symbol": sym}, timeout=timeout)
            if r.status_code == 200 and r.text:
                rows = _parse_csv(r.text)
                if rows:
                    row = rows[0]
                    price = float(row.get("price") or 0)
                    if price > 0:
                        return Quote(
                            price=round(price, 4),
                            source="thetadata",
                            age_seconds=_age(row.get("timestamp")),
                        )
        except Exception:
            logger.debug("thetadata last-trade failed for %s",
                              ticker, exc_info=True)

        # 2) Quote midpoint (fallback when no recent trade row).
        try:
            r = _r.get(f"{base}/v3/stock/snapshot/quote",
                          params={"symbol": sym}, timeout=timeout)
            if r.status_code == 200 and r.text:
                rows = _parse_csv(r.text)
                if rows:
                    row = rows[0]
                    bid = float(row.get("bid") or 0)
                    ask = float(row.get("ask") or 0)
                    mid = ((bid + ask) / 2.0 if (bid > 0 and ask > 0)
                              else (bid or ask))
                    if mid > 0:
                        return Quote(
                            price=round(mid, 4),
                            source="thetadata",
                            age_seconds=_age(row.get("timestamp")),
                        )
        except Exception:
            logger.debug("thetadata nbbo failed for %s",
                              ticker, exc_info=True)
    except Exception:
        logger.debug("thetadata quote outer failed for %s",
                          ticker, exc_info=True)
    return None


def _alpaca_quote(ticker: str) -> Optional[Quote]:
    """Last trade from Alpaca's free IEX feed. Used as fallback to
    yfinance when keys are present."""
    try:
        from backend.config import SETTINGS
        if not (SETTINGS.alpaca_api_key and SETTINGS.alpaca_api_secret):
            return None
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        client = StockHistoricalDataClient(
            api_key=SETTINGS.alpaca_api_key,
            secret_key=SETTINGS.alpaca_api_secret,
        )
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker.upper())
        resp = client.get_stock_latest_quote(req)
        q = resp.get(ticker.upper()) if resp else None
        if q is None:
            return None
        # Mid of bid/ask.
        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask)
        if mid <= 0:
            return None
        ts = getattr(q, "timestamp", None)
        age = None
        if ts is not None:
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                age = (now - ts).total_seconds()
            except Exception:
                age = None
        return Quote(price=round(mid, 4), source="alpaca", age_seconds=age)
    except Exception:
        logger.debug("alpaca quote failed for %s", ticker, exc_info=True)
        return None


def _yfinance_intraday(ticker: str) -> Optional[Quote]:
    """Most recent 1m bar's close. Age is approximately
    (now - bar.end_of_minute)."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="1d", interval="1m",
                                                  prepost=False, auto_adjust=False)
        if hist is None or hist.empty:
            return None
        last_bar = hist.iloc[-1]
        price = float(last_bar.get("Close", 0) or 0)
        if price <= 0:
            return None
        # Index is timezone-aware; convert to UTC seconds.
        ts = hist.index[-1]
        try:
            age = max(0.0,
                          time.time() - ts.to_pydatetime().timestamp())
        except Exception:
            age = None
        # MITS Phase 17.A item #13 — qualify the source tag when the
        # intraday bar is older than 5 minutes (or age can't be
        # computed at all). Without this tag the engine treats a 30-
        # minute-old print the same as a 10-second-old one.
        source = (
            "yfinance_stale"
            if (age is None or age > 300)
            else "yfinance_intraday"
        )
        return Quote(price=round(price, 4),
                         source=source, age_seconds=age)
    except Exception:
        logger.debug("yfinance intraday failed for %s",
                          ticker, exc_info=True)
        return None


def _yfinance_previous_close(ticker: str) -> Optional[Quote]:
    """Yesterday's daily close. Last resort — always carries an explicit
    'stale' age so the engine can refuse to act on it for entries."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="5d", interval="1d",
                                                  auto_adjust=False)
        if hist is None or hist.empty:
            return None
        price = float(hist["Close"].iloc[-1] or 0)
        if price <= 0:
            return None
        # Age = at least the time since the previous close. Conservative
        # estimate of 6h so callers treat it as "definitely stale".
        return Quote(price=round(price, 4),
                         source="yfinance_previous",
                         age_seconds=6 * 3600.0)
    except Exception:
        logger.debug("yfinance previous close failed for %s",
                          ticker, exc_info=True)
        return None


def get_quote(ticker: str) -> Quote:
    """Single entry point. Returns the freshest available quote with
    full provenance. Never raises — falls back to a stub if everything
    fails (price=0, source=unknown, age=None) so the caller can decide
    whether to refuse to act."""
    for fetch in (_thetadata_quote, _alpaca_quote,
                       _yfinance_intraday, _yfinance_previous_close):
        try:
            q = fetch(ticker)
            if q is not None and q.price > 0:
                return q
        except Exception:
            continue
    return Quote(price=0.0, source="unknown", age_seconds=None)
