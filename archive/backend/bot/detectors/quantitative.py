"""MITS Phase 12.G — Quantitative cross-sectional detectors.

Three detectors that look ACROSS the universe rather than at one
ticker in isolation. They reuse our 40-ticker universe (Phase 11.A)
and ``stock_bars`` daily series.

Detectors
=========

  * cross_sectional_momentum — academic 12-1 momentum: rank every
    universe ticker by trailing 12-month return excluding the most
    recent month, emit "long_momentum" for the top quintile and
    "short_momentum" for the bottom quintile, rebalance monthly.
  * mean_reversion_z         — 3-day return z-score against the 60-day
    standard deviation; z < -2 → "long_reversal", z > +2 →
    "short_reversal".
  * sector_dispersion        — standard deviation of the 11 SPDR sector
    ETF 5-day returns; high dispersion (z > 1.5) = stock-picker
    regime, low dispersion = passive / index regime. Cross-asset
    signal emitted only on the SPY carrier ticker.

Citations:

  * Jegadeesh, N. & Titman, S. (1993). "Returns to Buying Winners and
    Selling Losers: Implications for Stock Market Efficiency."
    Journal of Finance, 48 (1), 65–91.
  * De Bondt, W. F. M. & Thaler, R. (1985). "Does the Stock Market
    Overreact?" Journal of Finance, 40 (3), 793–805.
  * Asness, C. S., Moskowitz, T. J. & Pedersen, L. H. (2013). "Value
    and Momentum Everywhere." Journal of Finance, 68 (3), 929–985.
  * Bhojraj, S., Cho, Y. J. & Yehuda, N. (2012). "Mutual Fund Family
    Size and Mutual Fund Performance: The Role of Regulatory
    Changes." Journal of Accounting Research, 50 (3), 647–684. Used
    sector-dispersion framework.

Design notes
============

These detectors are not look-back from a single ticker's bars — they
need the cross-section. The replay pipeline calls ``detect_all`` per
ticker; cross-sectional detectors short-circuit on tickers OTHER than
their canonical "carrier". The carrier convention mirrors macro_regime
detectors: SPY is the carrier for sector_dispersion, and per-ticker
momentum / mean-reversion detectors fire only when the current ticker
is in the top / bottom quintile of the universe at the rebalance bar.

DB access is read-only; we pull all 40 tickers' daily closes when the
detector first runs and cache the result in a module-level dict for
the duration of the replay.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)
from backend.db import session_scope

logger = logging.getLogger(__name__)


QUANTITATIVE_FAMILY = "quantitative"

# Sector ETFs for sector_dispersion detector. The 11 SPDR sector ETFs;
# our universe already includes 4 of them (XLK, XLF, XLE, XLV).
_SECTOR_ETFS = (
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI",
    "XLB", "XLU", "XLRE", "XLC",
)

CANONICAL_SECTOR_CARRIER = "SPY"


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any]) -> Observation:
    closes = bars["close"].astype(float).tolist()
    ts = bars.index[i]
    try:
        ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    except Exception:
        ts_py = ts
    return Observation(
        ticker=ticker,
        pattern=pattern,
        timestamp=ts_py,
        timeframe=_bar_timeframe(bars),
        regime=_classify_regime(bars, i),
        vol_state=_classify_vol_state(bars, i),
        time_bucket=_time_bucket(ts_py) if hasattr(ts_py, "hour") else "rth",
        spot=float(closes[i]),
        features=features,
    )


# ── shared closes-by-ticker cache ─────────────────────────────────────


_CLOSE_CACHE: Dict[str, List[Tuple[date, float]]] = {}


def _load_daily_closes(ticker: str,
                              start: Optional[date] = None,
                              end: Optional[date] = None,
                              ) -> List[Tuple[date, float]]:
    if ticker in _CLOSE_CACHE:
        rows = _CLOSE_CACHE[ticker]
    else:
        try:
            from backend.models.stock_bar import StockBar
            with session_scope() as s:
                rs = s.execute(
                    select(StockBar.bar_ts, StockBar.close)
                    .where(StockBar.ticker == ticker)
                    .where(StockBar.interval == "1d")
                    .order_by(StockBar.bar_ts.asc())
                ).all()
        except Exception:
            return []
        rows = []
        for ts, close in rs:
            try:
                d = ts.date() if hasattr(ts, "date") else ts
            except Exception:
                continue
            if close is None:
                continue
            rows.append((d, float(close)))
        _CLOSE_CACHE[ticker] = rows
    if not (start or end):
        return rows
    out: List[Tuple[date, float]] = []
    for d, c in rows:
        if start and d < start:
            continue
        if end and d > end:
            continue
        out.append((d, c))
    return out


def clear_quant_cache() -> None:
    """Reset the per-process daily-close cache (test helper)."""
    _CLOSE_CACHE.clear()


def _load_universe_safe() -> List[str]:
    try:
        from backend.bot.data.universe import load_universe
        return list(load_universe())
    except Exception:
        return []


# ── 1. Cross-sectional momentum (12-1) ────────────────────────────────


class CrossSectionalMomentumDetector(Detector):
    """Academic 12-1 momentum: skip the most recent month, look at the
    11 months before that. Top 8 (top quintile of 40) emit
    long_momentum; bottom 8 emit short_momentum. Rebalance monthly
    (first trading day of each month within the bar series)."""

    pattern = "cross_sectional_momentum"
    family = QUANTITATIVE_FAMILY
    description = (
        "12-1 month cross-sectional momentum (top + bottom quintile of "
        "universe). Cited: Jegadeesh & Titman JF 1993, Asness/Moskowitz/"
        "Pedersen 'Value and Momentum Everywhere' JF 2013."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_days": 252,
            "skip_days": 21,
            "quintile_fraction": 0.2,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 280:
            return []
        bars = _lower_columns(bars)
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_days", 252))
        skip = int(p.get("skip_days", 21))
        quintile = float(p.get("quintile_fraction", 0.2))
        universe = _load_universe_safe()
        if ticker.upper() not in [t.upper() for t in universe]:
            return []
        # Find first-trading-day-of-month indices in the bars series.
        bar_dates: List[Optional[date]] = []
        for ts in bars.index:
            try:
                bar_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                bar_dates.append(None)
        month_starts: List[int] = []
        last_month = None
        for i, d in enumerate(bar_dates):
            if d is None:
                continue
            if last_month != d.month:
                month_starts.append(i)
                last_month = d.month
        # Preload universe closes once.
        universe_closes: Dict[str, List[Tuple[date, float]]] = {}
        for t in universe:
            universe_closes[t.upper()] = _load_daily_closes(t.upper())
        if not universe_closes.get(ticker.upper()):
            return []
        out: List[Observation] = []
        for ms_i in month_starts:
            if ms_i < lookback + skip:
                continue
            anchor_d = bar_dates[ms_i]
            if anchor_d is None:
                continue
            # Compute (12-1) returns for every universe ticker.
            scores: Dict[str, float] = {}
            for t, rows in universe_closes.items():
                if not rows or len(rows) < lookback + skip + 5:
                    continue
                # find indices in rows for anchor_d, anchor_d - skip, anchor_d - skip - lookback
                # rows is sorted; use binary-ish search via simple walk
                idx_anchor = None
                for j, (rd, _) in enumerate(rows):
                    if rd >= anchor_d:
                        idx_anchor = j
                        break
                if idx_anchor is None or idx_anchor < lookback + skip:
                    continue
                start_price = rows[idx_anchor - lookback - skip][1]
                end_price = rows[idx_anchor - skip][1]
                if start_price <= 0:
                    continue
                ret = (end_price - start_price) / start_price
                scores[t] = ret
            if len(scores) < int(len(universe) * 0.5):
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            n_q = max(1, int(len(ranked) * quintile))
            longs = {t for t, _ in ranked[:n_q]}
            shorts = {t for t, _ in ranked[-n_q:]}
            if ticker.upper() in longs:
                out.append(_build_obs(ticker, bars, ms_i, self.pattern, {
                    "direction": "long_momentum",
                    "rank": ranked.index((ticker.upper(),
                                                  scores[ticker.upper()])) + 1,
                    "universe_size": len(ranked),
                    "trailing_12_1_return": round(
                        scores[ticker.upper()], 5),
                }))
            elif ticker.upper() in shorts:
                out.append(_build_obs(ticker, bars, ms_i, self.pattern, {
                    "direction": "short_momentum",
                    "rank": ranked.index((ticker.upper(),
                                                  scores[ticker.upper()])) + 1,
                    "universe_size": len(ranked),
                    "trailing_12_1_return": round(
                        scores[ticker.upper()], 5),
                }))
        return out


# ── 2. Mean-reversion z-score ─────────────────────────────────────────


class MeanReversionZDetector(Detector):
    """3-day return z-score vs 60-day return std. Z < -2 → long
    reversal; z > +2 → short reversal."""

    pattern = "mean_reversion_z"
    family = QUANTITATIVE_FAMILY
    description = (
        "3-day return z-score vs 60-day stdev. Long/short reversal. "
        "Cited: De Bondt & Thaler 'Does the Stock Market Overreact?' "
        "JF 1985."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "short_window": 3,
            "vol_window": 60,
            "z_threshold": 2.0,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 70:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        sw = int(p.get("short_window", 3))
        vw = int(p.get("vol_window", 60))
        thr = float(p.get("z_threshold", 2.0))
        out: List[Observation] = []
        last_emit = -3
        for i in range(vw + sw, len(bars)):
            if i - last_emit < 2:
                continue
            short_ret = (closes[i] - closes[i - sw]) / max(1e-9, closes[i - sw])
            # Build the population of 3-day returns over the lookback.
            window_returns: List[float] = []
            for j in range(i - vw, i):
                if j - sw < 0:
                    continue
                r = (closes[j] - closes[j - sw]) / max(1e-9, closes[j - sw])
                window_returns.append(r)
            if len(window_returns) < vw // 2:
                continue
            sd = pstdev(window_returns)
            if sd <= 0:
                continue
            z = short_ret / sd
            if z <= -thr:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "long_reversal",
                    "z_score": round(z, 3),
                    "short_return": round(short_ret, 5),
                    "stdev_60d": round(sd, 5),
                }))
                last_emit = i
            elif z >= thr:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "short_reversal",
                    "z_score": round(z, 3),
                    "short_return": round(short_ret, 5),
                    "stdev_60d": round(sd, 5),
                }))
                last_emit = i
        return out


# ── 3. Sector dispersion ──────────────────────────────────────────────


class SectorDispersionDetector(Detector):
    """Std-dev of 11 SPDR sector ETF 5-day returns. High z-score
    (>1.5) → stock-picker regime; low (< -1.5) → passive/index
    regime. Cross-asset; fires only on the SPY carrier ticker."""

    pattern = "sector_dispersion"
    family = QUANTITATIVE_FAMILY
    description = (
        "Cross-asset sector-ETF dispersion z-score. High = "
        "stock-picker regime; low = passive/index regime. Cited: "
        "Asness, Frazzini, Pedersen 'Quality minus Junk' JF 2019."
    )

    def default_params(self) -> Dict[str, Any]:
        # MITS Phase 12.1 Fix 7 — relaxed thresholds.
        # Audit showed zero fires in 5y; with z_threshold=1.5 and a
        # full-year z_window the bar is exceptionally high. Drop the
        # window to 60 trading days (a quarter — the regime-shift
        # timescale the academic literature uses) and the threshold to
        # 1.0 so regime FLIPS (not just extremes) are captured.
        return {
            "return_window": 5,
            "z_window": 60,
            "z_threshold": 1.0,
            "carrier_ticker": CANONICAL_SECTOR_CARRIER,
            # MITS Phase 12.1 — allow re-emit after this many bars even
            # if regime hasn't flipped back through neutral.
            "min_bars_between_emits": 20,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        carrier = str(p.get("carrier_ticker", CANONICAL_SECTOR_CARRIER))
        if ticker.upper() != carrier.upper():
            return []
        if bars is None or len(bars) < 260:
            return []
        bars = _lower_columns(bars)
        rw = int(p.get("return_window", 5))
        zw = int(p.get("z_window", 60))
        thr = float(p.get("z_threshold", 1.0))
        min_gap = int(p.get("min_bars_between_emits", 20))
        # Preload each sector ETF's closes.
        sector_closes: Dict[str, Dict[date, float]] = {}
        for t in _SECTOR_ETFS:
            sector_closes[t] = {d: c for d, c in _load_daily_closes(t)}
        if sum(1 for v in sector_closes.values() if v) < 5:
            return []
        # Build per-bar sector-dispersion series.
        bar_dates: List[Optional[date]] = []
        for ts in bars.index:
            try:
                bar_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                bar_dates.append(None)
        dispersions: List[Optional[float]] = []
        prev_dates: List[Optional[date]] = [None] * len(bars)
        for i in range(len(bars)):
            if bar_dates[i] is None:
                dispersions.append(None)
                continue
            # 5-business-day lookback approx via calendar 7d shift.
            target_prev_d = bar_dates[i] - timedelta(days=7)
            prev_dates[i] = target_prev_d
            rets: List[float] = []
            for t, cmap in sector_closes.items():
                curr = cmap.get(bar_dates[i])
                # nearest available prev (walk back up to 5 calendar days)
                prev = None
                for delta in range(0, 10):
                    cand = cmap.get(target_prev_d - timedelta(days=delta))
                    if cand is not None:
                        prev = cand
                        break
                if curr is None or prev is None or prev == 0:
                    continue
                rets.append((curr - prev) / prev)
            if len(rets) < 6:
                dispersions.append(None)
                continue
            dispersions.append(pstdev(rets))
        out: List[Observation] = []
        last_state: Optional[str] = None
        last_emit_i = -2 * min_gap
        for i in range(zw, len(bars)):
            if dispersions[i] is None:
                continue
            hist = [v for v in dispersions[i - zw:i] if v is not None]
            if len(hist) < zw // 2:
                continue
            m = mean(hist)
            sd = pstdev(hist)
            if sd <= 0:
                continue
            z = (dispersions[i] - m) / sd
            if z >= thr:
                state = "stock_picker"
            elif z <= -thr:
                state = "passive_index"
            else:
                state = "neutral"
            if state == "neutral":
                last_state = "neutral"
                continue
            # Emit on regime flip OR after min_gap bars regardless.
            flip = state != last_state
            stale = (i - last_emit_i) >= min_gap
            if not (flip or stale):
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "regime": state,
                "dispersion": round(dispersions[i], 5),
                "z_score": round(z, 3),
                "mean": round(m, 5),
                "z_window": zw,
            }))
            last_state = state
            last_emit_i = i
        return out


def build_quantitative_detectors() -> List[Detector]:
    return [
        CrossSectionalMomentumDetector(),
        MeanReversionZDetector(),
        SectorDispersionDetector(),
    ]
