"""MITS Phase 10 — Shared indicator math for theory modules.

These helpers are deliberately pure-Python (no numpy/pandas dep) so a
single-theory call inside a tight cache-miss path stays fast and the
modules remain easy to unit-test with synthetic bars.

Citations for each formula are kept in the calling theory module's
docstring, but the canonical sources are:

  * Wilder, "New Concepts in Technical Trading Systems" (1978) — RSI,
    ATR, +DI/-DI/ADX.
  * Appel, "Technical Analysis: Power Tools for Active Investors"
    (FT Press, 2005) — MACD spec.
  * Bollinger, "Bollinger on Bollinger Bands" (McGraw-Hill, 2001).
  * Lane, "Investment Educators Tape Service" (1957–58) — Stochastic.
  * Murphy, "Technical Analysis of the Financial Markets" (NYIF, 1999)
    — general TA reference for ATR, SMA, EMA conventions.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schema import bar_close, bar_high, bar_low, bar_open


# ── moving averages ───────────────────────────────────────────────────


def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Simple moving average. Returns ``None`` for the warm-up bars."""
    out: List[Optional[float]] = []
    if period <= 0:
        return [None] * len(values)
    running = 0.0
    buf: List[float] = []
    for v in values:
        buf.append(float(v))
        running += float(v)
        if len(buf) > period:
            running -= buf.pop(0)
        if len(buf) < period:
            out.append(None)
        else:
            out.append(running / period)
    return out


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Exponential moving average with SMA-seeded warm-up."""
    out: List[Optional[float]] = []
    if period <= 0 or not values:
        return [None] * len(values)
    k = 2.0 / (period + 1.0)
    seed: Optional[float] = None
    for i, v in enumerate(values):
        v = float(v)
        if seed is None:
            if i + 1 >= period:
                seed = sum(values[i + 1 - period:i + 1]) / period
                out.append(seed)
            else:
                out.append(None)
            continue
        seed = (v - seed) * k + seed
        out.append(seed)
    return out


def stdev(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Rolling sample standard deviation."""
    out: List[Optional[float]] = []
    if period <= 1 or not values:
        return [None] * len(values)
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
            continue
        window = [float(x) for x in values[i + 1 - period:i + 1]]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / (period - 1)
        out.append(math.sqrt(max(0.0, var)))
    return out


# ── price-series accessors ───────────────────────────────────────────


def closes(bars: List[Dict[str, Any]]) -> List[float]:
    return [bar_close(b) for b in bars]


def highs(bars: List[Dict[str, Any]]) -> List[float]:
    return [bar_high(b) for b in bars]


def lows(bars: List[Dict[str, Any]]) -> List[float]:
    return [bar_low(b) for b in bars]


def opens(bars: List[Dict[str, Any]]) -> List[float]:
    return [bar_open(b) for b in bars]


def typical(bars: List[Dict[str, Any]]) -> List[float]:
    """``(H + L + C) / 3`` per bar."""
    return [(bar_high(b) + bar_low(b) + bar_close(b)) / 3.0 for b in bars]


def volumes(bars: List[Dict[str, Any]]) -> List[float]:
    return [float(b.get("volume") or 0.0) for b in bars]


# ── volatility ────────────────────────────────────────────────────────


def true_range(bars: List[Dict[str, Any]]) -> List[float]:
    """True Range (Wilder)."""
    out: List[float] = []
    prev_c = None
    for b in bars:
        h = bar_high(b)
        l = bar_low(b)
        c = bar_close(b)
        if prev_c is None:
            out.append(max(0.0, h - l))
        else:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            out.append(max(0.0, tr))
        prev_c = c
    return out


