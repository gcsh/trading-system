"""MITS Phase 11.B.1 — ThetaData v3 stock-bar history client + backfill.

ThetaTerminal v3 exposes two stock-history endpoints on the local
``http://127.0.0.1:25503`` daemon:

  * ``/v3/stock/history/eod``  — daily OHLCV.
    Params: ``symbol``, ``start_date`` (YYYYMMDD), ``end_date`` (YYYYMMDD).
  * ``/v3/stock/history/ohlc`` — intraday OHLCV (Stocks Standard tier).
    Adds ``ivl`` in milliseconds (60000=1m, 300000=5m, 900000=15m, 3600000=60m).

Both return a JSON envelope: ``{"header": {...}, "response": [{"data":
[...]}]}``. We parse `data` rows into normalized dicts so the
SyncOrchestrator can hand them to the silver-layer writer.

The backfill callbacks land rows in TWO places:

  1. Bronze parquet via :func:`backend.bot.data.lake.write_bronze` —
     immutable raw payloads, partitioned by date + ticker.
  2. Silver SQLite table :class:`StockBar` — typed rows ready for
     downstream signal modules. The bronze is the system of record;
     the silver is the in-process query layer.

Failures are surfaced as exceptions so the SyncOrchestrator's retry
envelope kicks in. Empty windows (weekend, holiday, IPO before start)
return :class:`CallbackResult` with ``rows_written=0`` and a
``last_completed_date`` of ``chunk_end`` so the orchestrator marks the
chunk done and moves on instead of looping forever on permanently-empty
windows.
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.stock_bar import StockBar

logger = logging.getLogger(__name__)


class SubscriptionError(RuntimeError):
    """Raised when ThetaData returns 403 because the active subscription
    tier doesn't include the requested endpoint. The orchestrator's
    retry envelope checks for this class and short-circuits the retry
    loop — re-hitting the API won't change a tier entitlement."""


# Interval string → ThetaData ``ivl`` milliseconds.
_INTRADAY_INTERVALS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "60m": 3_600_000,
}


def _base_url() -> str:
    port = int(getattr(TUNABLES, "thetadata_port", 25503))
    return f"http://127.0.0.1:{port}"


def _timeout() -> float:
    return float(getattr(TUNABLES, "thetadata_timeout_sec", 30.0))


# ── HTTP ──────────────────────────────────────────────────────────────


def _http_get(path: str, params: Dict[str, Any]) -> Tuple[int, str]:
    """Plain ``requests.get`` against the local terminal. Returns
    ``(status_code, body)``. Raises on transport errors so the orchestrator
    retries with backoff."""
    import requests
    url = f"{_base_url()}{path}"
    resp = requests.get(url, params=params, timeout=_timeout())
    return (resp.status_code, resp.text)


def _parse_json_envelope(body: str) -> List[Dict[str, Any]]:
    """ThetaData wraps rows in ``{"response": [{"data": [...]}]}``.
    Returns a flat list of data dicts."""
    if not body or body.strip() == "":
        return []
    try:
        payload = json.loads(body)
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    response = payload.get("response") or []
    if isinstance(response, list):
        for envelope in response:
            if not isinstance(envelope, dict):
                continue
            # MITS-P11 — ThetaData v3 JSON shape: ``{"response": [{row}, ...]}``
            # — the row dicts contain OHLC fields directly. Earlier code
            # looked for ``envelope["data"]`` which only exists on v2-era
            # endpoints; the v3 stock/history/eod endpoint returns flat
            # row dicts. Handle BOTH shapes for forward-compat.
            data = envelope.get("data")
            if isinstance(data, list):
                rows.extend(d for d in data if isinstance(d, dict))
            elif any(k in envelope for k in ("open", "close", "high", "low")):
                rows.append(envelope)
    return rows


def _parse_csv_envelope(body: str) -> List[Dict[str, Any]]:
    """ThetaTerminal default is CSV (header + rows). Parses into dicts."""
    import csv
    if not body or body.strip() == "":
        return []
    reader = csv.DictReader(io.StringIO(body))
    return [row for row in reader]


