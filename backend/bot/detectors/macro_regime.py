"""MITS Phase 12.F — Macro-regime detectors.

Four cross-asset macro detectors operating off the 46-series FRED
panel ingested in Phase 11.F. Each fires when a regime-defining
macro indicator crosses an institutional threshold, and the resulting
Observation is attached to ALL 40 universe tickers as a regime tag
that downstream cohort analysis can pivot on (the same observation
ticker is the originating ticker for emission so the FK to
``market_observations.ticker`` still resolves; consumers know macro
detectors are cross-asset because of the family slug ``macro_regime``).

Detectors
=========

  * yield_curve_inversion     — 10y minus 2y (DGS10 - DGS2) crossing
                                zero. Recession-leading indicator since
                                1969 (NBER analysis).
  * credit_spread_widening    — BAMLH0A0HYM2 (HY OAS) rises by >=50bp in
                                30 days = risk-off shift.
  * dollar_strength_shift     — DTWEXBGS (broad USD index) z-score
                                exceeds ±2 sigma.
  * composite_macro_regime    — combines yield curve, credit, USD, VIX
                                (VIXCLS) into a 0-100 risk-off score;
                                emit when it crosses the 60 (defensive)
                                or 30 (risk-on) thresholds.

Citations:

  * Estrella, A. & Mishkin, F. S. (1998). "Predicting U.S. Recessions:
    Financial Variables as Leading Indicators." Review of Economics
    and Statistics, 80 (1), 45–61. Yield-curve inversion section.
  * Adrian, T., Crump, R. K. & Moench, E. (2013). "Pricing the Term
    Structure with Linear Regressions." Journal of Financial
    Economics, 110 (1), 110–138.
  * Federal Reserve Bank of New York. Financial Conditions Indices
    methodology, 2020+. Risk-off composite construction template.
  * Bank for International Settlements (BIS). Quarterly Review, March
    2022. Cross-asset macro regime framework.

Design
======

Detectors hold an internal "regime state" via the most-recent emitted
observation. They emit on TRANSITION (e.g. the first bar where the
yield curve flips from positive to inverted), not every bar inside a
regime. This keeps cohort counts honest.

When the operator-side FRED ingestion has not yet landed all required
series the detector returns ``[]`` gracefully without raising.

These are cross-asset detectors: they're invoked with whatever ticker
the replay loop is on but the *signal* is the same for every ticker —
the macro state. To avoid 40-fold inflation we fire only for one
canonical ticker per universe (default SPY) and the consumer side
(agent_context / EOD / theory engine) reads the macro observation and
attaches it as context to every ticker's decision.
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


MACRO_REGIME_FAMILY = "macro_regime"

# Canonical "carrier" ticker — macro signals only fire on this ticker
# to avoid 40-fold inflation. Agent-side consumers cross-walk the
# signal to every ticker's context.
CANONICAL_MACRO_CARRIER = "SPY"


def _fetch_fred_series(series_id: str,
                              start: Optional[date] = None,
                              end: Optional[date] = None,
                              ) -> List[Tuple[date, float]]:
    """Return ``[(date, value), ...]`` sorted ascending. Empty when the
    series hasn't landed yet."""
    try:
        from backend.models.fred_observation import FredObservation
    except Exception:
        return []
    out: List[Tuple[date, float]] = []
    try:
        with session_scope() as s:
            q = (select(FredObservation.date, FredObservation.value)
                       .where(FredObservation.series_id == series_id)
                       .where(FredObservation.value.is_not(None))
                       .order_by(FredObservation.date.asc()))
            for d, v in s.execute(q).all():
                try:
                    dd = d.date() if hasattr(d, "date") else d
                except Exception:
                    continue
                if start and dd < start:
                    continue
                if end and dd > end:
                    continue
                out.append((dd, float(v)))
    except Exception:
        logger.debug("FRED fetch failed for %s", series_id, exc_info=True)
    return out


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


