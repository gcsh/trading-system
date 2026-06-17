"""IV history capture + percentile-rank computation.

The bot needs *real* IV rank — today's IV expressed as a percentile of
the last N trading days — not the linear estimate ``_iv_rank_estimate``
that was a placeholder until we had history.

Two write paths populate the ``iv_history`` table:

  1. **Live capture** — :func:`record_today` is called from
     ``options.options_snapshot`` whenever a fresh IV is computed.
     One row per (ticker, date). Idempotent: re-writing the same day
     for the same ticker is a no-op (UNIQUE constraint).

  2. **Historical backfill** — :func:`backfill` walks ThetaData's
     ``/v3/option/history/eod`` endpoint to populate up to 8 years per
     ticker. Designed to run as a one-off (``python -m
     backend.bot.data.iv_history --backfill --ticker AAPL``) or in a
     background task on first deploy of a new ticker.

The read path is :func:`iv_percentile_rank`, which computes percentile
rank from the table. Returns ``None`` when sample size is below the
``min_samples`` floor so callers can fall back to the linear estimator
during the corpus-warm-up window.
"""
from __future__ import annotations

import argparse
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional

from sqlalchemy import select, func

from backend.db import session_scope
from backend.models.iv_history import IVHistory

logger = logging.getLogger(__name__)


_BS_STRADDLE_K = math.sqrt(2.0 / math.pi)  # ≈ 0.7979 — Brenner-Subrahmanyam


# ── shapes ──────────────────────────────────────────────────────────────


@dataclass
class IVRankResult:
    rank: float  # 0..100
    sample_count: int
    lookback_days: int
    estimated: bool  # True only if we fell back to the linear estimator


# ── live capture ────────────────────────────────────────────────────────


def record_today(ticker: str, iv_atm: float, *,
                    expiry_used: Optional[date] = None,
                    dte_used: Optional[int] = None,
                    source: str = "live") -> None:
    """Idempotent: write today's ATM IV for ``ticker``. Silent on conflict
    so callers don't have to worry about race conditions inside a cycle."""
    if iv_atm is None or iv_atm <= 0:
        return
    today_dt = datetime.combine(date.today(), datetime.min.time())
    from sqlalchemy.exc import IntegrityError
    try:
        with session_scope() as s:
            existing = s.execute(
                select(IVHistory)
                .where(IVHistory.ticker == ticker.upper())
                .where(IVHistory.date == today_dt)
            ).scalar_one_or_none()
            if existing is not None:
                # Latest value of the day wins. Cheap intraday updates.
                existing.iv_atm = float(iv_atm)
                if expiry_used is not None:
                    existing.expiry_used = datetime.combine(
                        expiry_used, datetime.min.time())
                if dte_used is not None:
                    existing.dte_used = int(dte_used)
                existing.fetched_at = datetime.utcnow()
                return
            row = IVHistory(
                ticker=ticker.upper(),
                date=today_dt,
                iv_atm=float(iv_atm),
                expiry_used=(datetime.combine(expiry_used, datetime.min.time())
                                if expiry_used else None),
                dte_used=int(dte_used) if dte_used is not None else None,
                source=source,
            )
            s.add(row)
    except IntegrityError:
        # IV.FIX (2026-06-05) — two concurrent cycles can both pass the
        # SELECT and race the INSERT. The unique constraint is doing
        # exactly what we want — another writer won. Re-fetch and update
        # the existing row so the latest value still lands; demote the
        # log to DEBUG since this is the idempotent guarantee working.
        logger.debug("record_today race on %s — merging into existing row",
                          ticker)
        try:
            with session_scope() as s2:
                existing = s2.execute(
                    select(IVHistory)
                    .where(IVHistory.ticker == ticker.upper())
                    .where(IVHistory.date == today_dt)
                ).scalar_one_or_none()
                if existing is not None:
                    existing.iv_atm = float(iv_atm)
                    if expiry_used is not None:
                        existing.expiry_used = datetime.combine(
                            expiry_used, datetime.min.time())
                    if dte_used is not None:
                        existing.dte_used = int(dte_used)
                    existing.fetched_at = datetime.utcnow()
        except Exception:
            logger.debug("record_today merge-after-race failed for %s",
                              ticker, exc_info=True)
    except Exception:
        logger.warning("record_today failed for %s", ticker, exc_info=True)


# ── ranking ─────────────────────────────────────────────────────────────


