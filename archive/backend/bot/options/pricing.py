"""Real option pricing — P2.2 / P2.3.

Resolves a clean ``OptionMark`` (bid/ask/mid/iv/greeks + source +
freshness) from the live chain when possible, otherwise Black-Scholes
from a stored IV.

Source hierarchy:
  1. ThetaData chain quote (preferred). Mid = (bid + ask) / 2.
  2. Black-Scholes (bs_fallback) using ``stored_iv`` (or entry_iv).
  3. Stub (paper_stub) — last-resort placeholder used by the legacy
     codepath. Should be eliminated by P2.5 (paper-trial reset).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from backend.bot.options import blackscholes as _bs

logger = logging.getLogger(__name__)


# Chain mid is considered "fresh" if it was observed within this window.
# At entry the freshness rule is strict; at MTM we widen it. See P1.4 /
# the staleness policy table for the lifecycle-aware nuance.
ENTRY_MAX_AGE_SEC = 60
MARK_MAX_AGE_SEC = 600


@dataclass
class OptionMark:
    """Result of pricing one option contract."""
    bid: Optional[float]
    ask: Optional[float]
    mid: float
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]
    source: str            # thetadata | bs_fallback | paper_stub
    age_seconds: Optional[float]
    underlying: Optional[float]

    def to_dict(self) -> dict:
        return {
            "bid": self.bid, "ask": self.ask, "mid": self.mid,
            "iv": self.iv, "delta": self.delta, "gamma": self.gamma,
            "theta": self.theta, "vega": self.vega,
            "source": self.source, "age_seconds": self.age_seconds,
            "underlying": self.underlying,
        }


def _parse_expiration(expiration) -> Optional[date]:
    if isinstance(expiration, date):
        return expiration
    if isinstance(expiration, str):
        try:
            return date.fromisoformat(expiration[:10])
        except ValueError:
            return None
    return None


def _chain_mark(symbol: str, expiration: date, strike: float,
                   right: str) -> Optional[OptionMark]:
    """Pull ThetaData chain snapshot, find the matching strike/right."""
    try:
        from backend.bot.data.thetadata import get_client
        cl = get_client()
        if cl is None:
            return None
        quotes = cl.chain_snapshot(symbol, expiration)
    except Exception:
        logger.debug("chain_snapshot failed for %s %s",
                          symbol, expiration, exc_info=True)
        return None
    if not quotes:
        return None
    rstr = right.upper()
    # ThetaData uses single-char rights "C"/"P" in some payloads, full
    # "CALL"/"PUT" in others. Normalize both.
    rwant = "C" if rstr.startswith("C") else "P"
    target_strike = round(float(strike), 2)
    match = None
    for q in quotes:
        qstr = str(getattr(q, "right", "") or "").upper()
        if qstr and not (qstr.startswith(rwant) or qstr.startswith(rstr)):
            continue
        try:
            if abs(round(float(q.strike), 2) - target_strike) < 0.005:
                match = q
                break
        except Exception:
            continue
    if match is None:
        return None
    bid = float(getattr(match, "bid", 0) or 0)
    ask = float(getattr(match, "ask", 0) or 0)
    if bid <= 0 and ask <= 0:
        return None
    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask)
    ts = getattr(match, "timestamp", None)
    age = None
    if isinstance(ts, datetime):
        # Compare in UTC. quotes timestamps are stamped by the terminal.
        try:
            age = max(0.0, (datetime.utcnow() - ts).total_seconds())
        except Exception:
            age = None
    return OptionMark(
        bid=bid, ask=ask, mid=round(mid, 4),
        iv=None, delta=None, gamma=None, theta=None, vega=None,
        source="thetadata", age_seconds=age, underlying=None,
    )


def _bs_mark(symbol: str, spot: float, strike: float, expiration: date,
                iv: float, right: str) -> Optional[OptionMark]:
    """Black-Scholes fallback. Requires a stored IV (typically entry_iv
    or stored_iv from the position row)."""
    dte = max(0, (expiration - date.today()).days)
    kind = "call" if right.upper().startswith("C") else "put"
    try:
        snap = _bs.snapshot(spot, strike, dte, iv, kind=kind)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    mid = snap["price"]
    # Synthetic bid/ask using the configured half-spread.
    try:
        from backend.config import TUNABLES
        half = mid * float(TUNABLES.broker_option_spread_pct) / 2.0
    except Exception:
        half = mid * 0.01
    return OptionMark(
        bid=round(max(0.0, mid - half), 4),
        ask=round(mid + half, 4),
        mid=round(mid, 4),
        iv=iv,
        delta=snap["delta"], gamma=snap["gamma"],
        theta=snap["theta"], vega=snap["vega"],
        source="bs_fallback", age_seconds=None, underlying=spot,
    )


def price_at_entry(
    *,
    symbol: str,
    spot: float,
    strike: float,
    expiration,
    right: str,
    iv_hint: Optional[float] = None,
) -> OptionMark:
    """Price a contract at trade entry. Strict freshness rule (≤ 60s)
    on chain quotes — anything stale falls through to BS.

    Returns an ``OptionMark`` with ``source`` indicating where the
    number came from. Never raises — falls back to a paper_stub mark
    if everything fails (caller decides whether to reject the trade)."""
    exp = _parse_expiration(expiration)
    if exp is None or strike <= 0 or spot <= 0:
        return _stub_mark(strike)

    # 1. Chain quote.
    chain = _chain_mark(symbol, exp, strike, right)
    if chain is not None and (chain.age_seconds is None
                                       or chain.age_seconds <= ENTRY_MAX_AGE_SEC):
        # Recover IV from chain mid if we have spot + DTE.
        dte = max(0, (exp - date.today()).days)
        if iv_hint is not None and iv_hint > 0:
            iv = float(iv_hint)
        else:
            kind = "call" if right.upper().startswith("C") else "put"
            iv = _bs.implied_iv(spot, strike, dte, chain.mid, kind=kind) or 0.30
        try:
            chain.iv = iv
            chain.delta = _bs.delta(spot, strike, dte, iv, kind=kind if isinstance(iv, float) else "call")
            chain.gamma = _bs.gamma(spot, strike, dte, iv)
            chain.theta = _bs.theta(spot, strike, dte, iv, kind=kind)
            chain.vega  = _bs.vega(spot, strike, dte, iv)
            chain.underlying = spot
        except Exception:
            pass
        return chain

    # 2. BS fallback.
    iv = float(iv_hint) if (iv_hint and iv_hint > 0) else 0.30
    bs = _bs_mark(symbol, spot, strike, exp, iv, right)
    if bs is not None:
        return bs

    # 3. Stub (shouldn't reach here in normal operation).
    return _stub_mark(strike)


def price_for_mark(
    *,
    symbol: str,
    spot: float,
    strike: float,
    expiration,
    right: str,
    stored_iv: Optional[float] = None,
) -> OptionMark:
    """Reprice an OPEN position for MTM. Looser freshness rule (≤ 600s)
    so we still mark with a real quote when ThetaData is sluggish.
    Marks coming from BS fallback carry ``source='bs_fallback'`` so the
    UI can show the data-quality downgrade."""
    exp = _parse_expiration(expiration)
    if exp is None or strike <= 0:
        return _stub_mark(strike)

    chain = _chain_mark(symbol, exp, strike, right)
    if chain is not None and (chain.age_seconds is None
                                       or chain.age_seconds <= MARK_MAX_AGE_SEC):
        return chain

    if stored_iv and stored_iv > 0 and spot > 0:
        bs = _bs_mark(symbol, spot, strike, exp, float(stored_iv), right)
        if bs is not None:
            return bs

    return _stub_mark(strike)


def _stub_mark(strike: float) -> OptionMark:
    """Last-resort placeholder. 3% × strike — matches the legacy stub
    so behavior is continuous for positions that pre-date P2.2."""
    mid = max(0.05, 0.03 * float(strike or 0))
    return OptionMark(
        bid=mid * 0.95, ask=mid * 1.05, mid=round(mid, 4),
        iv=None, delta=None, gamma=None, theta=None, vega=None,
        source="paper_stub", age_seconds=None, underlying=None,
    )