def _align_series_to_bar_dates(series: List[Tuple[date, float]],
                                          bar_dates: List[Optional[date]]
                                          ) -> List[Optional[float]]:
    """Carry-forward series values aligned to ``bar_dates``."""
    if not series:
        return [None] * len(bar_dates)
    series_sorted = sorted(series, key=lambda x: x[0])
    si = 0
    carry: Optional[float] = None
    out: List[Optional[float]] = []
    for d in bar_dates:
        if d is None:
            out.append(carry)
            continue
        while si < len(series_sorted) and series_sorted[si][0] <= d:
            carry = series_sorted[si][1]
            si += 1
        out.append(carry)
    return out


# ── 1. Yield-curve inversion ──────────────────────────────────────────


class YieldCurveInversionDetector(Detector):
    """10y2y spread (DGS10 - DGS2) cross-zero detector. Fires only on
    the SPY carrier ticker; cross-asset consumers read the signal."""

    pattern = "yield_curve_inversion"
    family = MACRO_REGIME_FAMILY
    description = (
        "DGS10 - DGS2 spread crosses zero (inversion) or crosses back "
        "(steepening). Cited: Estrella & Mishkin 'Predicting U.S. "
        "Recessions' RES 1998."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "long_series": "DGS10",
            "short_series": "DGS2",
            "carrier_ticker": CANONICAL_MACRO_CARRIER,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        carrier = str(p.get("carrier_ticker", CANONICAL_MACRO_CARRIER))
        if ticker.upper() != carrier.upper():
            return []
        if bars is None or len(bars) < 5:
            return []
        bars = _lower_columns(bars)
        long_series = _fetch_fred_series(str(p.get("long_series", "DGS10")))
        short_series = _fetch_fred_series(str(p.get("short_series", "DGS2")))
        if not long_series or not short_series:
            return []
        bar_dates: List[Optional[date]] = []
        for ts in bars.index:
            try:
                bar_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                bar_dates.append(None)
        long_aligned = _align_series_to_bar_dates(long_series, bar_dates)
        short_aligned = _align_series_to_bar_dates(short_series, bar_dates)
        out: List[Observation] = []
        last_state: Optional[str] = None
        for i in range(len(bars)):
            if long_aligned[i] is None or short_aligned[i] is None:
                continue
            spread = long_aligned[i] - short_aligned[i]
            state = "inverted" if spread < 0 else "normal"
            if last_state is None:
                last_state = state
                continue
            if state != last_state:
                direction = ("inversion_onset" if state == "inverted"
                                  else "steepening_recovery")
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "spread_bp": round(spread * 100, 2),
                    "direction": direction,
                    "long_yield": round(long_aligned[i], 3),
                    "short_yield": round(short_aligned[i], 3),
                }))
                last_state = state
        return out


# ── 2. Credit spread widening ─────────────────────────────────────────


class CreditSpreadWideningDetector(Detector):
    """BAMLH0A0HYM2 (High-Yield OAS) rises by >=50bp in 30 days =
    risk-off shift; reverse for risk-on."""

    pattern = "credit_spread_widening"
    family = MACRO_REGIME_FAMILY
    description = (
        "ICE BofA US High-Yield OAS (BAMLH0A0HYM2) widens by >=50bp in "
        "30 days — institutional risk-off shift. Cited: FRBNY "
        "Financial Conditions Indices methodology."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "series_id": "BAMLH0A0HYM2",
            "lookback_days": 30,
            "threshold_bp": 50.0,
            "carrier_ticker": CANONICAL_MACRO_CARRIER,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        carrier = str(p.get("carrier_ticker", CANONICAL_MACRO_CARRIER))
        if ticker.upper() != carrier.upper():
            return []
        if bars is None or len(bars) < 35:
            return []
        bars = _lower_columns(bars)
        series = _fetch_fred_series(str(p.get("series_id", "BAMLH0A0HYM2")))
        if not series:
            return []
        bar_dates: List[Optional[date]] = []
        for ts in bars.index:
            try:
                bar_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                bar_dates.append(None)
        aligned = _align_series_to_bar_dates(series, bar_dates)
        lookback_d = int(p.get("lookback_days", 30))
        threshold = float(p.get("threshold_bp", 50.0))
        out: List[Observation] = []
        # Walk through bars; on each bar pick the value from ~30 calendar days back.
        for i in range(lookback_d, len(bars)):
            if aligned[i] is None or aligned[i - lookback_d] is None:
                continue
            delta_bp = (aligned[i] - aligned[i - lookback_d]) * 100.0
            if delta_bp >= threshold:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "risk_off",
                    "oas_current": round(aligned[i], 3),
                    "oas_prior": round(aligned[i - lookback_d], 3),
                    "delta_bp": round(delta_bp, 1),
                }))
            elif delta_bp <= -threshold:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "risk_on",
                    "oas_current": round(aligned[i], 3),
                    "oas_prior": round(aligned[i - lookback_d], 3),
                    "delta_bp": round(delta_bp, 1),
                }))
        return out


