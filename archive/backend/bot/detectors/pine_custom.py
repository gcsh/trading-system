"""MITS Phase 4 (P4.2) — Pine-imported detector runtime.

When the operator pastes a Pine Script into the /detectors/import-pine
modal, the script is persisted as a ``DetectorConfig`` row with
``source='pine_import'`` plus the raw Pine in ``pine_source``. P4.2
makes those rows actually FIRE during live detection.

What we support (best-effort, NOT a full Pine interpreter):

  * MACD cross (signal-line cross + zero-line cross)
  * RSI threshold cross (over N / under N)
  * Moving-average cross (SMA / EMA fast vs slow)
  * Price cross above/below an indicator (close > sma(close, N))

The rule set the translator extracts (see ``backend.bot.pine_import``)
is reduced to a list of structured rule dicts via
``_rules_from_translation``. The detector evaluates each rule on the
incoming bars and emits one ``Observation`` per recent rule fire — by
default only the most-recent cross is reported (single observation per
detect call), matching the institutional convention that "the cross
event IS the signal".

Adding a new Pine-support pattern:
  1. Add the regex in ``backend.bot.pine_import.translate_pine``.
  2. Extend ``_rules_from_translation`` here to recognise the new
     rule shape.
  3. Add a handler branch in ``_evaluate_rule``.
  4. Update ``can_evaluate_translation`` so the import-pine response
     accurately reports ``will_fire_next_cycle=True``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)
from backend.bot.pine_import import PineImportResult, translate_pine

logger = logging.getLogger(__name__)


PINE_FAMILY_SLUG = "pine_custom"


# ── rule schema ────────────────────────────────────────────────────────


def _rules_from_translation(result: PineImportResult) -> List[Dict[str, Any]]:
    """Convert the translator's natural-language rules back into
    structured dicts the evaluator can execute.

    The translator emits rules like::

        "buy when macd crosses above signal"
        "buy when rsi < 30"
        "sell when rsi > 70"
        "buy when price above ma50"
        "sell when price below ma200"

    We map each to a dict with at least ``kind`` and ``direction``.
    """
    rules: List[Dict[str, Any]] = []
    for raw in (result.rules or []):
        line = raw.lower().strip()
        if not line:
            continue
        if "buy" in line:
            direction = "bull"
        elif "sell" in line:
            direction = "bear"
        else:
            continue

        # MACD signal-line cross.
        if "macd" in line and "signal" in line:
            rules.append({
                "kind": "macd_signal_cross",
                "direction": direction,
            })
            continue
        # MACD zero-line cross.
        if "macd" in line and "zero" in line:
            rules.append({
                "kind": "macd_zero_cross",
                "direction": direction,
            })
            continue
        # RSI thresholds.
        m = re.search(r"rsi\s*[<>]\s*(\d{1,3})", line)
        if m:
            rules.append({
                "kind": "rsi_threshold",
                "direction": direction,
                "threshold": int(m.group(1)),
            })
            continue
        # Price vs MA.
        m = re.search(r"ma(\d{1,3})", line)
        if m and "price" in line:
            rules.append({
                "kind": "price_vs_ma",
                "direction": direction,
                "window": int(m.group(1)),
                "kind_avg": "sma",
            })
            continue
        # MA cross.
        m = re.search(r"ma(\d{1,3})\s*(?:crosses\s+(?:above|below))?\s*ma(\d{1,3})", line)
        if m:
            rules.append({
                "kind": "ma_cross",
                "direction": direction,
                "fast": int(m.group(1)),
                "slow": int(m.group(2)),
                "kind_avg": "sma",
            })
    return rules


def can_evaluate_translation(result: PineImportResult) -> bool:
    """Return ``True`` when at least one rule from the translation maps
    to a handler the evaluator implements. The Pine import endpoint
    surfaces this as ``will_fire_next_cycle``.
    """
    return bool(_rules_from_translation(result))


# ── indicator helpers ──────────────────────────────────────────────────


def _ema(values: List[float], period: int) -> List[float]:
    if period <= 0 or not values:
        return []
    k = 2.0 / (period + 1)
    out: List[float] = []
    ema = float(values[0])
    out.append(ema)
    for v in values[1:]:
        ema = (float(v) - ema) * k + ema
        out.append(ema)
    return out


def _sma(values: List[float], period: int) -> List[float]:
    if period <= 0 or not values:
        return []
    out: List[float] = []
    s = 0.0
    for i, v in enumerate(values):
        s += float(v)
        if i >= period:
            s -= float(values[i - period])
        if i >= period - 1:
            out.append(s / period)
        else:
            out.append(float("nan"))
    return out


def _rsi(closes: List[float], period: int = 14) -> List[float]:
    n = len(closes)
    if n <= period:
        return [float("nan")] * n
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out: List[float] = [float("nan")] * (period + 1)
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return out[:n]


def _macd(closes: List[float],
              fast: int = 12, slow: int = 26, signal: int = 9
              ) -> Tuple[List[float], List[float]]:
    if not closes:
        return [], []
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [(ema_fast[i] - ema_slow[i])
                  for i in range(len(closes))]
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line


# ── evaluator ──────────────────────────────────────────────────────────


def _most_recent_cross(series_a: List[float], series_b: List[float],
                            *, direction: str) -> Optional[int]:
    """Return the index of the most recent cross of series_a relative to
    series_b matching ``direction`` (``'bull'`` = above, ``'bear'`` = below),
    or None if no qualifying cross is found.
    """
    if not series_a or not series_b or len(series_a) != len(series_b):
        return None
    last = None
    for i in range(1, len(series_a)):
        a_prev, b_prev = series_a[i - 1], series_b[i - 1]
        a_now, b_now = series_a[i], series_b[i]
        # Skip NaN.
        if any((v is None or v != v) for v in (a_prev, b_prev, a_now, b_now)):
            continue
        if direction == "bull" and a_prev <= b_prev and a_now > b_now:
            last = i
        elif direction == "bear" and a_prev >= b_prev and a_now < b_now:
            last = i
    return last


def _evaluate_rule(rule: Dict[str, Any], closes: List[float]
                       ) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Run one rule against the closes; return (bar_index, features)
    for the most recent fire, or None if no fire."""
    kind = rule.get("kind")
    direction = rule.get("direction", "bull")
    if kind == "macd_signal_cross":
        macd, signal = _macd(closes)
        i = _most_recent_cross(macd, signal, direction=direction)
        if i is None:
            return None
        return i, {
            "rule_kind": "macd_signal_cross",
            "macd": round(macd[i], 4),
            "signal": round(signal[i], 4),
            "direction": direction,
        }
    if kind == "macd_zero_cross":
        macd, _ = _macd(closes)
        zeros = [0.0] * len(macd)
        i = _most_recent_cross(macd, zeros, direction=direction)
        if i is None:
            return None
        return i, {
            "rule_kind": "macd_zero_cross",
            "macd": round(macd[i], 4),
            "direction": direction,
        }
    if kind == "rsi_threshold":
        threshold = float(rule.get("threshold", 30))
        rsi = _rsi(closes)
        thresholds = [threshold] * len(rsi)
        # "buy when rsi < 30" → bear cross of rsi below threshold flags entry
        # (RSI dropping past 30 from above) — semantically a bullish entry.
        # We mirror: direction='bull' (entry) → look for RSI crossing UNDER threshold
        # direction='bear' (exit) → look for RSI crossing OVER threshold
        cross_dir = "bear" if direction == "bull" else "bull"
        i = _most_recent_cross(rsi, thresholds, direction=cross_dir)
        if i is None:
            return None
        return i, {
            "rule_kind": "rsi_threshold",
            "rsi": round(rsi[i], 2) if rsi[i] == rsi[i] else None,
            "threshold": threshold,
            "direction": direction,
        }
    if kind == "price_vs_ma":
        window = int(rule.get("window", 50))
        avg = (_ema(closes, window) if rule.get("kind_avg") == "ema"
               else _sma(closes, window))
        if not avg:
            return None
        i = _most_recent_cross(closes, avg, direction=direction)
        if i is None:
            return None
        return i, {
            "rule_kind": "price_vs_ma",
            "window": window,
            "ma": round(avg[i], 4),
            "close": round(closes[i], 4),
            "direction": direction,
        }
    if kind == "ma_cross":
        fast = int(rule.get("fast", 50))
        slow = int(rule.get("slow", 200))
        avg_kind = rule.get("kind_avg", "sma")
        f_series = (_ema(closes, fast) if avg_kind == "ema"
                    else _sma(closes, fast))
        s_series = (_ema(closes, slow) if avg_kind == "ema"
                    else _sma(closes, slow))
        i = _most_recent_cross(f_series, s_series, direction=direction)
        if i is None:
            return None
        return i, {
            "rule_kind": "ma_cross",
            "fast": fast,
            "slow": slow,
            "fast_value": round(f_series[i], 4),
            "slow_value": round(s_series[i], 4),
            "direction": direction,
        }
    return None