def iv_percentile_rank(ticker: str, current_iv: float, *,
                            lookback_days: int = 252,
                            min_samples: int = 20) -> Optional[IVRankResult]:
    """Percentile of ``current_iv`` within the last ``lookback_days`` of
    history for ``ticker``. Returns ``None`` when fewer than
    ``min_samples`` historical observations exist — caller should fall
    back to the linear estimator until the corpus warms up."""
    if current_iv is None or current_iv <= 0:
        return None
    cutoff_dt = datetime.combine(
        date.today() - timedelta(days=lookback_days), datetime.min.time())
    try:
        with session_scope() as s:
            rows = s.execute(
                select(IVHistory.iv_atm)
                .where(IVHistory.ticker == ticker.upper())
                .where(IVHistory.date >= cutoff_dt)
                .where(IVHistory.iv_atm.is_not(None))
            ).scalars().all()
    except Exception:
        logger.warning("iv_percentile_rank query failed for %s",
                          ticker, exc_info=True)
        return None
    values = [float(v) for v in rows if v is not None and v > 0]
    if len(values) < min_samples:
        return None
    below = sum(1 for v in values if v < current_iv)
    rank = round(below * 100.0 / len(values), 1)
    return IVRankResult(
        rank=rank,
        sample_count=len(values),
        lookback_days=lookback_days,
        estimated=False,
    )


# ── backfill ────────────────────────────────────────────────────────────


def _atm_iv_on_date(client, ticker: str, target_date: date,
                        spot_estimate: float,
                        target_dte: int = 30,
                        min_dte: int = 7) -> Optional[tuple]:
    """For ``target_date``, find the expiration that was closest to
    ``target_dte`` days out (relative to target_date), find the strike
    closest to ``spot_estimate``, fetch EOD call + put on target_date,
    return (iv_atm, expiry, dte) or None.

    ``spot_estimate`` is the underlying's close on target_date — caller
    provides it (typically from yfinance historical bars).
    """
    if spot_estimate is None or spot_estimate <= 0:
        return None
    # All-ever expirations (ThetaData returns historical + current)
    all_expirations = client.list_expirations(ticker)
    # Candidate: expirations that were future-from-target_date AND meet
    # the minimum DTE floor on target_date.
    candidates = [
        e for e in all_expirations
        if (e - target_date).days >= min_dte
    ]
    if not candidates:
        return None
    expiry = min(candidates, key=lambda e: abs((e - target_date).days - target_dte))
    dte = (expiry - target_date).days

    strikes = client.list_strikes(ticker, expiry)
    if not strikes:
        return None
    atm = min(strikes, key=lambda s: abs(s - spot_estimate))

    # Fetch EOD bars for both legs on target_date.
    call_bar = _eod_one_day(client, ticker, expiry, atm, "C", target_date)
    put_bar = _eod_one_day(client, ticker, expiry, atm, "P", target_date)
    if not call_bar or not put_bar:
        return None
    call_close = call_bar.get("close") or call_bar.get("c") or 0.0
    put_close = put_bar.get("close") or put_bar.get("c") or 0.0
    straddle = float(call_close) + float(put_close)
    if straddle <= 0:
        return None
    T = max(1, dte) / 365.0
    iv = straddle / (_BS_STRADDLE_K * atm * math.sqrt(T))
    if iv <= 0 or iv > 5.0:
        return None  # clearly bogus — skip
    return (round(iv, 4), expiry, dte)


def _eod_one_day(client, ticker: str, expiry: date, strike: float,
                    right: str, target_date: date) -> Optional[dict]:
    """Hit /v3/option/history/eod with start=end=target_date for one
    contract leg. Returns the single row's dict or None."""
    payload = client._get_json(  # noqa: SLF001 — internal access acceptable
        "/v3/option/history/eod",
        {
            "symbol": ticker.upper(),
            "expiration": expiry.isoformat(),
            "strike": f"{float(strike):.3f}",
            "right": "C" if right.upper().startswith("C") else "P",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "format": "json",
        },
    )
    if not payload:
        return None
    rows = payload.get("response") or []
    if not rows:
        return None
    first = rows[0]
    data = (first.get("data") or [{}])
    return data[0] if data else None


_HIST_CLOSES_CACHE: dict = {}