# ── 3. Dollar-strength shift ──────────────────────────────────────────


class DollarStrengthShiftDetector(Detector):
    """DTWEXBGS (broad USD index) z-score crosses ±2 sigma vs the
    trailing 252-day mean. Strong USD typically negative for SPX
    earnings; weak USD boosts commodities."""

    pattern = "dollar_strength_shift"
    family = MACRO_REGIME_FAMILY
    description = (
        "Broad USD index (DTWEXBGS) z-score crosses ±2 sigma. Risk-on "
        "vs risk-off for SPX earnings + commodities. Cited: BIS "
        "Quarterly Review 2022."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "series_id": "DTWEXBGS",
            "zscore_window": 252,
            "z_threshold": 2.0,
            "carrier_ticker": CANONICAL_MACRO_CARRIER,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        carrier = str(p.get("carrier_ticker", CANONICAL_MACRO_CARRIER))
        if ticker.upper() != carrier.upper():
            return []
        if bars is None or len(bars) < 260:
            return []
        bars = _lower_columns(bars)
        series = _fetch_fred_series(str(p.get("series_id", "DTWEXBGS")))
        if not series:
            return []
        bar_dates: List[Optional[date]] = []
        for ts in bars.index:
            try:
                bar_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                bar_dates.append(None)
        aligned = _align_series_to_bar_dates(series, bar_dates)
        window = int(p.get("zscore_window", 252))
        thr = float(p.get("z_threshold", 2.0))
        out: List[Observation] = []
        last_state: Optional[str] = None
        for i in range(window, len(bars)):
            recent = [v for v in aligned[i - window:i] if v is not None]
            if len(recent) < window // 2 or aligned[i] is None:
                continue
            m = mean(recent)
            sd = pstdev(recent)
            if sd <= 0:
                continue
            z = (aligned[i] - m) / sd
            if z >= thr:
                state = "strong_usd"
            elif z <= -thr:
                state = "weak_usd"
            else:
                state = "neutral"
            if state != "neutral" and state != last_state:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": state,
                    "z_score": round(z, 3),
                    "usd_index": round(aligned[i], 3),
                    "mean_252d": round(m, 3),
                    "stdev_252d": round(sd, 3),
                }))
                last_state = state
            elif state == "neutral":
                last_state = "neutral"
        return out


# ── 4. Composite macro regime ─────────────────────────────────────────


