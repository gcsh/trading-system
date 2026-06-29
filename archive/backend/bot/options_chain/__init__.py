"""Stage-3 options chain — live strike availability + IV surface + risk.

Replaces the strike-snap heuristic with a real chain lookup. When yfinance
returns a chain we have:
  • the actual ladder of listed strikes
  • bid / ask / last / IV / volume / OI per contract
  • all expirations the symbol trades

When the chain isn't available (yfinance flaky, ticker has no options,
test environment) we fall back to ``snap_strike`` so the engine never
blocks waiting for data.

Module responsibilities — one ChainQuote per (strike, expiry, kind):
  • ``fetch_chain(ticker, expiration?)``      pull + cache
  • ``available_expirations(ticker)``         list of dates
  • ``nearest_available_strike(ticker, ...)`` chain-aware fallback
  • ``iv_surface(ticker)``                    pivoted (strike, dte) → IV
  • ``assignment_probability(...)``           short-option risk
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.bot.data.options import snap_strike
from backend.bot.greeks import compute_greeks, implied_vol
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── data model ─────────────────────────────────────────────────────────────


@dataclass
class ChainQuote:
    ticker: str
    expiration: str               # YYYY-MM-DD
    strike: float
    kind: str                     # "call" | "put"
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    last: float = 0.0
    iv: Optional[float] = None
    volume: int = 0
    open_interest: int = 0
    dte: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["iv"] = round(self.iv, 4) if self.iv is not None else None
        d["mid"] = round(self.mid, 4)
        return d


@dataclass
class OptionChain:
    ticker: str
    spot: float
    fetched_at: str
    source: str                   # "yfinance" | "cboe" | "synthetic" | "fallback"
    expirations: List[str] = field(default_factory=list)
    quotes: List[ChainQuote] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def by_expiry(self, expiration: str) -> List[ChainQuote]:
        return [q for q in self.quotes if q.expiration == expiration]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker, "spot": self.spot,
            "fetched_at": self.fetched_at, "source": self.source,
            "expirations": self.expirations,
            "quotes": [q.to_dict() for q in self.quotes],
            "notes": self.notes,
        }


# ── cache ──────────────────────────────────────────────────────────────────


_CACHE: Dict[str, Tuple[float, OptionChain]] = {}


def _ttl() -> float:
    return float(getattr(TUNABLES, "options_cache_ttl", 600.0))


def _cache_key(ticker: str, expiration: Optional[str]) -> str:
    return f"{ticker.upper()}::{expiration or 'ALL'}"


def _cached(ticker: str, expiration: Optional[str]) -> Optional[OptionChain]:
    hit = _CACHE.get(_cache_key(ticker, expiration))
    if hit and (time.monotonic() - hit[0]) < _ttl():
        return hit[1]
    return None


def _store(ticker: str, expiration: Optional[str], chain: OptionChain) -> None:
    _CACHE[_cache_key(ticker, expiration)] = (time.monotonic(), chain)


def clear_cache() -> None:
    """Test helper — invalidate the in-memory chain cache."""
    _CACHE.clear()


# ── fetchers ──────────────────────────────────────────────────────────────


def _dte_for(expiration: str) -> int:
    try:
        return max(0, (date.fromisoformat(expiration) - date.today()).days)
    except Exception:
        return 0


def _fetch_from_yfinance(ticker: str,
                          expiration: Optional[str] = None) -> Optional[OptionChain]:
    """Real chain via yfinance. Best-effort — returns None on any failure
    so callers can fall through to the synthetic chain."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(ticker.upper())
        try:
            spot = float(t.fast_info.get("lastPrice") or 0.0)
        except Exception:
            spot = 0.0
        if spot <= 0:
            try:
                hist = t.history(period="1d")
                spot = float(hist["Close"].iloc[-1])
            except Exception:
                return None

        all_exps: List[str] = list(t.options or [])
        if not all_exps:
            return None
        exps_to_pull = [expiration] if expiration and expiration in all_exps else all_exps[:3]

        quotes: List[ChainQuote] = []
        for exp in exps_to_pull:
            try:
                chain = t.option_chain(exp)
            except Exception:
                continue
            dte = _dte_for(exp)
            for df, kind in ((chain.calls, "call"), (chain.puts, "put")):
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    bid = float(row.get("bid") or 0.0)
                    ask = float(row.get("ask") or 0.0)
                    last = float(row.get("lastPrice") or 0.0)
                    mid = (bid + ask) / 2 if bid and ask else (last or bid or ask)
                    iv = row.get("impliedVolatility")
                    iv = float(iv) if iv and not math.isnan(iv) else None
                    quotes.append(ChainQuote(
                        ticker=ticker.upper(), expiration=exp,
                        strike=float(row.get("strike") or 0.0), kind=kind,
                        bid=bid, ask=ask, mid=mid, last=last, iv=iv,
                        volume=int(row.get("volume") or 0),
                        open_interest=int(row.get("openInterest") or 0),
                        dte=dte,
                    ))
        if not quotes:
            return None
        return OptionChain(
            ticker=ticker.upper(), spot=spot,
            fetched_at=datetime.utcnow().isoformat(),
            source="yfinance", expirations=all_exps, quotes=quotes,
        )
    except Exception:
        logger.debug("yfinance chain fetch failed for %s", ticker, exc_info=True)
        return None


