"""MITS Phase 0 — Detector base classes.

Every Detector is a pure function from `(ticker, bars: pd.DataFrame)` to
a list of `Observation` rows. Detectors:

  * Never look ahead — at bar index i, they only consult bars[0:i+1].
  * Never persist directly — the caller (historical_replay,
    live engine) decides where to write.
  * Are idempotent on identical inputs.
  * Return ALL historical pattern hits in the bar series, not just the
    latest. The replay framework relies on this to bootstrap the corpus.

The DataFrame is expected to have OHLCV columns (lowercase `open`,
`high`, `low`, `close`, `volume`) and a DatetimeIndex. Detectors that
need additional fields (VWAP, IV, etc.) compute or accept them as kwargs.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Observation:
    """A single pattern-detection event.

    Mirrors the columns of `MarketObservation` so persistence is a
    one-liner. `features` is a plain dict — the persistence layer
    JSON-encodes it before insertion.
    """

    ticker: str
    pattern: str
    timestamp: datetime
    timeframe: str = "1d"
    regime: str = "unknown"
    vol_state: str = "normal"
    time_bucket: str = "rth"
    spot: Optional[float] = None
    iv_rank: Optional[float] = None
    gex_state: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)
    source: str = "historical_replay"
    # MITS Phase 12.1 — directional tag.
    # 'long' | 'short' | 'neutral' | None
    direction: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "pattern": self.pattern,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "timeframe": self.timeframe,
            "regime": self.regime,
            "vol_state": self.vol_state,
            "time_bucket": self.time_bucket,
            "spot": self.spot,
            "iv_rank": self.iv_rank,
            "gex_state": self.gex_state,
            "features": dict(self.features) if self.features else {},
            "source": self.source,
            "direction": self.direction,
        }


class Detector(ABC):
    """Abstract base for every pattern detector.

    Subclasses set ``pattern`` (string slug used as the observation
    ``pattern`` column) and implement ``detect``.
    """

    pattern: str = ""
    # ``family`` groups detectors in the operator-facing UI. One of:
    # 'candlesticks' | 'price_action' | 'market_structure' | 'liquidity'
    # | 'vwap' | 'volume_profile' | 'options_intel'. Subclasses override.
    family: str = "uncategorized"
    # ``description`` shows up as the hover tooltip in the operator UI.
    # Plain English — explain what this detector fires on so the
    # operator (a markets beginner) can toggle confidently.
    description: str = ""

    @abstractmethod
    def detect(self, ticker: str, bars, **kwargs) -> List[Observation]:
        """Return every observation in the bar series.

        Implementations MUST tolerate empty / missing input gracefully
        and never raise on routine data issues — log + return [].
        """
        raise NotImplementedError

    def default_params(self) -> Dict[str, Any]:
        """Return the detector's tunable parameters + their defaults.

        Subclasses override when they expose operator-tunable knobs
        (lookback windows, breakout thresholds, vol multipliers, etc.).
        The detector control-plane UI reads this so operators can
        adjust without code changes. Empty dict = no operator knobs.
        """
        return {}


# ── helpers shared across detectors ───────────────────────────────────


def _lower_columns(bars):
    """Coerce OHLCV column names to lowercase. yfinance returns
    capitalised names; we standardise on lower for the rest of the
    pipeline. Returns the same DataFrame (modified in-place is fine —
    callers pass copies for replay)."""
    try:
        if hasattr(bars.columns, "get_level_values"):
            bars.columns = [str(c[0]) if isinstance(c, tuple) else str(c)
                                for c in bars.columns]
        bars.columns = [str(c).strip().lower() for c in bars.columns]
    except Exception:
        pass
    return bars


def _bar_timeframe(bars) -> str:
    """Infer a coarse timeframe label from the DatetimeIndex spacing."""
    try:
        if bars is None or len(bars) < 2:
            return "1d"
        idx = bars.index
        delta = (idx[1] - idx[0]).total_seconds()
        if delta < 60:
            return "1m"
        if delta < 300:
            return "1m"
        if delta < 600:
            return "5m"
        if delta < 1800:
            return "15m"
        if delta < 3600:
            return "30m"
        if delta < 7200:
            return "1h"
        if delta < 86400:
            return "1h"
        return "1d"
    except Exception:
        return "1d"


def _time_bucket(ts: datetime) -> str:
    """Coarse intraday session bucket: pre / open / morning / mid /
    afternoon / close / post / rth. Used as a cohort axis."""
    try:
        hh = ts.hour
        mm = ts.minute
        # All in UTC-ish ET — yfinance daily bars are 00:00; intraday is ET.
        if hh < 9 or (hh == 9 and mm < 30):
            return "pre"
        if hh == 9 and mm < 45:
            return "open"
        if hh < 11:
            return "morning"
        if hh < 13:
            return "mid"
        if hh < 15:
            return "afternoon"
        if hh < 16:
            return "close"
        return "post"
    except Exception:
        return "rth"


def _classify_regime(bars, i: int) -> str:
    """Quick-and-dirty trend regime classifier using a 20-bar SMA.

    Returns one of: trending_up | trending_down | choppy | unknown.
    Lightweight enough to call from every observation without dragging
    the full regime engine into the corpus path.

    MITS Phase 13 Fix 6 — adaptive window. The previous floor of i<20
    emitted ``unknown`` for the entire first 20 bars of every ticker's
    backfill (~2,118 cells / 3.5% of corpus). We now fall back to a
    shorter window when i in [5, 20): the regime label is noisier but
    still semantically correct (uptrend on the first 5 bars of an
    obvious uptrend should be ``trending_up``, not ``unknown``). When
    we have fewer than 5 bars there genuinely isn't enough signal —
    keep ``unknown`` so the aggregator can isolate it.
    """
    try:
        if i < 5:
            return "unknown"
        window_len = min(20, i + 1)
        window = bars["close"].iloc[max(0, i - window_len + 1):i + 1]
        if len(window) < 3:
            return "unknown"
        sma = window.mean()
        last = float(bars["close"].iloc[i])
        first = float(window.iloc[0])
        slope = (last - first) / max(1e-9, abs(first))
        # When window is short, require a stronger slope to claim a
        # trend label (avoid noise) — 0.5% for full 20, 1% for short.
        slope_floor = 0.005 if window_len >= 20 else 0.01
        if last > sma and slope > slope_floor:
            return "trending_up"
        if last < sma and slope < -slope_floor:
            return "trending_down"
        return "choppy"
    except Exception:
        return "unknown"


def _classify_vol_state(bars, i: int) -> str:
    """ATR-percentile vol classifier. low | normal | high."""
    try:
        if i < 30:
            return "normal"
        high = bars["high"].iloc[max(0, i - 30):i + 1]
        low = bars["low"].iloc[max(0, i - 30):i + 1]
        close = bars["close"].iloc[max(0, i - 30):i + 1]
        # Simple TR proxy: high - low.
        tr = (high - low).abs()
        last_tr = float(tr.iloc[-1])
        med = float(tr.median())
        if med <= 0:
            return "normal"
        ratio = last_tr / med
        if ratio < 0.6:
            return "low"
        if ratio > 1.6:
            return "high"
        return "normal"
    except Exception:
        return "normal"
