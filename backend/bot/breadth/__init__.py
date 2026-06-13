"""Stage-18a — Market Breadth Engine.

"SPY +1% on 8 stocks pulling it is fragile. On 400 stocks, healthy."
That's what breadth tells you. The regime classifier currently has no
breadth signal — this module fixes that.

Daily snapshot of:
  • % of universe above 20-day MA
  • % above 50-day MA
  • % above 200-day MA
  • advancers vs decliners (today's close vs yesterday)
  • new 52-week highs / lows
  • cumulative advance/decline line
  • McClellan Oscillator (19-EMA - 39-EMA of net A/D)

The "universe" is a hard-coded large-cap roster (100 tickers — enough
for meaningful breadth, light enough to fetch in one yfinance batch).
Easy to widen later. Lives in ``UNIVERSE`` below.

Pure persistence + helpers. Compute is done by ``refresh()`` and cached
in ``breadth_snapshots`` so downstream callers read locally.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.breadth_snapshot import BreadthSnapshot

logger = logging.getLogger(__name__)


# 100 large-cap US tickers spanning every major sector. Enough for
# meaningful breadth stats; light enough to fetch in one yfinance call.
UNIVERSE: Tuple[str, ...] = (
    # Mega cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Semis
    "AMD", "QCOM", "INTC", "TXN", "MU", "AMAT", "LRCX", "KLAC", "ADI", "MRVL",
    # Cloud + software
    "ADBE", "NOW", "INTU", "PANW", "CRWD", "SNOW", "PLTR", "WDAY", "TEAM", "DDOG",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SPGI", "AXP", "V",
    # Mega caps non-tech
    "BRK-B", "WMT", "PG", "HD", "KO", "PEP", "COST", "MCD", "DIS", "NKE",
    # Healthcare + pharma
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT", "DHR", "BMY",
    # Energy + materials
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "PSX", "VLO", "MPC", "FCX",
    # Industrials
    "GE", "CAT", "BA", "HON", "UPS", "DE", "RTX", "LMT", "UNP", "MMM",
    # Consumer + retail
    "MA", "TGT", "LOW", "SBUX", "BKNG", "ABNB", "UBER", "LYFT", "PINS", "SNAP",
    # Crypto-adjacent / EV / fintech
    "COIN", "MSTR", "RIOT", "MARA", "RIVN", "LCID", "F", "GM", "SQ", "PYPL",
)


# ── data loader (yfinance) ──────────────────────────────────────────────


@dataclass
class TickerHistory:
    ticker: str
    closes: List[float]               # newest-last
    dates: List[datetime]             # newest-last


def _default_history_fetcher(tickers: Tuple[str, ...],
                                *, period: str = "1y",
                                ) -> Dict[str, TickerHistory]:
    """Pull last-1y daily closes for the whole universe in one yfinance
    call. Returns a dict keyed by ticker; missing tickers are simply
    absent (we tolerate partial fetches)."""
    out: Dict[str, TickerHistory] = {}
    try:
        import pandas as pd
        import yfinance as yf
        df = yf.download(
            list(tickers), period=period, interval="1d",
            progress=False, group_by="ticker", auto_adjust=True,
            threads=True,
        )
    except Exception:
        logger.warning("breadth yfinance batch failed", exc_info=True)
        return out
    if df is None or len(df) == 0:
        return out
    for tk in tickers:
        try:
            sub = df[tk] if tk in df.columns.levels[0] else None
            if sub is None or "Close" not in sub.columns:
                continue
            closes_series = sub["Close"].dropna()
            if closes_series.empty:
                continue
            out[tk] = TickerHistory(
                ticker=tk,
                closes=[float(x) for x in closes_series.tolist()],
                dates=[d.to_pydatetime() if hasattr(d, "to_pydatetime") else d
                          for d in closes_series.index.tolist()],
            )
        except Exception:
            logger.debug("breadth fetch failed for %s", tk, exc_info=True)
    return out


# ── breadth math ────────────────────────────────────────────────────────


def _sma(values: List[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _ema(values: List[float], window: int) -> List[float]:
    """Exponential moving average. Returns one value per input."""
    if not values:
        return []
    alpha = 2.0 / (window + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


@dataclass
class BreadthStats:
    date: datetime
    pct_above_20dma: Optional[float]
    pct_above_50dma: Optional[float]
    pct_above_200dma: Optional[float]
    advancers: int
    decliners: int
    new_highs: int
    new_lows: int
    ad_line: Optional[float]
    mcclellan: Optional[float]
    sample_size: int


def compute_breadth(history: Dict[str, TickerHistory],
                       *, prev_ad_line: float = 0.0,
                       prev_ad_series: Optional[List[float]] = None,
                       ) -> Optional[BreadthStats]:
    """Compute one day's breadth stats from per-ticker history.

    ``prev_ad_line`` is the previous day's cumulative A/D total so we can
    extend the cumulative series. ``prev_ad_series`` is the recent daily
    net-A/D values (newest-last) used for the McClellan EMAs — pass the
    most recent ~60 days from the cache to get a real McClellan.
    """
    if not history:
        return None
    above_20 = above_50 = above_200 = 0
    sample_20 = sample_50 = sample_200 = 0
    advancers = decliners = 0
    new_highs = new_lows = 0
    latest_date: Optional[datetime] = None

    for h in history.values():
        if not h.closes or len(h.closes) < 2:
            continue
        latest = h.closes[-1]
        prior = h.closes[-2]
        if latest_date is None or (h.dates and h.dates[-1] > latest_date):
            latest_date = h.dates[-1]
        # MAs
        ma20 = _sma(h.closes, 20)
        if ma20 is not None:
            sample_20 += 1
            if latest > ma20:
                above_20 += 1
        ma50 = _sma(h.closes, 50)
        if ma50 is not None:
            sample_50 += 1
            if latest > ma50:
                above_50 += 1
        ma200 = _sma(h.closes, 200)
        if ma200 is not None:
            sample_200 += 1
            if latest > ma200:
                above_200 += 1
        # A/D
        if latest > prior:
            advancers += 1
        elif latest < prior:
            decliners += 1
        # 52w highs/lows (252 trading days ≈ 1y)
        window = h.closes[-252:] if len(h.closes) >= 252 else h.closes
        if window and latest >= max(window):
            new_highs += 1
        if window and latest <= min(window):
            new_lows += 1

    sample_size = sum(1 for h in history.values() if len(h.closes) >= 2)
    if sample_size == 0:
        return None

    net_ad = advancers - decliners
    ad_line = prev_ad_line + net_ad

    # McClellan = EMA(19, daily net-A/D) − EMA(39, daily net-A/D).
    mcclellan: Optional[float] = None
    if prev_ad_series:
        series = list(prev_ad_series) + [net_ad]
        if len(series) >= 39:
            mcclellan = _ema(series, 19)[-1] - _ema(series, 39)[-1]

    return BreadthStats(
        date=latest_date or datetime.utcnow(),
        pct_above_20dma=round(above_20 / sample_20, 4) if sample_20 else None,
        pct_above_50dma=round(above_50 / sample_50, 4) if sample_50 else None,
        pct_above_200dma=round(above_200 / sample_200, 4) if sample_200 else None,
        advancers=advancers, decliners=decliners,
        new_highs=new_highs, new_lows=new_lows,
        ad_line=round(ad_line, 2) if ad_line is not None else None,
        mcclellan=round(mcclellan, 2) if mcclellan is not None else None,
        sample_size=sample_size,
    )


# ── refresh + cache ─────────────────────────────────────────────────────


def _recent_ad_series(universe: str = "sp500", *, limit: int = 60) -> Tuple[float, List[float]]:
    """Last cumulative A/D total + the recent net-A/D series (for the
    McClellan calculation). Returns (last_ad_line, [net_ad...newest-last])."""
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(BreadthSnapshot)
                .where(BreadthSnapshot.universe == universe)
                .order_by(desc(BreadthSnapshot.date))
                .limit(limit)
            ).scalars().all())
            if not rows:
                return (0.0, [])
            last = rows[0]
            # Reconstruct daily net_ad (advancers - decliners) by walking
            # newest-to-oldest, returning oldest-first.
            net_series = [r.advancers - r.decliners for r in rows[::-1]]
            return (float(last.ad_line or 0.0), net_series)
    except Exception:
        return (0.0, [])


def refresh(*, universe: str = "sp500",
               history_fetcher: Optional[Callable[..., Dict[str, TickerHistory]]] = None,
               ) -> Dict[str, Any]:
    """Pull the universe's price history, compute today's breadth, persist."""
    fetcher = history_fetcher or _default_history_fetcher
    histories = fetcher(UNIVERSE)
    if not histories:
        return {"snapshots_written": 0,
                "reason": "yfinance fetch returned no data"}
    prev_ad, prev_series = _recent_ad_series(universe=universe)
    stats = compute_breadth(histories,
                                prev_ad_line=prev_ad, prev_ad_series=prev_series)
    if stats is None:
        return {"snapshots_written": 0,
                "reason": "no histories with sufficient data"}
    snap_date = stats.date
    try:
        with session_scope() as session:
            # Skip if today's row already exists.
            existing = session.execute(
                select(BreadthSnapshot)
                .where(BreadthSnapshot.universe == universe)
                .where(BreadthSnapshot.date == snap_date)
            ).scalar_one_or_none()
            if existing is not None:
                return {"snapshots_written": 0,
                        "reason": f"row for {snap_date.isoformat()} already exists"}
            session.add(BreadthSnapshot(
                date=snap_date, universe=universe,
                pct_above_20dma=stats.pct_above_20dma,
                pct_above_50dma=stats.pct_above_50dma,
                pct_above_200dma=stats.pct_above_200dma,
                advancers=stats.advancers, decliners=stats.decliners,
                new_highs=stats.new_highs, new_lows=stats.new_lows,
                ad_line=stats.ad_line, mcclellan=stats.mcclellan,
                sample_size=stats.sample_size,
            ))
    except Exception:
        logger.exception("breadth snapshot write failed")
        return {"snapshots_written": 0, "reason": "write failed"}
    # MITS Phase 8.2 — capture breadth snapshot to bronze.
    try:
        from backend.bot.data import lake as _lake
        _lake.write_bronze(
            "breadth", "snapshot",
            [{
                "date": snap_date.isoformat(),
                "universe": universe,
                "pct_above_20dma": stats.pct_above_20dma,
                "pct_above_50dma": stats.pct_above_50dma,
                "pct_above_200dma": stats.pct_above_200dma,
                "advancers": stats.advancers,
                "decliners": stats.decliners,
                "new_highs": stats.new_highs,
                "new_lows": stats.new_lows,
                "ad_line": stats.ad_line,
                "mcclellan": stats.mcclellan,
                "sample_size": stats.sample_size,
            }],
            extra_tags={"universe": universe},
            request_url="breadth://snapshot",
            source_version=__name__,
        )
    except Exception:
        pass
    return {"snapshots_written": 1, "date": snap_date.isoformat(),
            "sample_size": stats.sample_size}


# ── helpers ─────────────────────────────────────────────────────────────


def latest(universe: str = "sp500") -> Optional[BreadthSnapshot]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(BreadthSnapshot)
                .where(BreadthSnapshot.universe == universe)
                .order_by(desc(BreadthSnapshot.date))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            session.expunge(row)
            return row
    except Exception:
        return None


def history(universe: str = "sp500", *, limit: int = 60) -> List[Dict[str, Any]]:
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(BreadthSnapshot)
                .where(BreadthSnapshot.universe == universe)
                .order_by(desc(BreadthSnapshot.date))
                .limit(limit)
            ).scalars().all())
            return [r.to_dict() for r in rows]
    except Exception:
        return []


def regime_health() -> Dict[str, Any]:
    """One-line interpretation of the latest breadth: are the
    participation numbers healthy or fragile?"""
    l = latest()
    if l is None:
        return {"verdict": "unknown",
                "reason": "no breadth snapshots yet — refresh() to build one"}
    p20 = l.pct_above_20dma or 0.0
    p50 = l.pct_above_50dma or 0.0
    p200 = l.pct_above_200dma or 0.0
    net = l.advancers - l.decliners

    # Stan Druckenmiller's heuristic: "How many stocks participated?"
    if p50 > 0.65 and p200 > 0.55:
        verdict = "healthy_advance"
    elif p50 < 0.35 and p200 < 0.40:
        verdict = "broken"
    elif p20 > 0.70 and p50 < 0.45:
        verdict = "narrow_rally_fragile"
    elif p20 < 0.30 and p50 > 0.55:
        verdict = "pullback_in_bull"
    else:
        verdict = "mixed"

    return {
        "verdict": verdict,
        "pct_above_20dma": p20, "pct_above_50dma": p50,
        "pct_above_200dma": p200,
        "net_ad": net, "new_highs": l.new_highs, "new_lows": l.new_lows,
        "mcclellan": l.mcclellan,
        "as_of": l.date.isoformat() if l.date else None,
    }
