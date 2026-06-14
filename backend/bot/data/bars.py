"""MITS Phase 4 (P4.3) — Unified OHLCV bar fetcher with ThetaData→yfinance fallback.

Single entry point so the analysis route + EOD pass + any future bar
consumer all sit on the same priority chain:

  1. **ThetaData v3** (``/v3/stock/history/eod`` for daily,
     ``/v3/stock/history/ohlc`` for intraday). Authoritative when
     subscribed; subject to Standard-tier endpoint availability.
  2. **yfinance** — broad coverage but flaky after-hours (the failure
     mode that prompted the 2026-06-02 cutover).

Each call returns ``{"bars": [...], "source": "thetadata"|"yfinance",
"interval": "...", "window": "..."}``. Callers downstream tag the
response with ``bar_source`` so the operator can see at a glance which
provider answered.

Bar shape (one dict per row):

  ``{"t": ISO8601 str, "open": float, "high": float, "low": float,
     "close": float, "volume": float}``

The DataFrame accessor ``fetch_bars_df`` returns the parallel pandas
DataFrame for callers that prefer the original shape (detectors).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── window → (interval, lookback) resolution ──────────────────────────


# Defaults match the analysis route's existing yfinance presets so we
# don't change render behaviour when ThetaData answers vs falls back.
_WINDOW_PRESETS: Dict[str, Tuple[str, int]] = {
    # window slug -> (default interval, default lookback days)
    # `today` looks back 3 days so weekend / holiday visits land on the
    # most recent trading session (Friday) instead of returning empty.
    # The frontend already trims to "today" client-side via timeframe
    # selector when the operator wants a strict intraday view.
    "today": ("5m", 3),
    "5d":    ("15m", 5),
    # Long-range presets — daily bars. The frontend timeframe selector
    # picks the right slug per UI button so a 3Y view returns 3 years
    # of bars instead of the legacy 30-day `all` ceiling that made 3Y/
    # 5Y/MAX look like 6 months.
    "1m":    ("1d", 31),
    "3m":    ("1d", 95),
    "6m":    ("1d", 185),
    "1y":    ("1d", 370),
    "3y":    ("1d", 3 * 366),
    "5y":    ("1d", 5 * 366),
    "max":   ("1d", 15 * 366),
    # `all` — legacy short hourly preset retained for back-compat with
    # callers that send window=all expecting intraday hourly bars.
    "all":   ("1h", 30),
}


def _resolve_window(window: str, interval: Optional[str] = None
                       ) -> Tuple[str, int]:
    w = (window or "today").lower()
    iv_default, lookback = _WINDOW_PRESETS.get(w, _WINDOW_PRESETS["today"])
    iv = (interval or iv_default).lower()
    return iv, lookback


# ── ThetaData fetcher ────────────────────────────────────────────────


def _thetadata_base_url() -> str:
    return os.environ.get(
        "THETADATA_BASE_URL", "http://127.0.0.1:25503",
    ).rstrip("/")


def _interval_to_ms(interval: str) -> Optional[int]:
    """Map yfinance-style interval to ThetaData OHLC milliseconds."""
    m = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "60m": 3_600_000,
    }
    return m.get(interval.lower())


def _interval_to_label(interval: str) -> Optional[str]:
    """Map yfinance-style interval to the ThetaData v3 ``interval``
    query-param STRING label. v3 deprecated ``ivl`` (numeric ms) in
    favour of ``interval`` with labels like ``"1m"`` / ``"5m"`` /
    ``"15m"`` / ``"60m"``. Sister module ``thetadata_stocks.py`` uses
    the same idiom — verified empirically against the live terminal."""
    m = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "60m",
        "60m": "60m",
    }
    return m.get(interval.lower())


def _fetch_bars_thetadata(
    ticker: str, *, interval: str, lookback_days: int,
) -> Optional[List[Dict[str, Any]]]:
    """Try ThetaData first. Returns ``None`` on timeout, 4xx/5xx, or
    empty payload so the caller can fall back."""
    try:
        import requests
    except Exception:
        return None
    try:
        end = date.today()
        start = end - timedelta(days=max(1, lookback_days))
        base = _thetadata_base_url()
        # ThetaData v3 endpoints expect compact YYYYMMDD; ISO form
        # (`YYYY-MM-DD`) silently returns 4xx and the caller falls
        # back to yfinance. Sister module `thetadata_stocks.py`
        # already uses this form — staying consistent kills the
        # silent fallback that hid behind the "bar_source=yfinance"
        # pill on long-range analysis windows.
        start_compact = start.strftime("%Y%m%d")
        end_compact = end.strftime("%Y%m%d")
        if interval == "1d":
            url = f"{base}/v3/stock/history/eod"
            params = {
                "symbol": ticker.upper(),
                "start_date": start_compact,
                "end_date": end_compact,
                "format": "json",
            }
        else:
            label = _interval_to_label(interval)
            if label is None:
                return None
            url = f"{base}/v3/stock/history/ohlc"
            params = {
                "symbol": ticker.upper(),
                "start_date": start_compact,
                "end_date": end_compact,
                "interval": label,
                "venue": "utp_cta",
                "format": "json",
            }
        r = requests.get(url, params=params, timeout=4.0)
        if r.status_code != 200:
            return None
        body = r.json() or {}
        rows = body.get("response") or []
        if not rows:
            return None
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                data = row.get("data") or row
                if isinstance(data, list):
                    # Some ThetaData v3 shapes use a top-level "data" list.
                    inner = data
                else:
                    inner = [data]
                for d in inner:
                    ts_raw = (
                        d.get("timestamp")
                        or d.get("date")
                        or d.get("ms_of_day")
                    )
                    ts = _parse_theta_timestamp(ts_raw, d.get("date"))
                    if ts is None:
                        continue
                    o = float(d.get("open") or 0.0)
                    h = float(d.get("high") or 0.0)
                    lo = float(d.get("low") or 0.0)
                    c = float(d.get("close") or 0.0)
                    # ThetaData emits zero-priced rows for non-trading
                    # minutes (weekends, halts, pre-listing). Without
                    # this drop the UI receives an array of zero-mid
                    # candles that lightweight-charts can't render into
                    # a meaningful price scale — the chart paints empty.
                    if o <= 0 and h <= 0 and lo <= 0 and c <= 0:
                        continue
                    out.append({
                        "t": ts.isoformat(),
                        "open": o,
                        "high": h,
                        "low": lo,
                        "close": c,
                        "volume": float(d.get("volume") or 0.0),
                    })
            except Exception:
                continue
        out.sort(key=lambda b: b["t"])
        return out or None
    except Exception as exc:
        logger.debug("thetadata bars fetch failed for %s: %s", ticker, exc)
        return None


def _parse_theta_timestamp(ts_raw: Any, date_hint: Any = None
                                  ) -> Optional[datetime]:
    if ts_raw is None:
        return None
    if isinstance(ts_raw, (int, float)):
        # ThetaData ms-of-day requires a date hint. Skip if absent.
        if date_hint is None:
            return None
        try:
            d = (datetime.strptime(str(date_hint), "%Y-%m-%d").date()
                  if not isinstance(date_hint, date) else date_hint)
            return datetime(d.year, d.month, d.day) + timedelta(
                milliseconds=int(ts_raw))
        except Exception:
            return None
    s = str(ts_raw)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


# ── yfinance fetcher ─────────────────────────────────────────────────


def _yf_period_for(lookback_days: int) -> str:
    """yfinance treats period as a slug. Map our lookback to the
    closest available period.

    Supported yfinance period slugs: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y,
    10y, ytd, max. We round UP to the nearest slug so the caller never
    gets less history than requested.

    Phase-10 fix: previous implementation capped at ``"6mo"`` which
    silently truncated the Theory Studio ``5y`` / ``max`` windows to
    ~125 daily bars. The mapping below honours the full range.
    """
    if lookback_days <= 1:
        return "1d"
    if lookback_days <= 5:
        return "5d"
    if lookback_days <= 30:
        return "1mo"
    if lookback_days <= 90:
        return "3mo"
    if lookback_days <= 180:
        return "6mo"
    if lookback_days <= 365:
        return "1y"
    if lookback_days <= 730:
        return "2y"
    if lookback_days <= 1825:
        return "5y"
    if lookback_days <= 3650:
        return "10y"
    return "max"


def _fetch_bars_yfinance(
    ticker: str, *, interval: str, lookback_days: int,
) -> Optional[List[Dict[str, Any]]]:
    try:
        import yfinance as yf
        period = _yf_period_for(lookback_days)
        df = yf.download(
            ticker, period=period, interval=interval,
            progress=False, auto_adjust=False,
        )
        if df is None or len(df) == 0:
            return None
        if hasattr(df.columns, "get_level_values"):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass
        bars: List[Dict[str, Any]] = []
        for ts, row in df.iterrows():
            try:
                t = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                bars.append({
                    "t": t,
                    "open": float(row.get("Open")),
                    "high": float(row.get("High")),
                    "low": float(row.get("Low")),
                    "close": float(row.get("Close")),
                    "volume": float(row.get("Volume") or 0),
                })
            except Exception:
                continue
        return bars or None
    except Exception as exc:
        logger.debug("yfinance bars fetch failed for %s: %s", ticker, exc)
        return None


# ── public API ───────────────────────────────────────────────────────


def fetch_bars(ticker: str, *, window: str = "today",
                  interval: Optional[str] = None,
                  lookback_days: Optional[int] = None,
                  prefer: str = "thetadata") -> Dict[str, Any]:
    """Pull bars via ThetaData→yfinance fallback.

    Returns ``{"bars": [...], "source": "thetadata"|"yfinance",
    "interval": str, "window": str}``. ``bars`` is empty on total
    failure (both providers returned nothing).
    """
    ticker = (ticker or "").upper().strip()
    iv, default_lookback = _resolve_window(window, interval)
    look = int(lookback_days if lookback_days is not None else default_lookback)
    result: Dict[str, Any] = {
        "bars": [],
        "source": "none",
        "interval": iv,
        "window": (window or "today").lower(),
    }
    if not ticker:
        return result

    providers = []
    if prefer == "yfinance":
        providers = ["yfinance", "thetadata"]
    else:
        providers = ["thetadata", "yfinance"]
    for name in providers:
        if name == "thetadata":
            bars = _fetch_bars_thetadata(
                ticker, interval=iv, lookback_days=look,
            )
        else:
            bars = _fetch_bars_yfinance(
                ticker, interval=iv, lookback_days=look,
            )
        if bars:
            result["bars"] = bars
            result["source"] = name
            # MITS Phase 8.2 — capture raw bars into the bronze lake
            # (gated by TUNABLES.lake_bronze_enabled). Fire-and-forget;
            # never blocks the fetch path.
            try:
                from backend.bot.data import lake as _lake
                _lake.write_bronze(
                    name, "bars", bars, ticker=ticker,
                    extra_tags={"interval": iv, "window": result["window"]},
                    request_url=f"{name}://bars/{ticker}",
                    source_version=__name__,
                )
            except Exception:
                pass
            return result
    return result


def bars_to_dataframe(bars: List[Dict[str, Any]]):
    """Convert the unified bar list into the DataFrame shape detectors
    expect (DatetimeIndex + lowercase OHLCV columns).
    """
    if not bars:
        return None
    try:
        import pandas as pd
    except Exception:
        return None
    rows = []
    idx = []
    for b in bars:
        try:
            ts = datetime.fromisoformat(str(b.get("t")))
        except Exception:
            continue
        rows.append({
            "open": float(b.get("open") or 0.0),
            "high": float(b.get("high") or 0.0),
            "low": float(b.get("low") or 0.0),
            "close": float(b.get("close") or 0.0),
            "volume": float(b.get("volume") or 0.0),
        })
        idx.append(ts)
    if not rows:
        return None
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    return df


def fetch_bars_df(ticker: str, *, window: str = "today",
                     interval: Optional[str] = None,
                     lookback_days: Optional[int] = None,
                     prefer: str = "thetadata") -> Tuple[Any, str]:
    """Convenience wrapper that returns ``(df, source)`` — handy for
    callers that work in pandas (detectors)."""
    payload = fetch_bars(ticker, window=window, interval=interval,
                            lookback_days=lookback_days, prefer=prefer)
    df = bars_to_dataframe(payload["bars"])
    return df, payload.get("source", "none")


__all__ = [
    "fetch_bars",
    "fetch_bars_df",
    "bars_to_dataframe",
]