def _thetadata_historical_closes(ticker: str, start: date, end: date) -> dict:
    """Stock daily closes via the local ThetaData terminal
    (``/v3/stock/history/eod``). Our Options Standard subscription
    includes stock EOD — verified 2026-06-03. Preferred source: no
    rate-limits, no crumb dance, runs on the same box.

    The terminal returns CSV by default (header + rows). We bypass
    ``client._get_json`` and read the response directly so we can
    parse either format."""
    try:
        import requests
        from backend.config import TUNABLES
        port = getattr(TUNABLES, "thetadata_port", 25503)
        url = f"http://127.0.0.1:{port}/v3/stock/history/eod"
        resp = requests.get(url, params={
            "symbol": ticker.upper(),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }, timeout=15)
        if resp.status_code != 200:
            return {}
        body = resp.text.strip()
        if not body:
            return {}
        out: dict = {}
        # Try JSON first (some terminals return JSON envelope).
        if body.startswith("{"):
            import json as _json
            payload = _json.loads(body)
            for envelope in payload.get("response") or []:
                for row in envelope.get("data") or []:
                    ts = row.get("created") or row.get("last_trade")
                    close = row.get("close")
                    if not ts or close in (None, 0):
                        continue
                    d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                    out[d] = float(close)
            return out
        # CSV parsing — first line is header.
        import csv, io
        reader = csv.DictReader(io.StringIO(body))
        for row in reader:
            ts = row.get("created") or row.get("last_trade")
            close_str = row.get("close")
            if not ts or not close_str:
                continue
            try:
                close = float(close_str)
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            try:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except Exception:
                continue
            out[d] = close
        return out
    except Exception:
        logger.warning("thetadata stock EOD failed for %s",
                          ticker, exc_info=True)
        return {}


def _alpaca_historical_closes(ticker: str, start: date, end: date) -> dict:
    """Stock daily closes via Alpaca's free Market Data v2 IEX feed.
    Alpaca is much more reliable than yfinance for backfill loops
    (no crumb/cookie dance, 200 req/min paper-tier ceiling)."""
    try:
        from backend.config import SETTINGS
        if not (SETTINGS.alpaca_api_key and SETTINGS.alpaca_api_secret):
            return {}
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = StockHistoricalDataClient(
            api_key=SETTINGS.alpaca_api_key,
            secret_key=SETTINGS.alpaca_api_secret,
        )
        req = StockBarsRequest(
            symbol_or_symbols=ticker.upper(),
            timeframe=TimeFrame.Day,
            start=datetime(start.year, start.month, start.day),
            end=datetime(end.year, end.month, end.day) + timedelta(days=1),
        )
        bars = client.get_stock_bars(req)
        out: dict = {}
        for bar in bars.data.get(ticker.upper(), []) or []:
            ts = bar.timestamp
            d = ts.date() if hasattr(ts, "date") else ts
            close = float(getattr(bar, "close", 0) or 0)
            if close > 0:
                out[d] = close
        return out
    except Exception:
        logger.warning("alpaca historical closes failed for %s",
                          ticker, exc_info=True)
        return {}


def _historical_closes(ticker: str, start: date, end: date) -> dict:
    """Fetch underlying historical daily closes. Returns
    ``{date: close_price}``. Source preference:
      1. **ThetaData** (``/v3/stock/history/eod``) — preferred. No
         rate-limits, no crumb dance, runs on the same box. Our
         Options Standard subscription covers stock EOD.
      2. **Alpaca** — fallback if ThetaData terminal is unreachable
         AND ``ALPACA_API_KEY``/``ALPACA_API_SECRET`` are set.
      3. **yfinance** — last resort. Brittle (crumb-bans the IP under
         load) but covers tickers Alpaca might miss.

    Cached in-process keyed by (ticker, start, end) — backfill loops
    over many (ticker, strategy) cells re-request the same window."""
    key = (ticker.upper(), start, end)
    if key in _HIST_CLOSES_CACHE:
        return _HIST_CLOSES_CACHE[key]

    out = _thetadata_historical_closes(ticker, start, end)
    if out:
        _HIST_CLOSES_CACHE[key] = out
        return out

    out = _alpaca_historical_closes(ticker, start, end)
    if out:
        _HIST_CLOSES_CACHE[key] = out
        return out

    # Fallback to yfinance. Two code paths:
    #   1. ``yf.download()`` — batch endpoint, bypasses the per-symbol
    #      crumb dance that ``Ticker.history()`` triggers and tends to be
    #      more resilient for backfill-style bursts.
    #   2. ``Ticker.history()`` — the historical interactive path, used
    #      when ``yf.download`` returns empty.
    import time as _time

    last_exc = None
    for attempt in range(3):
        try:
            import yfinance as yf
            df = yf.download(
                tickers=ticker, start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False, progress=False, threads=False,
                group_by="column",
            )
            if df is not None and not df.empty:
                # download() can return multi-level columns (Close → ticker)
                # depending on yfinance version. Flatten to a simple series.
                close_series = None
                try:
                    close_series = df["Close"]
                    # If multi-index columns, pick the ticker column.
                    if hasattr(close_series, "columns"):
                        close_series = close_series[ticker]
                except Exception:
                    close_series = None
                out = {}
                if close_series is not None:
                    for ts, val in close_series.dropna().items():
                        # ts may be Timestamp, date, or string depending on yf version.
                        if hasattr(ts, "date"):
                            d = ts.date()
                        elif isinstance(ts, str):
                            try:
                                d = datetime.strptime(ts[:10], "%Y-%m-%d").date()
                            except Exception:
                                continue
                        else:
                            d = ts
                        try:
                            v = float(val)
                        except (TypeError, ValueError):
                            continue
                        if v > 0:
                            out[d] = v
                if out:
                    _HIST_CLOSES_CACHE[key] = out
                    return out
            # Fallback to per-symbol Ticker.history.
            span_days = (end - start).days
            period = None
            if span_days <= 31:    period = "1mo"
            elif span_days <= 92:  period = "3mo"
            elif span_days <= 185: period = "6mo"
            elif span_days <= 380: period = "1y"
            elif span_days <= 760: period = "2y"
            elif span_days <= 1900: period = "5y"
            tk = yf.Ticker(ticker)
            hist = tk.history(period=period, auto_adjust=False) if period else tk.history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )
            if hist is not None and not hist.empty:
                out = {}
                for ts, row in hist.iterrows():
                    d = ts.date() if hasattr(ts, "date") else ts
                    if d < start or d > end:
                        continue
                    close = row.get("Close")
                    if close is not None and close > 0:
                        out[d] = float(close)
                if out:
                    _HIST_CLOSES_CACHE[key] = out
                    return out
            # Both paths returned empty; back off and retry.
            if attempt < 2:
                _time.sleep(20 * (attempt + 1))
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                _time.sleep(20 * (attempt + 1))
    logger.warning("yfinance historical closes failed for %s after retries: %s",
                      ticker, last_exc)
    _HIST_CLOSES_CACHE[key] = {}
    return {}