def _parse_bars(body: str) -> List[Dict[str, Any]]:
    """Picks JSON vs CSV by sniffing the first byte."""
    s = body.lstrip()
    if s.startswith("{"):
        return _parse_json_envelope(body)
    return _parse_csv_envelope(body)


# ── normalization ─────────────────────────────────────────────────────


_DATE_FIELDS = (
    "date", "trade_date", "session_date", "session",
    # MITS-P11 — ThetaData v3 CSV uses ISO timestamps in these fields.
    # EOD endpoint returns ``created`` (snapshot time) + ``last_trade``;
    # intraday ohlc returns ``timestamp``. ``_coerce_date`` already
    # handles ISO strings — just need to look up the field name.
    "created", "last_trade", "timestamp",
)
_TS_FIELDS = ("ms_of_day", "ms", "time")
_NUMERIC_FIELDS = ("open", "high", "low", "close", "volume", "vwap")


def _row_bar_ts(row: Dict[str, Any], default_date: Optional[date],
                interval_ms: Optional[int]) -> Optional[datetime]:
    """Resolve the row's bar timestamp.

    EOD endpoint: ThetaData returns a 'date' field as YYYYMMDD int/str.
    Intraday: a 'ms_of_day' (millis since midnight ET) + a 'date'.
    """
    raw_date = None
    raw_iso = None  # MITS-P11 — preserve full ISO when available.
    for f in _DATE_FIELDS:
        if f in row and row[f] not in (None, "", 0):
            raw_date = row[f]
            s = str(row[f])
            # Heuristic: ThetaData v3 CSV format includes "T" in ISO
            # timestamps. If present, capture the full string so the
            # intraday branch can recover hour-minute-second precision
            # instead of falling back to ms_of_day=0.
            if "T" in s:
                raw_iso = s
            break
    bar_date: Optional[date] = default_date
    if raw_date is not None:
        bar_date = _coerce_date(raw_date) or default_date
    if bar_date is None:
        return None
    if interval_ms is None:
        # Daily bar — midnight is fine; the date alone is the identity.
        return datetime(bar_date.year, bar_date.month, bar_date.day)
    # Intraday — prefer the full ISO timestamp when ThetaData v3 returned
    # one in the row (CSV "timestamp" column). Falls back to ms_of_day
    # for the legacy JSON envelope.
    if raw_iso is not None:
        try:
            return datetime.fromisoformat(raw_iso.replace("Z", "").rstrip())
        except Exception:
            pass
    ms = None
    for f in _TS_FIELDS:
        if f in row and row[f] not in (None, ""):
            ms = row[f]
            break
    try:
        ms_int = int(ms) if ms is not None else 0
    except Exception:
        ms_int = 0
    seconds = ms_int / 1000.0
    base = datetime(bar_date.year, bar_date.month, bar_date.day)
    return base + timedelta(seconds=seconds)


def _coerce_date(value: Any) -> Optional[date]:
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except Exception:
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")[:10]).date()
    except Exception:
        return None


def _coerce_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_bar(row: Dict[str, Any], *, ticker: str, interval: str,
                       default_date: Optional[date],
                       interval_ms: Optional[int]) -> Optional[Dict[str, Any]]:
    bar_ts = _row_bar_ts(row, default_date, interval_ms)
    if bar_ts is None:
        return None
    return {
        "ticker": ticker.upper(),
        "interval": interval,
        "bar_ts": bar_ts,
        "open": _coerce_float(row.get("open")),
        "high": _coerce_float(row.get("high")),
        "low": _coerce_float(row.get("low")),
        "close": _coerce_float(row.get("close")),
        "volume": _coerce_float(row.get("volume")),
        "vwap": _coerce_float(row.get("vwap")),
        "trades": _coerce_int(row.get("count") or row.get("trades")),
    }


# ── public API ────────────────────────────────────────────────────────