def _synthetic_chain(ticker: str, spot: float,
                      expiration: Optional[str] = None) -> OptionChain:
    """Last-resort chain — generates a ladder around spot using snap_strike's
    interval table and a flat 0.30 IV. Used in tests + when yfinance is down
    so downstream code can still demonstrate behaviour."""
    if spot <= 0:
        return OptionChain(ticker=ticker.upper(), spot=0.0,
                            fetched_at=datetime.utcnow().isoformat(),
                            source="fallback", notes=["no spot price available"])

    # 7d / 30d / 60d default expirations.
    from datetime import timedelta
    today = date.today()
    if expiration:
        exps = [expiration]
    else:
        exps = [(today + timedelta(days=d)).isoformat() for d in (7, 30, 60)]

    # Strike ladder: ±15 rungs around spot at the price band's listed interval.
    bands = getattr(TUNABLES, "strike_intervals", None) or [
        (25.0, 0.50), (100.0, 1.0), (500.0, 5.0), (float("inf"), 10.0),
    ]
    interval = next((step for upper, step in bands if spot < float(upper)),
                     float(bands[-1][1]))
    atm = snap_strike(spot)
    strikes = [round(atm + i * interval, 2) for i in range(-15, 16) if atm + i * interval > 0]

    quotes: List[ChainQuote] = []
    for exp in exps:
        dte = max(1, _dte_for(exp))
        T = dte / 365.0
        sigma = 0.30  # flat default; iv-surface will use real data when available
        for strike in strikes:
            for kind in ("call", "put"):
                g = compute_greeks(spot, strike, T, sigma, kind=kind)
                mid = max(0.05, g.price)
                quotes.append(ChainQuote(
                    ticker=ticker.upper(), expiration=exp,
                    strike=strike, kind=kind,
                    bid=round(max(0.0, mid - 0.05), 2),
                    ask=round(mid + 0.05, 2), mid=round(mid, 2),
                    last=round(mid, 2), iv=sigma,
                    dte=dte,
                ))
    return OptionChain(ticker=ticker.upper(), spot=spot,
                        fetched_at=datetime.utcnow().isoformat(),
                        source="synthetic", expirations=exps, quotes=quotes,
                        notes=["yfinance unavailable — synthetic chain at σ=0.30"])


def fetch_chain(ticker: str, *, expiration: Optional[str] = None,
                 spot_hint: Optional[float] = None,
                 prefer_synthetic: bool = False) -> OptionChain:
    """Public entry — caches, then yfinance, then synthetic fallback."""
    hit = _cached(ticker, expiration)
    if hit is not None:
        return hit
    chain: Optional[OptionChain] = None
    if not prefer_synthetic:
        chain = _fetch_from_yfinance(ticker, expiration)
    if chain is None:
        chain = _synthetic_chain(ticker, spot_hint or 0.0, expiration)
    _store(ticker, expiration, chain)
    return chain


def available_expirations(ticker: str) -> List[str]:
    return fetch_chain(ticker).expirations


# ── strike availability + chain-aware fallback ─────────────────────────────


