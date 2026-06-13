"""IV regime classifier (P2.3).

Reads the ``iv_history`` time series (populated by P1.3 backfill) and
labels each ticker's current implied-volatility regime:

  • **mean_reverting** — IV oscillates around a stable mean; the most
    common regime for liquid large-caps. Indicates short-vol setups
    have positive expectancy.

  • **trending_up** — IV grinding higher across recent history. Bias
    toward long-vol (long straddles, defensive sizing).

  • **trending_down** — IV grinding lower. Bias toward short-vol
    (premium-selling strategies have tailwinds).

  • **expanding** — variance of IV itself is increasing (vol of vol
    rising). Caution on directional bets; widen stops.

  • **stable_low** — IV in a low-variance plateau. Strategies counting
    on "elevated IV" should down-size.

  • **unknown** — sample size below the gate's confidence floor.

Outputs are deterministic on the iv_history corpus, cached in-process
for 1 hour. Agents read via ``context.iv_regime[ticker]``.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.db import session_scope
from backend.models.iv_history import IVHistory

logger = logging.getLogger(__name__)


MIN_SAMPLES = 30          # below this → tier=unknown (matches our P1.3 floor)
RECENT_WINDOW = 30        # bars considered "recent" for slope/std
LOOKBACK_DAYS = 365       # max history we look at


# Regime decision thresholds — picked from observed AAPL/SPY behavior.
# Override per-ticker later if needed.
_TREND_SLOPE = 0.0008     # |slope| > this (IV/day) → trending
_STD_LOW = 0.04           # rolling std < this → stable_low
_EXPANDING_RATIO = 1.5    # recent_std / trailing_std > this → expanding


@dataclass
class IVRegimeReport:
    ticker: str
    regime: str               # mean_reverting | trending_up | trending_down | expanding | stable_low | unknown
    confidence: float         # 0..1 — how decisive the classifier was
    sample_count: int
    current_iv: Optional[float] = None
    mean_iv: Optional[float] = None
    std_iv: Optional[float] = None
    slope: Optional[float] = None        # rolling regression slope (IV per day)
    autocorr_lag1: Optional[float] = None  # lag-1 autocorrelation — close to 1 ⇒ persistent
    recent_std: Optional[float] = None
    trailing_std: Optional[float] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── feature math ────────────────────────────────────────────────────────


def _linreg_slope(xs: List[float], ys: List[float]) -> float:
    """OLS slope; returns 0 on degenerate inputs."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def _autocorr_lag1(values: List[float]) -> float:
    """Lag-1 autocorrelation in [-1, 1]. Close to 1 → IV is persistent
    (yesterday's value predicts today). Mean-reverting series have low
    or negative autocorrelation."""
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    num = sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n))
    den = sum((v - mean) ** 2 for v in values)
    return num / den if den > 0 else 0.0


# ── classifier ──────────────────────────────────────────────────────────


