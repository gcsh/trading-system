"""Stage-3 Black-Scholes pricing + Greeks + implied volatility.

The bot's options layer needs to answer four questions at quote time:

  1. **Theoretical value** — what's this contract worth right now given a
     volatility input? (``bs_price``)
  2. **First-order Greeks** — delta, gamma, theta, vega.
  3. **Implied volatility** — given a market price, what σ is the market
     pricing in? (``implied_vol``, bisection — always converges in [0.01, 5])
  4. **One-call breakdown** — combined ``compute_greeks`` returning every
     answer in a single dict for the chain endpoint.

Pure math — no DB, no network. ``compute_greeks`` is called once per chain
quote when the surface is built. ``implied_vol`` is bisection (no SciPy
dependency) so the module stays portable.

Conventions:
  • Time in years (T = dte / 365)
  • Rate (r) and volatility (σ) as decimals (0.05 = 5%)
  • Strike (K), Spot (S), Price always in USD
  • Greeks per ONE share of the underlying — multiply by 100 for per-contract
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.config import TUNABLES


# ── normal CDF / PDF without SciPy ─────────────────────────────────────────


def _norm_cdf(x: float) -> float:
    """Φ(x) — standard normal CDF via math.erf. Python 3.14 has it built in."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """φ(x) — standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── Black-Scholes ──────────────────────────────────────────────────────────


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    """Shared term for every BS formula. Caller checks domain (T>0, σ>0)."""
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, sigma: float,
              kind: str = "call") -> float:
    """Black-Scholes theoretical price. Returns 0.0 on degenerate inputs
    (T≤0, σ≤0, K≤0) — the caller decides how to surface "no value"."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        # At expiry (T=0) the price is intrinsic — still report it honestly.
        if T <= 0 and S > 0 and K > 0:
            return max(0.0, (S - K) if kind == "call" else (K - S))
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if kind == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# ── Greeks ────────────────────────────────────────────────────────────────


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float        # PER DAY (already / 365)
    vega: float         # PER 1 VOL POINT (already / 100)
    rho: float          # per 1% rate change (already / 100)
    price: float        # theoretical BS price at the supplied σ

    def to_dict(self) -> Dict[str, float]:
        return {"delta": round(self.delta, 4), "gamma": round(self.gamma, 6),
                 "theta": round(self.theta, 4), "vega": round(self.vega, 4),
                 "rho": round(self.rho, 4), "price": round(self.price, 4)}


def compute_greeks(S: float, K: float, T: float, sigma: float,
                    *, r: Optional[float] = None, kind: str = "call") -> Greeks:
    """All first-order Greeks in one call. ``r`` defaults to ``TUNABLES.risk_free_rate``."""
    if r is None:
        r = float(getattr(TUNABLES, "risk_free_rate", 0.045))
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        # Degenerate — return zeros except the intrinsic price at expiry.
        return Greeks(0.0, 0.0, 0.0, 0.0, 0.0,
                       bs_price(S, K, max(T, 1e-9), r, max(sigma, 1e-9), kind))
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    sqrt_T = math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)

    if kind == "call":
        delta = _norm_cdf(d1)
        # Daily theta — divide by 365 so the UI doesn't show "-365" for a
        # 1-year call.
        theta = (-S * pdf_d1 * sigma / (2 * sqrt_T)
                  - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
        rho = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-S * pdf_d1 * sigma / (2 * sqrt_T)
                  + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
        rho = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100.0

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    # Vega — per 1 VOL POINT (e.g. 0.20 → 0.21 is one point).
    vega = S * pdf_d1 * sqrt_T / 100.0
    price = bs_price(S, K, T, r, sigma, kind)
    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho,
                   price=price)


# ── Implied vol — bisection (no SciPy needed) ──────────────────────────────


# ── Stage-15 position helper (used by bot/scenarios) ─────────────────────


def years_to_expiry(expiration, *, reference=None) -> Optional[float]:
    """ISO-date or date/datetime → fractional years to expiration.
    Returns ``None`` on unparseable, and 0.0 when already expired."""
    from datetime import date, datetime
    if expiration is None:
        return None
    if isinstance(expiration, (date, datetime)):
        d = expiration if isinstance(expiration, date) else expiration.date()
    else:
        try:
            d = datetime.fromisoformat(str(expiration)[:10]).date()
        except Exception:
            return None
    ref = (reference or datetime.utcnow()).date()
    days = (d - ref).days
    return max(0.0, days / 365.0)


def greeks_from_position(position: Dict[str, Any],
                            *, reference=None) -> Greeks:
    """Translate an executor position dict (``strike``, ``expiration``,
    ``option_type``, ``current_price``, optional ``meta.iv`` / ``meta.iv_rank``)
    into a fully-populated ``Greeks`` instance.

    Returns degenerate-zero Greeks when inputs are too thin to compute —
    callers should treat that as the signal to fall back to a heuristic.
    """
    strike = float(position.get("strike") or 0.0)
    opt_type = (position.get("option_type") or "call").lower()
    expiration = position.get("expiration")
    T = years_to_expiry(expiration, reference=reference) or 0.0
    underlying = float(position.get("underlying_price")
                          or position.get("current_price")
                          or 0.0)
    meta = position.get("meta") or {}
    iv = None
    if isinstance(meta, dict):
        iv = meta.get("iv") or meta.get("implied_volatility")
    if iv is None:
        iv_rank = position.get("iv_rank")
        if iv_rank is None and isinstance(meta, dict):
            iv_rank = meta.get("iv_rank")
        if iv_rank is not None:
            try:
                # IV rank 0..100 → decimal IV ~ 15% .. 45%
                iv = 0.15 + 0.30 * (float(iv_rank) / 100.0)
            except Exception:
                iv = 0.30
        else:
            iv = 0.30
    return compute_greeks(underlying, strike, T, float(iv), kind=opt_type)


def implied_vol(price: float, S: float, K: float, T: float,
                 *, r: Optional[float] = None, kind: str = "call",
                 lo: float = 0.01, hi: float = 5.0,
                 tol: float = 1e-4, max_iter: int = 80) -> Optional[float]:
    """Solve for σ such that BS(σ) = ``price`` via bisection.

    Bisection is preferred over Newton-Raphson for portability: it always
    converges as long as the option price is bounded by the [σ=lo, σ=hi]
    BS values. Returns None when the price is outside the bracket (e.g.
    ITM beyond intrinsic by more than the time-value range can explain).
    """
    if r is None:
        r = float(getattr(TUNABLES, "risk_free_rate", 0.045))
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if kind == "call" else (K - S))
    if price < intrinsic - 1e-6:
        # Below intrinsic value — quote is broken; can't recover an IV.
        return None
    f_lo = bs_price(S, K, T, r, lo, kind) - price
    f_hi = bs_price(S, K, T, r, hi, kind) - price
    if f_lo * f_hi > 0:
        return None        # price not bracketed
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(S, K, T, r, mid, kind) - price
        if abs(f_mid) < tol:
            return round(mid, 6)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round(0.5 * (lo + hi), 6)