def nearest_available_strike(ticker: str, target: float, *,
                               kind: str = "call",
                               expiration: Optional[str] = None,
                               spot_hint: Optional[float] = None,
                               ) -> Tuple[float, str]:
    """Return the listed strike closest to ``target`` plus the source tag.

    Falls back to ``snap_strike`` when no chain is available so the engine
    never blocks; the source tag tells the caller what they got.
    """
    chain = fetch_chain(ticker, expiration=expiration, spot_hint=spot_hint)
    candidates: List[float] = []
    if chain.quotes:
        target_exp = expiration or (chain.expirations[1]
                                      if len(chain.expirations) >= 2
                                      else (chain.expirations[0] if chain.expirations else None))
        for q in chain.quotes:
            if q.kind == kind and (target_exp is None or q.expiration == target_exp):
                candidates.append(q.strike)
    if candidates:
        best = min(candidates, key=lambda s: abs(s - target))
        return float(best), chain.source
    return snap_strike(target, kind=kind), "snap_fallback"


# ── IV surface ────────────────────────────────────────────────────────────


@dataclass
class IVSurfaceSample:
    expiration: str
    dte: int
    strikes: List[float] = field(default_factory=list)
    call_iv: List[Optional[float]] = field(default_factory=list)
    put_iv: List[Optional[float]] = field(default_factory=list)


def iv_surface(ticker: str) -> Dict[str, Any]:
    """Pivot the chain into a (strike, dte) view per kind. The UI can render
    this as a heatmap or a per-expiry vol smile."""
    chain = fetch_chain(ticker)
    by_exp: Dict[str, Dict[float, Dict[str, Optional[float]]]] = {}
    for q in chain.quotes:
        by_exp.setdefault(q.expiration, {}).setdefault(q.strike, {"call": None, "put": None})
        by_exp[q.expiration][q.strike][q.kind] = q.iv

    samples: List[Dict[str, Any]] = []
    for exp in sorted(by_exp):
        strikes_sorted = sorted(by_exp[exp])
        sample = IVSurfaceSample(
            expiration=exp,
            dte=_dte_for(exp),
            strikes=strikes_sorted,
            call_iv=[by_exp[exp][s]["call"] for s in strikes_sorted],
            put_iv=[by_exp[exp][s]["put"] for s in strikes_sorted],
        )
        samples.append(asdict(sample))
    return {"ticker": ticker.upper(), "spot": chain.spot,
             "source": chain.source, "fetched_at": chain.fetched_at,
             "samples": samples}


# ── assignment risk for short options ─────────────────────────────────────


def assignment_probability(*, spot: float, strike: float, dte: int,
                             kind: str, ex_div_days: Optional[int] = None,
                             side: str = "SHORT") -> Dict[str, Any]:
    """Heuristic probability of early assignment on a short option.

    Inputs the engine cares about + delta-driven baseline:
      • ITM-ness: short call with spot > strike, or short put with spot < strike
      • DTE: probability spikes inside the last week
      • Dividends: short calls assigned the day before ex-div when ITM
                    (so the buyer captures the dividend)
    Output ∈ [0, 1] plus the per-factor breakdown for explainability.
    """
    if side.upper() != "SHORT" or spot <= 0 or strike <= 0:
        return {"probability": 0.0, "reasons": ["only short positions have assignment risk"]}
    if kind == "call":
        itm = max(0.0, (spot - strike) / strike)
    else:
        itm = max(0.0, (strike - spot) / strike)

    # ITM factor: 0 when OTM, asymptotic to 1 deeper in
    itm_factor = 1.0 - math.exp(-12.0 * itm)
    # DTE factor: 1 at expiry, decays toward 0 over 45 days
    dte_factor = math.exp(-max(0, dte) / 15.0) if dte >= 0 else 1.0
    # Dividend factor: only relevant for short calls ITM near ex-div
    div_factor = 1.0
    reasons: List[str] = []
    if kind == "call" and itm > 0 and ex_div_days is not None and 0 <= ex_div_days <= 5:
        div_factor = 1.5
        reasons.append(f"ITM short call within {ex_div_days}d of ex-dividend — early-exercise risk")

    probability = min(0.99, itm_factor * dte_factor * div_factor)
    if itm == 0:
        reasons.append("OTM — assignment unlikely")
    elif dte <= 1 and itm > 0:
        reasons.append("expiry imminent — ITM short almost certainly assigned")
    elif dte <= 7 and itm > 0.02:
        reasons.append("near expiry + ITM — elevated early-assignment risk")

    return {
        "probability": round(probability, 4),
        "itm_pct": round(itm, 4),
        "dte_factor": round(dte_factor, 4),
        "itm_factor": round(itm_factor, 4),
        "div_factor": round(div_factor, 4),
        "reasons": reasons,
    }