def _classify(values: List[float], dates: List[date]) -> Tuple[str, float, str]:
    """Return (regime, confidence, note) from the sorted IV time series.

    Dates are ASCENDING (oldest first). Decision logic is deliberately
    simple and auditable.
    """
    n = len(values)
    if n < MIN_SAMPLES:
        return "unknown", 0.0, f"insufficient samples ({n} < {MIN_SAMPLES})"

    # Project dates to ordinals for the regression slope.
    base = dates[0].toordinal()
    xs = [d.toordinal() - base for d in dates]
    slope = _linreg_slope(xs, values)
    overall_std = statistics.stdev(values) if n > 1 else 0.0
    overall_mean = statistics.mean(values)

    # Split into recent/trailing halves for variance-of-variance.
    half = n // 2
    trailing = values[:half]
    recent = values[half:]
    trailing_std = statistics.stdev(trailing) if len(trailing) > 1 else 0.0
    recent_std = statistics.stdev(recent) if len(recent) > 1 else 0.0
    expanding_ratio = (recent_std / trailing_std) if trailing_std > 1e-9 else 0.0

    autocorr = _autocorr_lag1(values)

    # Priority of checks — first match wins.
    if expanding_ratio >= _EXPANDING_RATIO:
        confidence = min(1.0, (expanding_ratio - 1.0) / 2.0)
        return "expanding", round(confidence, 3), (
            f"recent IV variance {recent_std:.4f} is "
            f"{expanding_ratio:.2f}× the trailing-half variance"
        )
    if slope >= _TREND_SLOPE:
        confidence = min(1.0, abs(slope) / (_TREND_SLOPE * 4))
        return "trending_up", round(confidence, 3), (
            f"slope {slope:.5f} IV/day over {n} samples ⇒ uptrend"
        )
    if slope <= -_TREND_SLOPE:
        confidence = min(1.0, abs(slope) / (_TREND_SLOPE * 4))
        return "trending_down", round(confidence, 3), (
            f"slope {slope:.5f} IV/day over {n} samples ⇒ downtrend"
        )
    if overall_std < _STD_LOW and abs(autocorr) > 0.5:
        confidence = min(1.0, abs(autocorr))
        return "stable_low", round(confidence, 3), (
            f"std {overall_std:.4f} below {_STD_LOW}, persistent (autocorr {autocorr:.2f})"
        )
    # Default: mean-reverting. Confidence is highest when autocorr is
    # LOW (oscillation) and overall_std is moderate.
    revert_score = max(0.0, 1.0 - max(0.0, autocorr))
    confidence = min(1.0, revert_score)
    return "mean_reverting", round(confidence, 3), (
        f"autocorr {autocorr:.2f}, std {overall_std:.4f} — oscillating around {overall_mean:.4f}"
    )


# ── public API ──────────────────────────────────────────────────────────


_CACHE: Dict[str, Tuple[float, IVRegimeReport]] = {}
_CACHE_TTL = 3600.0       # 1 hour — IV regime doesn't change intraday


def classify_ticker(ticker: str, *, force: bool = False) -> IVRegimeReport:
    """Public entry point. Returns a fresh classification or the cached
    one (within ``_CACHE_TTL``). Set ``force=True`` to bypass cache."""
    key = ticker.upper()
    now = time.monotonic()
    if not force:
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]

    cutoff = datetime.combine(date.today() - timedelta(days=LOOKBACK_DAYS),
                                  datetime.min.time())
    try:
        with session_scope() as s:
            rows = s.execute(
                select(IVHistory.date, IVHistory.iv_atm)
                .where(IVHistory.ticker == key)
                .where(IVHistory.iv_atm.is_not(None))
                .where(IVHistory.date >= cutoff)
                .order_by(IVHistory.date.asc())
            ).all()
    except Exception:
        logger.warning("iv_regime query failed for %s", key, exc_info=True)
        rows = []

    dates: List[date] = []
    values: List[float] = []
    for d, v in rows:
        if v is None or v <= 0:
            continue
        d_only = d.date() if hasattr(d, "date") else d
        dates.append(d_only)
        values.append(float(v))

    if not values:
        report = IVRegimeReport(
            ticker=key, regime="unknown", confidence=0.0,
            sample_count=0, note="no iv_history rows found",
        )
        _CACHE[key] = (now, report)
        return report

    regime, conf, note = _classify(values, dates)

    half = len(values) // 2
    trailing = values[:half]
    recent = values[half:]
    report = IVRegimeReport(
        ticker=key,
        regime=regime,
        confidence=conf,
        sample_count=len(values),
        current_iv=round(values[-1], 4),
        mean_iv=round(statistics.mean(values), 4),
        std_iv=round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
        slope=round(_linreg_slope(
            [d.toordinal() - dates[0].toordinal() for d in dates], values,
        ), 6),
        autocorr_lag1=round(_autocorr_lag1(values), 3),
        recent_std=(round(statistics.stdev(recent), 4)
                       if len(recent) > 1 else 0.0),
        trailing_std=(round(statistics.stdev(trailing), 4)
                         if len(trailing) > 1 else 0.0),
        note=note,
    )
    _CACHE[key] = (now, report)
    return report


def regime_for_universe(tickers: List[str]) -> Dict[str, IVRegimeReport]:
    """Convenience: classify all listed tickers. Returns
    ``{ticker: IVRegimeReport}``."""
    return {t.upper(): classify_ticker(t) for t in tickers}


def reset_cache() -> None:
    """Test helper / manual ops — wipe the in-process cache."""
    _CACHE.clear()