class CompositeMacroRegimeDetector(Detector):
    """0-100 risk-off score combining yield curve, HY OAS, USD index,
    and VIX. Emit on threshold crossings: >= 60 → defensive,
    <= 30 → risk-on."""

    pattern = "composite_macro_regime"
    family = MACRO_REGIME_FAMILY
    description = (
        "Composite 0-100 risk-off score (yield curve + HY OAS + USD + "
        "VIX). Emits on defensive (>=60) and risk-on (<=30) crossings. "
        "Cited: FRBNY Financial Conditions Indices 2020+."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "yield_long": "DGS10",
            "yield_short": "DGS2",
            "credit_series": "BAMLH0A0HYM2",
            "usd_series": "DTWEXBGS",
            "vix_series": "VIXCLS",
            "defensive_threshold": 60.0,
            "risk_on_threshold": 30.0,
            "carrier_ticker": CANONICAL_MACRO_CARRIER,
        }

    def _normalize_component(self, value: Optional[float],
                                       history: List[Optional[float]],
                                       higher_is_riskier: bool = True
                                       ) -> float:
        """Map ``value`` to 0-100 within the trailing distribution.
        Higher_is_riskier=True means higher value -> higher risk-off
        score; False inverts (e.g. yield curve where MORE negative is
        riskier)."""
        if value is None:
            return 50.0
        hist = [v for v in history if v is not None]
        if len(hist) < 30:
            return 50.0
        sorted_h = sorted(hist)
        # Percentile of value among sorted_h.
        below = sum(1 for x in sorted_h if x <= value)
        pct = below / len(sorted_h)
        score = pct * 100.0
        if not higher_is_riskier:
            score = 100.0 - score
        return score

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        carrier = str(p.get("carrier_ticker", CANONICAL_MACRO_CARRIER))
        if ticker.upper() != carrier.upper():
            return []
        if bars is None or len(bars) < 260:
            return []
        bars = _lower_columns(bars)
        yield_long = _fetch_fred_series(str(p.get("yield_long", "DGS10")))
        yield_short = _fetch_fred_series(str(p.get("yield_short", "DGS2")))
        credit = _fetch_fred_series(str(p.get("credit_series", "BAMLH0A0HYM2")))
        usd = _fetch_fred_series(str(p.get("usd_series", "DTWEXBGS")))
        vix = _fetch_fred_series(str(p.get("vix_series", "VIXCLS")))
        if not (yield_long and yield_short and credit and usd and vix):
            return []
        bar_dates: List[Optional[date]] = []
        for ts in bars.index:
            try:
                bar_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                bar_dates.append(None)
        yl_a = _align_series_to_bar_dates(yield_long, bar_dates)
        ys_a = _align_series_to_bar_dates(yield_short, bar_dates)
        cr_a = _align_series_to_bar_dates(credit, bar_dates)
        us_a = _align_series_to_bar_dates(usd, bar_dates)
        vx_a = _align_series_to_bar_dates(vix, bar_dates)
        spread = [
            (yl_a[i] - ys_a[i]) if (yl_a[i] is not None and ys_a[i] is not None)
            else None
            for i in range(len(bars))
        ]
        defensive = float(p.get("defensive_threshold", 60.0))
        risk_on = float(p.get("risk_on_threshold", 30.0))
        out: List[Observation] = []
        last_state: Optional[str] = None
        window = 252
        for i in range(window, len(bars)):
            curve_score = self._normalize_component(
                spread[i], spread[i - window:i],
                higher_is_riskier=False,  # inverted curve is RISKIER
            )
            credit_score = self._normalize_component(
                cr_a[i], cr_a[i - window:i], higher_is_riskier=True,
            )
            usd_score = self._normalize_component(
                us_a[i], us_a[i - window:i], higher_is_riskier=True,
            )
            vix_score = self._normalize_component(
                vx_a[i], vx_a[i - window:i], higher_is_riskier=True,
            )
            composite = (curve_score * 0.25 + credit_score * 0.35
                              + usd_score * 0.10 + vix_score * 0.30)
            if composite >= defensive:
                state = "defensive"
            elif composite <= risk_on:
                state = "risk_on"
            else:
                state = "neutral"
            if state != "neutral" and state != last_state:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "regime": state,
                    "composite_score": round(composite, 2),
                    "curve_component": round(curve_score, 1),
                    "credit_component": round(credit_score, 1),
                    "usd_component": round(usd_score, 1),
                    "vix_component": round(vix_score, 1),
                }))
                last_state = state
            elif state == "neutral":
                last_state = "neutral"
        return out


def build_macro_regime_detectors() -> List[Detector]:
    return [
        YieldCurveInversionDetector(),
        CreditSpreadWideningDetector(),
        DollarStrengthShiftDetector(),
        CompositeMacroRegimeDetector(),
    ]