# ── detector ───────────────────────────────────────────────────────────


class PineCustomDetector(Detector):
    """One instance per persisted Pine-import detector. Stores the rule
    set (decoded from ``pine_source`` at registry-build time) and fires
    when any rule's most-recent cross lands inside the bar window.

    Description carries the recognized rule list so the operator can
    confirm the runtime understood the script the same way the UI did.
    """

    family = PINE_FAMILY_SLUG

    def __init__(self, name: str, pine_source: str) -> None:
        self.pattern = name
        self.pine_source = pine_source or ""
        try:
            self._translation = translate_pine(self.pine_source)
        except Exception:
            self._translation = PineImportResult()
        self._rules = _rules_from_translation(self._translation)
        recognised = ", ".join(self._translation.recognized) or "(no rules)"
        self.description = f"Pine-imported detector: {recognised}."

    def default_params(self) -> Dict[str, Any]:
        return {
            "max_lookback_bars": 200,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        if not self._rules:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        max_look = int(p.get("max_lookback_bars", 200))
        if max_look > 0 and len(closes) > max_look:
            # Evaluate on the tail so old replays don't keep flagging
            # a years-old cross.
            offset = len(closes) - max_look
            closes_window = closes[offset:]
            bars_window = bars.iloc[offset:]
        else:
            offset = 0
            closes_window = closes
            bars_window = bars

        out: List[Observation] = []
        for rule in self._rules:
            res = _evaluate_rule(rule, closes_window)
            if res is None:
                continue
            i_local, features = res
            i_global = i_local + offset
            ts = bars.index[i_global]
            try:
                ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            except Exception:
                ts_py = ts
            features["source_script"] = "pine_import"
            out.append(Observation(
                ticker=ticker,
                pattern=self.pattern,
                timestamp=ts_py,
                timeframe=_bar_timeframe(bars_window),
                regime=_classify_regime(bars_window, i_local),
                vol_state=_classify_vol_state(bars_window, i_local),
                time_bucket=_time_bucket(ts_py) if hasattr(ts_py, "hour") else "rth",
                spot=float(closes[i_global]),
                features=features,
            ))
        return out


def build_pine_custom_detectors() -> List[Detector]:
    """Scan ``DetectorConfig`` rows with source='pine_import' and build
    one ``PineCustomDetector`` per row. Failures (corrupt scripts) are
    skipped with a debug log so they don't wedge the registry."""
    try:
        from sqlalchemy import select as _select
        from backend.db import session_scope
        from backend.models.detector_config import DetectorConfig
    except Exception:
        return []
    out: List[Detector] = []
    try:
        with session_scope() as s:
            rows = s.execute(
                _select(DetectorConfig)
                .where(DetectorConfig.source == "pine_import")
            ).scalars().all()
            for row in rows:
                if not row.name or not row.pine_source:
                    continue
                try:
                    det = PineCustomDetector(row.name, row.pine_source)
                    if det._rules:
                        out.append(det)
                except Exception:
                    logger.debug("pine custom detector build failed for %s",
                                       row.name, exc_info=True)
    except Exception:
        logger.debug("pine custom detector scan failed", exc_info=True)
    return out