def atr(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    """Average True Range using Wilder's RMA (1/n smoothing)."""
    tr = true_range(bars)
    out: List[Optional[float]] = []
    seed: Optional[float] = None
    for i, v in enumerate(tr):
        if i + 1 < period:
            out.append(None)
            continue
        if seed is None:
            seed = sum(tr[:period]) / period
            out.append(seed)
            continue
        seed = (seed * (period - 1) + v) / period
        out.append(seed)
    return out


# ── momentum ──────────────────────────────────────────────────────────


def rsi(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder RSI (RMA-smoothed average gain / loss)."""
    out: List[Optional[float]] = [None]
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        d = float(values[i]) - float(values[i - 1])
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    if len(values) <= period:
        return [None] * len(values)
    # Initial averages (simple mean of first ``period`` deltas).
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for _ in range(1, period):
        out.append(None)
    if avg_loss == 0:
        rsi_val = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_val = 100.0 - 100.0 / (1.0 + rs)
    out.append(rsi_val)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100.0 - 100.0 / (1.0 + rs)
        out.append(rsi_val)
    while len(out) < len(values):
        out.append(None)
    return out


def macd(
    values: Sequence[float],
    fast: int = 12, slow: int = 26, signal: int = 9,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Returns ``(macd_line, signal_line, histogram)``."""
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line: List[Optional[float]] = []
    for f, s in zip(fast_ema, slow_ema):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
    # Signal line is an EMA of the macd_line — but EMA helper needs
    # numeric input, so we replace ``None`` with the first numeric value
    # for the warm-up region and re-mask afterwards.
    first_idx = next((i for i, v in enumerate(macd_line) if v is not None), None)
    sig_line: List[Optional[float]] = [None] * len(macd_line)
    if first_idx is not None:
        clean = [v if v is not None else 0.0 for v in macd_line[first_idx:]]
        ema_sig = ema(clean, signal)
        for j, v in enumerate(ema_sig):
            idx = first_idx + j
            if v is None or idx < first_idx + signal:
                sig_line[idx] = None
            else:
                sig_line[idx] = v
    hist: List[Optional[float]] = []
    for m, s in zip(macd_line, sig_line):
        if m is None or s is None:
            hist.append(None)
        else:
            hist.append(m - s)
    return macd_line, sig_line, hist


def stochastic(
    bars: List[Dict[str, Any]],
    k_period: int = 14, d_period: int = 3,
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """Lane Stochastic %K / %D."""
    hh = highs(bars)
    ll = lows(bars)
    cc = closes(bars)
    k: List[Optional[float]] = []
    for i in range(len(bars)):
        if i + 1 < k_period:
            k.append(None)
            continue
        window_hi = max(hh[i + 1 - k_period:i + 1])
        window_lo = min(ll[i + 1 - k_period:i + 1])
        rng = window_hi - window_lo
        if rng <= 0:
            k.append(None)
        else:
            k.append(100.0 * (cc[i] - window_lo) / rng)
    # %D = SMA(d_period) of %K — handle Nones by substituting last.
    d: List[Optional[float]] = [None] * len(k)
    for i in range(len(k)):
        if i + 1 < d_period:
            continue
        chunk = [v for v in k[i + 1 - d_period:i + 1] if v is not None]
        if len(chunk) < d_period:
            continue
        d[i] = sum(chunk) / d_period
    return k, d


# ── bollinger ────────────────────────────────────────────────────────


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Returns ``(mid, upper, lower)``."""
    m = sma(values, period)
    s = stdev(values, period)
    upper: List[Optional[float]] = []
    lower: List[Optional[float]] = []
    for mi, si in zip(m, s):
        if mi is None or si is None:
            upper.append(None); lower.append(None)
        else:
            upper.append(mi + mult * si)
            lower.append(mi - mult * si)
    return m, upper, lower


# ── donchian ────────────────────────────────────────────────────────


def donchian(
    bars: List[Dict[str, Any]], period: int = 20,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Returns ``(upper, lower, mid)`` channel arrays."""
    hh = highs(bars)
    ll = lows(bars)
    upper: List[Optional[float]] = []
    lower: List[Optional[float]] = []
    mid: List[Optional[float]] = []
    for i in range(len(bars)):
        if i + 1 < period:
            upper.append(None); lower.append(None); mid.append(None)
            continue
        u = max(hh[i + 1 - period:i + 1])
        l = min(ll[i + 1 - period:i + 1])
        upper.append(u); lower.append(l); mid.append((u + l) / 2.0)
    return upper, lower, mid


# ── keltner ─────────────────────────────────────────────────────────


def keltner(
    bars: List[Dict[str, Any]], ema_period: int = 20, atr_period: int = 10,
    mult: float = 2.0,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Keltner Channel: EMA(close) ± mult × ATR(period)."""
    cc = closes(bars)
    mid = ema(cc, ema_period)
    a = atr(bars, atr_period)
    upper: List[Optional[float]] = []
    lower: List[Optional[float]] = []
    for m, av in zip(mid, a):
        if m is None or av is None:
            upper.append(None); lower.append(None)
        else:
            upper.append(m + mult * av)
            lower.append(m - mult * av)
    return mid, upper, lower


__all__ = [
    "sma", "ema", "stdev",
    "closes", "highs", "lows", "opens", "typical", "volumes",
    "true_range", "atr",
    "rsi", "macd", "stochastic",
    "bollinger", "donchian", "keltner",
]