def backfill(ticker: str, *, lookback_days: int = 365,
                target_dte: int = 30,
                pace_seconds: float = 0.05) -> dict:
    """One-off historical fill: populate ``iv_history`` for ``ticker``
    going back ``lookback_days`` calendar days.

    Returns ``{"inserted": N, "skipped": M, "errors": K}`` so the caller
    can confirm what happened. Skipped includes weekends/holidays where
    no underlying close was returned by yfinance.
    """
    from backend.bot.data.thetadata import get_client
    client = get_client()

    end = date.today()
    start = end - timedelta(days=lookback_days)
    closes = _historical_closes(ticker, start, end)
    if not closes:
        logger.warning("backfill aborted: no historical closes for %s", ticker)
        return {"inserted": 0, "skipped": 0, "errors": 1}

    # Skip dates we already have.
    with session_scope() as s:
        cutoff_dt = datetime.combine(start, datetime.min.time())
        existing_rows = s.execute(
            select(IVHistory.date)
            .where(IVHistory.ticker == ticker.upper())
            .where(IVHistory.date >= cutoff_dt)
        ).scalars().all()
        existing_dates = {
            d.date() if hasattr(d, "date") else d for d in existing_rows
        }

    inserted = 0
    skipped = 0
    errors = 0
    sorted_dates = sorted(closes.keys())
    for d in sorted_dates:
        if d in existing_dates:
            skipped += 1
            continue
        spot = closes[d]
        try:
            result = _atm_iv_on_date(
                client, ticker, d, spot, target_dte=target_dte,
            )
        except Exception:
            errors += 1
            continue
        if result is None:
            skipped += 1
            continue
        iv_value, expiry, dte = result
        try:
            with session_scope() as s:
                row = IVHistory(
                    ticker=ticker.upper(),
                    date=datetime.combine(d, datetime.min.time()),
                    iv_atm=iv_value,
                    expiry_used=datetime.combine(expiry, datetime.min.time()),
                    dte_used=dte,
                    source="backfill",
                )
                s.add(row)
            inserted += 1
        except Exception:
            errors += 1
        # Pace to be nice to the terminal even though Standard tier is unlimited.
        if pace_seconds > 0:
            time.sleep(pace_seconds)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


# ── CLI ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":  # pragma: no cover — manual ops helper
    parser = argparse.ArgumentParser(description="IV history maintenance")
    parser.add_argument("--backfill", action="store_true",
                            help="Run historical backfill for --ticker.")
    parser.add_argument("--ticker", type=str, required=True,
                            help="Ticker symbol (e.g. AAPL).")
    parser.add_argument("--days", type=int, default=365,
                            help="Lookback window in calendar days.")
    args = parser.parse_args()

    if args.backfill:
        result = backfill(args.ticker, lookback_days=args.days)
        print(f"backfill {args.ticker}: {result}")
    else:
        parser.print_help()