def fetch_daily_history(ticker: str, start: date, end: date
                           ) -> List[Dict[str, Any]]:
    """Daily OHLCV rows in ``[start, end]``. Empty list on no-data."""
    status, body = _http_get("/v3/stock/history/eod", {
        "symbol": ticker.upper(),
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "format": "json",
    })
    if status == 472:  # ThetaData "no-data" convention.
        return []
    if status == 404:
        return []
    if status == 403:
        raise SubscriptionError(
            f"thetadata daily history requires higher tier "
            f"(ticker={ticker}): {body[:200]}"
        )
    if status != 200:
        raise RuntimeError(
            f"thetadata daily history failed status={status} ticker={ticker} "
            f"window=[{start},{end}] body={body[:200]}"
        )
    raw_rows = _parse_bars(body)
    normalized: List[Dict[str, Any]] = []
    for row in raw_rows:
        bar = _normalize_bar(row, ticker=ticker, interval="1d",
                                default_date=None, interval_ms=None)
        if bar is not None:
            normalized.append(bar)
    return normalized


def fetch_intraday_history(ticker: str, start: date, end: date,
                              interval: str = "1m") -> List[Dict[str, Any]]:
    """Intraday OHLCV rows in ``[start, end]``. Interval is "1m" / "5m" /
    "15m" / "60m". Empty list on no-data."""
    if interval not in _INTRADAY_INTERVALS:
        raise ValueError(f"unsupported intraday interval: {interval}")
    ivl_ms = _INTRADAY_INTERVALS[interval]
    # ThetaData v3 ``interval`` accepts the STRING label ("1m"/"5m"/"15m"/
    # "60m"), NOT milliseconds. Verified empirically against the live
    # terminal on 2026-06-09 — sending the int yields 500 "Invalid
    # interval". ``ivl_ms`` is kept for bar-timestamp arithmetic below.
    status, body = _http_get("/v3/stock/history/ohlc", {
        "symbol": ticker.upper(),
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "interval": interval,
        "format": "json",
    })
    if status in (472, 404):
        return []
    if status == 403:
        # Tier-gated (e.g. Stocks Standard not active on this terminal).
        # Raise SubscriptionError so the orchestrator marks the chunk
        # error WITHOUT exhausting all 6 retries — a missing entitlement
        # won't be remedied by another HTTP call.
        raise SubscriptionError(
            f"thetadata intraday history requires higher tier "
            f"(ticker={ticker} interval={interval}): {body[:200]}"
        )
    if status != 200:
        raise RuntimeError(
            f"thetadata intraday history failed status={status} ticker={ticker} "
            f"interval={interval} window=[{start},{end}] body={body[:200]}"
        )
    raw_rows = _parse_bars(body)
    normalized: List[Dict[str, Any]] = []
    for row in raw_rows:
        bar = _normalize_bar(row, ticker=ticker, interval=interval,
                                default_date=None, interval_ms=ivl_ms)
        if bar is not None:
            normalized.append(bar)
    return normalized


# ── silver writer ─────────────────────────────────────────────────────


def write_silver_bars(bars: Iterable[Dict[str, Any]]) -> int:
    """Bulk upsert into :class:`StockBar`. Returns rows written.

    Idempotent on (ticker, interval, bar_ts) — the unique constraint
    silently drops duplicates so re-running a backfill chunk is a no-op
    on already-landed rows.
    """
    count = 0
    bar_list = [b for b in bars if b and b.get("bar_ts") is not None]
    if not bar_list:
        return 0
    try:
        with session_scope() as s:
            # Pre-load existing keys to dedupe in-memory, then insert in
            # bulk. The unique constraint is the system-of-record; this
            # is just a perf optimization to skip per-row IntegrityErrors.
            tickers = {b["ticker"] for b in bar_list}
            intervals = {b["interval"] for b in bar_list}
            min_ts = min(b["bar_ts"] for b in bar_list)
            max_ts = max(b["bar_ts"] for b in bar_list)
            existing_rows = s.execute(
                select(StockBar.ticker, StockBar.interval, StockBar.bar_ts)
                .where(StockBar.ticker.in_(tickers))
                .where(StockBar.interval.in_(intervals))
                .where(StockBar.bar_ts >= min_ts)
                .where(StockBar.bar_ts <= max_ts)
            ).all()
            existing = {(r[0], r[1], r[2]) for r in existing_rows}
            for b in bar_list:
                key = (b["ticker"], b["interval"], b["bar_ts"])
                if key in existing:
                    continue
                try:
                    s.add(StockBar(
                        ticker=b["ticker"],
                        interval=b["interval"],
                        bar_ts=b["bar_ts"],
                        open=b.get("open"),
                        high=b.get("high"),
                        low=b.get("low"),
                        close=b.get("close"),
                        volume=b.get("volume"),
                        vwap=b.get("vwap"),
                        trades=b.get("trades"),
                        source="thetadata",
                    ))
                    count += 1
                except IntegrityError:
                    s.rollback()
                    continue
    except Exception:
        logger.exception("write_silver_bars failed")
    return count


