"""P2.3 — IV regime classifier unit tests."""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

from backend.bot.iv_regime import (
    _autocorr_lag1,
    _classify,
    _linreg_slope,
)


def _dates(n: int, start: date = date(2024, 1, 1)) -> list:
    return [start + timedelta(days=i) for i in range(n)]


def test_linreg_slope_uptrend():
    xs = list(range(50))
    ys = [x * 0.001 for x in xs]  # rising 0.001/day
    slope = _linreg_slope(xs, ys)
    assert abs(slope - 0.001) < 1e-6


def test_linreg_slope_flat():
    xs = list(range(20))
    ys = [0.25] * 20
    assert _linreg_slope(xs, ys) == 0.0


def test_autocorr_persistent():
    # Strictly linear series — lag-1 autocorr → 1 as n grows.
    # Lower bound 0.85 leaves room for the tail bias on n=40.
    values = [0.20 + 0.001 * i for i in range(40)]
    ac = _autocorr_lag1(values)
    assert ac > 0.85


def test_autocorr_oscillating():
    # Alternating series → strongly negative autocorr.
    values = [0.25 + ((-1) ** i) * 0.03 for i in range(40)]
    ac = _autocorr_lag1(values)
    assert ac < 0.0


def test_classify_unknown_when_too_few_samples():
    vals = [0.25] * 10
    dates = _dates(10)
    regime, conf, _ = _classify(vals, dates)
    assert regime == "unknown"
    assert conf == 0.0


def test_classify_trending_up():
    # Strong rising series over 60 days, slope >> _TREND_SLOPE.
    vals = [0.20 + 0.002 * i for i in range(60)]
    dates = _dates(60)
    regime, conf, _ = _classify(vals, dates)
    assert regime == "trending_up"
    assert conf > 0.0


def test_classify_trending_down():
    vals = [0.50 - 0.002 * i for i in range(60)]
    dates = _dates(60)
    regime, _, _ = _classify(vals, dates)
    assert regime == "trending_down"


def test_classify_expanding_when_recent_variance_jumps():
    # First half: quiet (std ~ 0.005). Second half: noisy (std ~ 0.05).
    quiet = [0.25 + 0.003 * (i % 2) for i in range(30)]
    noisy = [0.25 + 0.06 * ((i % 7) / 7 - 0.5) for i in range(30)]
    vals = quiet + noisy
    dates = _dates(60)
    regime, _, _ = _classify(vals, dates)
    assert regime == "expanding"


def test_classify_stable_low():
    # Slowly-drifting low-variance series: stays in a band <_STD_LOW
    # AND has high lag-1 autocorrelation (each value close to prior).
    # Period-3 modulo (the prior version) defeats lag-1 autocorr;
    # a tiny random-walk-like increment yields persistent neighbors.
    vals = [0.22 + 0.0002 * (i // 6) for i in range(60)]
    dates = _dates(60)
    regime, _, _ = _classify(vals, dates)
    assert regime == "stable_low"


def test_classify_mean_reverting_default():
    # Moderate variance, low autocorr (oscillating) → mean_reverting.
    vals = [0.30 + ((-1) ** i) * 0.04 + 0.001 * (i % 5) for i in range(60)]
    dates = _dates(60)
    regime, _, _ = _classify(vals, dates)
    assert regime == "mean_reverting"
