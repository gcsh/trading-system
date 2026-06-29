"""Black-Scholes pricing + Greeks — P2.1.

Pure-function module. No external dependencies (uses math.erf for the
normal CDF). Used by paper_executor for:

  * BS-fallback option pricing when ThetaData chain quote is stale or
    unavailable (P2.2).
  * MTM repricing for open positions when chain marks are unfresh (P2.3).

Conventions:
  * ``spot``, ``strike`` — positive currency.
  * ``dte`` — calendar days to expiration (we convert to T = dte/365).
  * ``iv`` — annualized vol as a decimal (0.30 = 30%).
  * ``rate`` — risk-free rate as decimal (default 0.05 ≈ 1Y T-bill).
  * ``kind`` — "call" or "put".

Returns are per-share (option contract = 100 × per-share).
"""
from __future__ import annotations

import math
from typing import Tuple


_RATE_DEFAULT = 0.05
_T_MIN = 1.0 / 365.0


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via math.erf — same identity scipy uses."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard-normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _validate(spot: float, strike: float, dte: float, iv: float) -> None:
    if spot <= 0:
        raise ValueError(f"spot must be > 0, got {spot}")
    if strike <= 0:
        raise ValueError(f"strike must be > 0, got {strike}")
    if dte < 0:
        raise ValueError(f"dte must be >= 0, got {dte}")
    if iv <= 0:
        raise ValueError(f"iv must be > 0, got {iv}")


def _d1_d2(spot: float, strike: float, dte: float, iv: float,
              rate: float = _RATE_DEFAULT) -> Tuple[float, float, float]:
    """Returns (d1, d2, T) used by every Greek."""
    T = max(_T_MIN, dte / 365.0)
    sigma_sqrt_t = iv * math.sqrt(T)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * T) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return d1, d2, T


def price(spot: float, strike: float, dte: float, iv: float,
             rate: float = _RATE_DEFAULT, kind: str = "call") -> float:
    """Per-share Black-Scholes option price."""
    _validate(spot, strike, dte, iv)
    d1, d2, T = _d1_d2(spot, strike, dte, iv, rate)
    discount = math.exp(-rate * T)
    if kind.lower().startswith("c"):
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def delta(spot: float, strike: float, dte: float, iv: float,
              rate: float = _RATE_DEFAULT, kind: str = "call") -> float:
    """∂price/∂spot."""
    _validate(spot, strike, dte, iv)
    d1, _, _ = _d1_d2(spot, strike, dte, iv, rate)
    if kind.lower().startswith("c"):
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def gamma(spot: float, strike: float, dte: float, iv: float,
              rate: float = _RATE_DEFAULT) -> float:
    """∂²price/∂spot². Same for calls and puts."""
    _validate(spot, strike, dte, iv)
    d1, _, T = _d1_d2(spot, strike, dte, iv, rate)
    return _norm_pdf(d1) / (spot * iv * math.sqrt(T))


def theta(spot: float, strike: float, dte: float, iv: float,
              rate: float = _RATE_DEFAULT, kind: str = "call") -> float:
    """∂price/∂t — per CALENDAR day (industry convention).
    Returns a NEGATIVE number for long positions."""
    _validate(spot, strike, dte, iv)
    d1, d2, T = _d1_d2(spot, strike, dte, iv, rate)
    discount = math.exp(-rate * T)
    common = -(spot * _norm_pdf(d1) * iv) / (2.0 * math.sqrt(T))
    if kind.lower().startswith("c"):
        annual = common - rate * strike * discount * _norm_cdf(d2)
    else:
        annual = common + rate * strike * discount * _norm_cdf(-d2)
    return annual / 365.0


def vega(spot: float, strike: float, dte: float, iv: float,
             rate: float = _RATE_DEFAULT) -> float:
    """∂price/∂iv per 1% absolute IV change (i.e. 0.01 vol move).
    Same for calls and puts."""
    _validate(spot, strike, dte, iv)
    d1, _, T = _d1_d2(spot, strike, dte, iv, rate)
    return spot * _norm_pdf(d1) * math.sqrt(T) / 100.0


def implied_iv(spot: float, strike: float, dte: float, mid_price: float,
                  rate: float = _RATE_DEFAULT, kind: str = "call",
                  iv_guess: float = 0.30,
                  tol: float = 1e-5, max_iter: int = 60) -> float:
    """Newton-Raphson implied volatility from observed mid price.
    Returns None when convergence fails. Useful for re-marking stored
    IV against a freshly observed chain mid."""
    iv = max(0.01, iv_guess)
    try:
        for _ in range(max_iter):
            p = price(spot, strike, dte, iv, rate, kind)
            v = vega(spot, strike, dte, iv, rate) * 100.0  # back to per-vol
            if v <= 1e-9:
                break
            diff = p - mid_price
            if abs(diff) < tol:
                return iv
            iv = max(0.005, iv - diff / v)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    # Final check.
    try:
        if abs(price(spot, strike, dte, iv, rate, kind) - mid_price) < 0.01:
            return iv
    except Exception:
        pass
    return None


def snapshot(spot: float, strike: float, dte: float, iv: float,
                 rate: float = _RATE_DEFAULT, kind: str = "call") -> dict:
    """Full pricing snapshot. Used at trade entry to store entry_iv +
    entry greeks on the PaperPosition row."""
    return {
        "price": price(spot, strike, dte, iv, rate, kind),
        "delta": delta(spot, strike, dte, iv, rate, kind),
        "gamma": gamma(spot, strike, dte, iv, rate),
        "theta": theta(spot, strike, dte, iv, rate, kind),
        "vega":  vega(spot, strike, dte, iv, rate),
        "iv":    iv,
        "rate":  rate,
    }