# ── bronze writer ─────────────────────────────────────────────────────


def write_bronze_bars(ticker: str, interval: str, rows: List[Dict[str, Any]],
                          *, chunk_start: date, chunk_end: date) -> None:
    """Hand the raw rows to the lake bronze writer. Best-effort —
    bronze writes are fire-and-forget (silver is the SQL-side
    system-of-record)."""
    if not rows:
        return
    try:
        from backend.bot.data import lake as _lake
        # Re-shape into JSON-safe dicts (ISO ts strings).
        payload = []
        for r in rows:
            row = dict(r)
            ts = row.get("bar_ts")
            if hasattr(ts, "isoformat"):
                row["bar_ts"] = ts.isoformat()
            payload.append(row)
        _lake.write_bronze(
            source="thetadata",
            dtype=f"stock_bars_{interval}",
            rows=payload,
            ticker=ticker,
            extra_tags={
                "interval": interval,
                "chunk_start": chunk_start.isoformat(),
                "chunk_end": chunk_end.isoformat(),
            },
            request_url=f"thetadata://v3/stock/history/{interval}",
            source_version=__name__,
        )
    except Exception:
        logger.debug("bronze write failed for %s %s [%s,%s]",
                      ticker, interval, chunk_start, chunk_end,
                      exc_info=True)


# ── orchestrator callbacks ────────────────────────────────────────────


def daily_backfill_callback(ticker: str, chunk_start: date,
                                chunk_end: date) -> CallbackResult:
    """Pulls 1d bars for ``[chunk_start, chunk_end]``, writes silver +
    bronze, returns the orchestrator-shaped result."""
    rows = fetch_daily_history(ticker, chunk_start, chunk_end)
    if not rows:
        # No data — typical for pre-IPO windows. Mark chunk done.
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "no_data"},
        )
    written = write_silver_bars(rows)
    write_bronze_bars(ticker, "1d", rows,
                          chunk_start=chunk_start, chunk_end=chunk_end)
    last_dt = max(r["bar_ts"].date() for r in rows
                   if r.get("bar_ts") is not None)
    return CallbackResult(
        last_completed_date=last_dt,
        rows_written=written,
        metadata={"raw_rows": len(rows)},
    )


def intraday_backfill_callback_factory(interval: str
                                              ) -> Callable[[str, date, date], CallbackResult]:
    """Returns a callback bound to ``interval`` (1m/5m/15m/60m). The
    orchestrator wires one of these per interval."""

    def _callback(ticker: str, chunk_start: date,
                       chunk_end: date) -> CallbackResult:
        rows = fetch_intraday_history(ticker, chunk_start, chunk_end,
                                            interval=interval)
        if not rows:
            return CallbackResult(
                last_completed_date=chunk_end,
                rows_written=0,
                metadata={"reason": "no_data", "interval": interval},
            )
        written = write_silver_bars(rows)
        write_bronze_bars(ticker, interval, rows,
                              chunk_start=chunk_start, chunk_end=chunk_end)
        last_dt = max(r["bar_ts"].date() for r in rows
                       if r.get("bar_ts") is not None)
        return CallbackResult(
            last_completed_date=last_dt,
            rows_written=written,
            metadata={"raw_rows": len(rows), "interval": interval},
        )

    return _callback


__all__ = [
    "fetch_daily_history",
    "fetch_intraday_history",
    "write_silver_bars",
    "write_bronze_bars",
    "daily_backfill_callback",
    "intraday_backfill_callback_factory",
]
