"""MITS Phase 0 — liquidity-grab detectors.

  LiquiditySweep — bar takes out (wicks beyond) a recent swing high/low
                   then closes BACK inside the range. Classic
                   stop-running by larger players.
  StopHunt — similar but requires a sharp reversal candle (close in the
             opposite third of the range from the wick).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any], closes) -> Observation:
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


class LiquiditySweepDetector(Detector):
    """Bar's HIGH (LOW) exceeds the prior 10-bar high (low) but CLOSE
    is back inside the prior range. Direction stored as feature."""

    pattern = "liquidity_sweep"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 10,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 12:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 10))
        out: List[Observation] = []
        for i in range(lookback, len(bars)):
            prior_high = max(highs[i - lookback:i])
            prior_low = min(lows[i - lookback:i])
            if highs[i] > prior_high and closes[i] < prior_high:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "above",
                    "swept_level": round(prior_high, 4),
                    "wick_overshoot": round(highs[i] - prior_high, 4),
                    "close_back_in": True,
                }, closes))
            elif lows[i] < prior_low and closes[i] > prior_low:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "below",
                    "swept_level": round(prior_low, 4),
                    "wick_overshoot": round(prior_low - lows[i], 4),
                    "close_back_in": True,
                }, closes))
        return out


class StopHuntDetector(Detector):
    """Sweep + sharp reversal candle. Same precondition as
    LiquiditySweepDetector PLUS the close is in the opposite third of
    the bar's range (deep wick, decisive reversal)."""

    pattern = "stop_hunt"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 10,
            "reversal_third": 0.33,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 12:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 10))
        rev_third = float(p.get("reversal_third", 0.33))
        out: List[Observation] = []
        for i in range(lookback, len(bars)):
            prior_high = max(highs[i - lookback:i])
            prior_low = min(lows[i - lookback:i])
            bar_range = highs[i] - lows[i]
            if bar_range <= 0:
                continue
            close_pos = (closes[i] - lows[i]) / bar_range
            if highs[i] > prior_high and close_pos < rev_third:
                # Wick above, closed in bottom third → bear stop hunt.
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "bear",
                    "swept_level": round(prior_high, 4),
                    "close_pos_in_range": round(close_pos, 4),
                }, closes))
            elif lows[i] < prior_low and close_pos > (1.0 - rev_third):
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "bull",
                    "swept_level": round(prior_low, 4),
                    "close_pos_in_range": round(close_pos, 4),
                }, closes))
        return out


def build_liquidity_detectors() -> List[Detector]:
    return [LiquiditySweepDetector(), StopHuntDetector()]
