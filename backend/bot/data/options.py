"""Options-chain / IV / earnings data — multi-source with fallback.

Provider selection is controlled by the ``OPTIONS_PROVIDER`` env var (or
``TUNABLES.options_provider``):

  thetadata_first  (default during cutover) — try ThetaData v3 terminal
                   first, fall back to yfinance/cboe on None. This is the
                   path we want once the ThetaData terminal is stable.
  thetadata        — ThetaData only, no fallback (use after cutover trust
                   period).
  yfinance         — legacy path, ignores ThetaData entirely. Useful for
                   the side-by-side diff log + tests.

Providers in priority order:

  ThetaData v3 (paid, OPRA NBBO)  — primary. ATM IV computed locally from
                                    straddle via Brenner-Subrahmanyam since
                                    Standard tier doesn't expose IV/greeks
                                    endpoints directly.
  yfinance option chains          — fallback. Includes impliedVolatility per
                                    contract but ships stale mids during
                                    after-hours / illiquid names (this is
                                    why we moved to ThetaData on 2026-06-02).
  Cboe delayed options JSON       — secondary fallback. Real IV but coarse
                                    expiry granularity.

Earnings date: yfinance calendar (no ThetaData equivalent).

Honest limit on iv_rank: we now have 8 yrs of ThetaData history available
(see ``data/iv_history.py`` — Phase 1.3) but the percentile-rank backfill
ships separately. Until that's wired, ``iv_rank`` is still flagged
``iv_rank_estimated``.
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import date, datetime
from typing import Dict, Optional

import numpy as np

from backend.config import TUNABLES

logger = logging.getLogger(__name__)

_CACHE: Dict[str, tuple] = {}
_TTL = TUNABLES.options_cache_ttl


# ── P1.5 in-process aggregates (data quality observability) ────────────
#
# Provider hits + sanity-flag counts since process start. Operator reads
# this via /system/data-quality to confirm ThetaData is the dominant
# source and to see which sanity flags fire most often. Resets on bot
# restart (intentional — process-lifetime granularity is what matters
# for "is the current build healthy" decisions).


import threading as _threading
_COUNTER_LOCK = _threading.Lock()
_PROVIDER_HITS: Dict[str, int] = {}
_SANITY_FLAG_HITS: Dict[str, int] = {}
_DQ_SESSION_START = time.time()


def _record_provider_hit(name: str) -> None:
    with _COUNTER_LOCK:
        _PROVIDER_HITS[name] = _PROVIDER_HITS.get(name, 0) + 1


def _normalize_flag(flag: str) -> str:
    """Bucket the dynamic flag strings ("stale_900s", "wide_spread_40.00%")
    into stable keys ("stale", "wide_spread") so per-key counters mean
    something across a long session."""
    if flag.startswith("stale_"):
        return "stale"
    if flag.startswith("wide_spread_"):
        return "wide_spread"
    if flag.startswith("warn_spread_"):
        return "warn_spread"
    if flag.startswith("parity_violation"):
        return "parity_violation"
    if flag.startswith("smile_outlier"):
        return "smile_outlier"
    if flag.startswith("intraday_iv_jump"):
        return "intraday_iv_jump"
    return flag


def _record_sanity_flags(flags: Optional[list]) -> None:
    if not flags:
        return
    with _COUNTER_LOCK:
        for f in flags:
            key = _normalize_flag(f)
            _SANITY_FLAG_HITS[key] = _SANITY_FLAG_HITS.get(key, 0) + 1


def _record_thetadata_rejection(flags: Optional[list]) -> None:
    """Called from ``_atm_from_thetadata`` when sanity rejects. Increments
    both ``thetadata_rejected`` provider counter AND the per-flag counts
    so the observability page can distinguish 'how often did ThetaData
    give us trash' from 'how often did the fallback chain succeed
    overall.'"""
    with _COUNTER_LOCK:
        _PROVIDER_HITS["thetadata_rejected"] = (
            _PROVIDER_HITS.get("thetadata_rejected", 0) + 1
        )
        for f in (flags or []):
            key = _normalize_flag(f)
            _SANITY_FLAG_HITS[key] = _SANITY_FLAG_HITS.get(key, 0) + 1


def get_data_quality_aggregates() -> dict:
    """Snapshot of provider + sanity counters. Public so the
    /system/data-quality route can serve it cheaply."""
    with _COUNTER_LOCK:
        return {
            "session_start_unix": _DQ_SESSION_START,
            "uptime_seconds": round(time.time() - _DQ_SESSION_START, 1),
            "providers": dict(_PROVIDER_HITS),
            "sanity_flags": dict(_SANITY_FLAG_HITS),
        }


def reset_data_quality_aggregates() -> None:
    """Operator-acknowledge: wipe the counters. Reset is occasionally
    useful when investigating 'has the new build cleaned things up.'"""
    global _DQ_SESSION_START
    with _COUNTER_LOCK:
        _PROVIDER_HITS.clear()
        _SANITY_FLAG_HITS.clear()
        _DQ_SESSION_START = time.time()


def _selected_provider() -> str:
    """Resolve provider preference. Env var wins so we can flip without
    a config redeploy during the cutover."""
    env = os.environ.get("OPTIONS_PROVIDER")
    if env:
        return env.strip().lower()
    cfg = getattr(TUNABLES, "options_provider", None)
    return (cfg or "thetadata_first").strip().lower()


def snap_strike(price: float, kind: str = "call", moneyness: float = 0.0) -> float:
    """Snap a raw target price to a realistic chain-strike increment.

    Real option chains ladder strikes at fixed intervals that depend on the
    underlying's price level — e.g. NVDA at $215 has strikes at 210, 212.50,
    215, 217.50, 220. Strategies were writing the *stock price* as the strike
    (``round(price, 2)`` → $215.35) which is never an actual listed strike.

    Args:
        price: spot price of the underlying.
        kind:  "call" (round to nearest) | "put" (round to nearest) — placeholder
               for future OTM-bias logic; both currently use the nearest interval.
        moneyness: optional offset BEFORE snapping. Positive shifts above spot,
               negative below. E.g. ``moneyness=-0.05`` picks a 5% OTM put.

    The intervals are config-driven (``TUNABLES.strike_intervals``):
        price < $25     → $0.50
        $25 ≤ p < $100  → $1
        $100 ≤ p < $500 → $5
        p ≥ $500        → $10
    """
    if price is None or price <= 0:
        return 0.0
    target = float(price) * (1.0 + float(moneyness or 0.0))
    interval = _strike_interval(target)
    snapped = round(target / interval) * interval
    # Avoid floating-point noise like 217.4999999999.
    return round(snapped, 2)


def _strike_interval(price: float) -> float:
    """Lookup the listed-strike spacing for an underlying at ``price``."""
    bands = getattr(TUNABLES, "strike_intervals", None) or [
        (25.0, 0.50), (100.0, 1.0), (500.0, 5.0), (float("inf"), 10.0),
    ]
    for upper, step in bands:
        if price < float(upper):
            return float(step)
    return float(bands[-1][1])


# Brenner-Subrahmanyam ATM straddle constant: straddle ≈ k·S·σ·√T where k = √(2/π).
_BS_STRADDLE_K = math.sqrt(2.0 / math.pi)  # ≈ 0.7979


# ── chain-aware selection (P1.4) ──────────────────────────────────────


def chain_expiry(ticker: str, *, target_dte: int = 30,
                    min_dte: int = 1) -> Optional[date]:
    """Pick the listed expiration closest to ``target_dte`` for ``ticker``.

    Uses the ThetaData terminal when reachable; returns None on failure so
    callers can fall back to arithmetic ``date.today() + timedelta(days=N)``.
    """
    try:
        from backend.bot.data.thetadata import get_client
        return get_client().nearest_expiration(
            ticker, target_dte=target_dte, min_dte=min_dte,
        )
    except Exception as exc:
        logger.debug("chain_expiry fallback for %s: %s", ticker, exc)
        return None


def chain_strike_with_drift(ticker: str, spot: float, kind: str = "call", *,
                                  moneyness: float = 0.0,
                                  expiry: Optional[date] = None,
                                  target_dte: int = 30,
                                  max_spread_pct: float = 0.10,
                                  min_size: int = 1,
                                  target_delta: Optional[float] = None,
                                  delta_tolerance: float = 0.07,
                                  ) -> "tuple[float, dict]":
    """Wrapper over :func:`chain_strike` that ALSO returns drift metadata
    describing how far the picked strike landed from the strategy's
    intent (P1.4-FU2).

    The drift dict has shape::

        {
            "target_strike": float,         # spot · (1 + moneyness)
            "target_moneyness": float,      # what the strategy asked for
            "moneyness_actual": float,      # what we actually picked
            "strike_drift_pct": float,      # |actual - intended|
        }

    Strategies pack the dict into ``metadata`` so the chairman + risk
    sizing can see "wanted 5% OTM, got 1.6% OTM because the closer
    strike had no quote" rather than treating the executed trade as if
    intent was met.
    """
    strike = chain_strike(
        ticker, spot, kind,
        moneyness=moneyness, expiry=expiry, target_dte=target_dte,
        max_spread_pct=max_spread_pct, min_size=min_size,
        target_delta=target_delta, delta_tolerance=delta_tolerance,
    )
    target_strike = float(spot) * (1.0 + float(moneyness or 0.0)) if spot else 0.0
    actual_money = (strike / spot - 1.0) if spot and spot > 0 else 0.0
    drift = abs(actual_money - float(moneyness or 0.0))
    drift_meta = {
        "target_strike": round(target_strike, 2),
        "target_moneyness": round(float(moneyness or 0.0), 4),
        "moneyness_actual": round(actual_money, 4),
        "strike_drift_pct": round(drift, 4),
        "target_delta": target_delta,
    }
    return strike, drift_meta


def resolve_expiry_dte(ticker: str, target_dte: int = 30,
                            *, min_dte: int = 1) -> tuple[Optional[str], int]:
    """Return ``(expiry_iso_or_none, dte)`` — use the listed expiration
    closest to ``target_dte`` when ThetaData is reachable, otherwise
    fall back to ``(None, target_dte)`` so the caller can still ship a
    Signal with the arithmetic default DTE.

    Why this exists: strategies write ``Signal(dte=30, metadata={"dte":
    30})`` for the conventional 30-day options trade, but no expiration
    is actually listed at exactly 30 days. The engine's
    ``_default_expiration`` then synthesizes a date that the chain may
    not have, leading to "no quote" failures at order time. This helper
    plumbs the *real* nearest expiry through.
    """
    expiry = chain_expiry(ticker, target_dte=target_dte, min_dte=min_dte)
    if expiry is None:
        return (None, target_dte)
    actual_dte = max(1, (expiry - date.today()).days)
    return (expiry.isoformat(), actual_dte)


def chain_strike(ticker: str, spot: float, kind: str = "call", *,
                    moneyness: float = 0.0,
                    expiry: Optional[date] = None,
                    target_dte: int = 30,
                    max_spread_pct: float = 0.10,
                    min_size: int = 1,
                    target_delta: Optional[float] = None,
                    delta_tolerance: float = 0.07) -> float:
    """Pick a listed strike from the actual chain.

    Improvement over :func:`snap_strike` (which is pure arithmetic):

      1. The returned strike is one that is **actually listed** for the
         chosen expiration — eliminates "we picked $215.35 but the grid
         is $212.50 / $215 / $217.50."
      2. Among candidate strikes, prefers ones with both bid and ask
         populated and ``(ask-bid)/mid <= max_spread_pct`` — eliminates
         the "we picked a strike with no quote" failure mode.
      3. **Delta-band selection (P1.4-FU3)** — if ``target_delta`` is
         provided, compute each candidate strike's IV (via bisection on
         the BS mid-price) → delta, and pick the strike whose absolute
         delta lands closest to ``target_delta`` within
         ``delta_tolerance``. Institutional convention: CSPs target
         delta ~0.30, ATM trades ~0.50. Falls back to moneyness-based
         selection when no strike fits the band (rare) or when the
         delta math fails (illiquid quotes).
      4. Otherwise picks the listed strike closest to the arithmetic
         target ``spot * (1 + moneyness)`` — preserves the existing
         strategy intent on which OTM band to sit in.

    Falls back to :func:`snap_strike` when:
      - terminal unreachable (returns the arithmetic best guess),
      - no expirations found (same),
      - chain returns no quotes meeting even the relaxed liquidity gate.

    The fallback is logged at WARNING so we know when we're degraded.
    """
    if not spot or spot <= 0:
        return 0.0
    target_price = float(spot) * (1.0 + float(moneyness or 0.0))
    right = "CALL" if str(kind).lower().startswith("c") else "PUT"

    try:
        from backend.bot.data.thetadata import get_client
        client = get_client()
        if expiry is None:
            expiry = client.nearest_expiration(ticker, target_dte=target_dte)
            if expiry is None:
                logger.warning("chain_strike: no listed expiry for %s, falling back", ticker)
                return snap_strike(spot, kind, moneyness)

        chain = client.chain_snapshot(ticker, expiry)
        candidates = [q for q in chain if q.right == right]
        if not candidates:
            logger.warning("chain_strike: empty %s chain for %s/%s, falling back",
                              right, ticker, expiry)
            return snap_strike(spot, kind, moneyness)

        # Tightest liquidity gate first — both sides quoted, sizes >= min,
        # spread within bound. If none qualify, relax progressively so we
        # never reject ALL strikes (better an imperfect listed strike
        # than a fabricated one).
        def is_strict(q):
            return (
                q.bid > 0 and q.ask > 0
                and q.bid_size >= min_size and q.ask_size >= min_size
                and (q.spread_pct or 1.0) <= max_spread_pct
            )

        def is_quoted(q):
            return q.bid > 0 and q.ask > 0

        pool = (
            [q for q in candidates if is_strict(q)]
            or [q for q in candidates if is_quoted(q)]
            or candidates
        )

        # Delta-band selection (P1.4-FU3). Try to land near target_delta;
        # if no strike fits the band or the IV solve fails, fall through
        # to moneyness-based selection.
        if target_delta is not None and pool:
            try:
                from backend.bot.greeks import compute_greeks, implied_vol
                T = max(1, (expiry - date.today()).days) / 365.0
                want = abs(float(target_delta))
                kind_arg = "call" if right == "CALL" else "put"
                delta_candidates: list = []
                for q in pool:
                    mid = q.mid
                    if mid <= 0:
                        continue
                    iv = implied_vol(mid, spot, q.strike, T, kind=kind_arg)
                    if iv is None or iv <= 0:
                        continue
                    g = compute_greeks(spot, q.strike, T, iv, kind=kind_arg)
                    delta_candidates.append((q.strike, abs(g.delta)))
                if delta_candidates:
                    in_band = [(s, d) for s, d in delta_candidates
                                  if abs(d - want) <= delta_tolerance]
                    if in_band:
                        best_s, _ = min(in_band, key=lambda sd: abs(sd[1] - want))
                        return float(best_s)
                    # Nothing inside tolerance — fall through to moneyness
                    # rather than picking a far-off-band strike that would
                    # surprise the strategy.
            except Exception as exc:
                logger.debug("delta-band selection failed for %s: %s",
                                ticker, exc)

        best = min(pool, key=lambda q: abs(q.strike - target_price))
        return float(best.strike)
    except Exception as exc:
        logger.warning("chain_strike fallback for %s (%s): %s", ticker, kind, exc)
        return snap_strike(spot, kind, moneyness)


def _atm_from_thetadata(ticker: str, spot: float,
                            target_dte: int = 30) -> Optional[dict]:
    """ATM snapshot via the local ThetaData v3 terminal.

    Strategy:
      1. Pick the listed expiration closest to ``target_dte`` (skipping 0DTE).
      2. Find the listed strike closest to ``spot``.
      3. Quote both the ATM call and ATM put.
      4. Straddle mid → implied_move = straddle / spot.
      5. Brenner-Subrahmanyam inversion → iv_atm (we don't trust vendor IV
         because Standard tier doesn't expose it; computing locally also
         means losses can't be blamed on "vendor IV was wrong").

    Returns ``None`` (so callers can fall back) on any of:
      • terminal unreachable
      • no expiration meeting min DTE
      • no strikes / quotes returned
      • zero straddle (illiquid contract — refuse to fabricate IV)
    """
    try:
        from backend.bot.data.thetadata import (
            get_client, check_quote_sanity, check_parity_sanity,
            check_smile_sanity, check_intraday_iv_sanity,
        )
    except Exception:
        return None
    if not spot or spot <= 0:
        return None
    client = get_client()
    expiration = client.nearest_expiration(ticker, target_dte=target_dte)
    if expiration is None:
        return None

    # Fetch the full chain once (TTL-cached) so the smile check + parity
    # check + ATM extraction all share the same fetch.
    chain = client.chain_snapshot(ticker, expiration)
    if not chain:
        return None
    # Find the listed strike closest to spot — call and put at that strike.
    strikes_present = sorted({q.strike for q in chain if q.strike > 0})
    if not strikes_present:
        return None
    strike = min(strikes_present, key=lambda s: abs(s - spot))
    call_q = next((q for q in chain if q.strike == strike and q.right == "CALL"), None)
    put_q = next((q for q in chain if q.strike == strike and q.right == "PUT"), None)
    if call_q is None or put_q is None:
        return None

    # Sanity gate (P1.2). One market_open lookup, applied to both legs.
    try:
        from backend.bot.calendar import is_us_market_open
        market_open = is_us_market_open()
    except Exception:
        market_open = False
    call_sanity = check_quote_sanity(call_q, market_open=market_open)
    put_sanity = check_quote_sanity(put_q, market_open=market_open)
    if not (call_sanity.passed and put_sanity.passed):
        flags = list(set(call_sanity.flags) | set(put_sanity.flags))
        # Info-level: the integrity layer is doing its job, not a system
        # fault. Counts are still aggregated via _record_thetadata_rejection
        # for the data-quality dashboard.
        logger.info(
            "thetadata sanity rejected %s %s @ %s — flags=%s",
            ticker, expiration, strike, flags,
        )
        _record_thetadata_rejection(flags)
        return None

    # Put-call parity check (P1.2-FU1). One of the strongest data-integrity
    # signals — when C-P drifts from the no-arb value by more than tolerance,
    # ONE of the legs is wrong. Hard reject; let the caller fall back.
    rf = float(getattr(TUNABLES, "risk_free_rate", 0.045))
    div_yield = _dividend_yield(ticker) or 0.0
    parity = check_parity_sanity(
        call_q, put_q,
        spot=spot, expiration=expiration,
        risk_free_rate=rf, dividend_yield=div_yield,
    )
    if not parity.passed:
        flags = [parity.flag] if parity.flag else ["parity_violation"]
        logger.info(
            "thetadata parity rejected %s %s @ %s — dev=%.2f tol=%.2f",
            ticker, expiration, strike,
            parity.deviation or 0.0, parity.tolerance,
        )
        _record_thetadata_rejection(flags)
        return None

    # IV smile sanity (P1.2-FU2). One-call (or one-put) sample of N
    # strikes near ATM, outlier detection vs median. Catches "one strike
    # has wildly wrong mid → unreasonable IV" failures that parity
    # doesn't catch (parity only checks ONE strike's C+P pair).
    smile_check = check_smile_sanity(
        chain, spot=spot, expiration=expiration,
        risk_free_rate=rf, kind="call",
    )
    if not smile_check.passed:
        flags = [smile_check.flag] if smile_check.flag else ["smile_outlier"]
        logger.info(
            "thetadata smile rejected %s %s — flag=%s median_iv=%s",
            ticker, expiration, smile_check.flag,
            f"{smile_check.median_iv:.4f}" if smile_check.median_iv else "n/a",
        )
        _record_thetadata_rejection(flags)
        return None
    # Confidence drops to "medium" if either leg flagged a soft warning.
    confidences = (call_sanity.confidence, put_sanity.confidence)
    if "low" in confidences:
        data_confidence = "low"
    elif "medium" in confidences:
        data_confidence = "medium"
    else:
        data_confidence = "high"
    sanity_flags = sorted(set(call_sanity.flags) | set(put_sanity.flags))

    straddle = (call_q.mid or 0.0) + (put_q.mid or 0.0)
    if straddle <= 0:
        return None
    dte = max(1, (expiration - date.today()).days)
    T = dte / 365.0
    implied_move = straddle / spot
    iv_atm: Optional[float] = None
    if T > 0 and spot > 0:
        iv_atm = round(straddle / (_BS_STRADDLE_K * spot * math.sqrt(T)), 4)

    # Intra-tick IV consistency (P1.2-FU3). Compare iv_atm to the rolling
    # trailing distribution; reject when z-score exceeds threshold.
    if iv_atm is not None and iv_atm > 0:
        intraday = check_intraday_iv_sanity(ticker, iv_atm)
        if not intraday.passed:
            flag = intraday.flag or "intraday_iv_outlier"
            logger.info(
                "thetadata intraday IV rejected %s — flag=%s z=%.1f n=%d",
                ticker, flag, intraday.z_score or 0.0, intraday.sample_count,
            )
            _record_thetadata_rejection([flag])
            return None
        # Carry the intraday-check flags into the surface confidence so
        # downstream knows the corpus is still small (warm-up).
        if intraday.sample_count < 5:
            sanity_flags = sorted(set(sanity_flags) | {"intraday_iv_warmup"})

    return {
        "iv_atm": iv_atm,
        "implied_move": round(implied_move, 4),
        "dte": dte,
        "expiry": expiration.isoformat(),
        "source": "thetadata",
        "data_confidence": data_confidence,
        "sanity_flags": sanity_flags,
    }


def _atm_from_yfinance(ticker: str, spot: float) -> Optional[dict]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    exps = list(t.options or [])
    if not exps:
        return None
    expiry = exps[0]
    chain = t.option_chain(expiry)
    # yfinance returns None for chain.calls / chain.puts when the
    # provider degrades (thin names, after-hours, transient errors).
    # Guard explicitly so `.copy()` doesn't fall through to the broad
    # except handler that spams warnings every cycle.
    if chain is None or chain.calls is None or chain.puts is None:
        return None
    calls, puts = chain.calls.copy(), chain.puts.copy()
    if calls.empty or puts.empty or not spot:
        return None
    calls["d"] = (calls["strike"] - spot).abs()
    puts["d"] = (puts["strike"] - spot).abs()
    c = calls.sort_values("d").iloc[0]
    p = puts.sort_values("d").iloc[0]
    ivs = [v for v in (c.get("impliedVolatility"), p.get("impliedVolatility")) if v and v == v and v < 5]
    iv_atm = float(np.mean(ivs)) if ivs else None
    dte = max(1, (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days)

    def mid(row):
        b, a = float(row.get("bid") or 0), float(row.get("ask") or 0)
        return (b + a) / 2 if (b > 0 and a > 0) else float(row.get("lastPrice") or 0)

    straddle = mid(c) + mid(p)
    if straddle > 0:
        implied_move = straddle / spot
    elif iv_atm:
        implied_move = iv_atm * math.sqrt(dte / 365.0)
    else:
        return None
    return {"iv_atm": round(iv_atm, 4) if iv_atm else None,
            "implied_move": round(implied_move, 4), "dte": dte, "expiry": expiry, "source": "yfinance"}


def _atm_from_cboe(ticker: str, spot: float) -> Optional[dict]:
    from curl_cffi import requests as creq

    r = creq.get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker.upper()}.json",
                 impersonate="chrome", timeout=15)
    if r.status_code != 200:
        return None
    opts = (r.json().get("data") or {}).get("options") or []
    best = None
    for o in opts:
        sym = o.get("option", "")
        iv = o.get("iv")
        if not iv or iv <= 0:
            continue
        # contractSymbol like AAPL260529C00312500 → strike = last 8 digits / 1000
        try:
            strike = int(sym[-8:]) / 1000.0
        except Exception:
            continue
        dist = abs(strike - spot)
        if best is None or dist < best[0]:
            best = (dist, float(iv))
    if not best or not spot:
        return None
    iv_atm = round(best[1], 4)
    return {"iv_atm": iv_atm, "implied_move": round(iv_atm * math.sqrt(30 / 365.0), 4),
            "dte": 30, "expiry": None, "source": "cboe"}


# Dividend-yield cache for the put-call parity check (P1.2-FU1). yfinance's
# ``Ticker.info`` is slow (~500 ms) — caching per session means it fires
# once per ticker per process lifetime. Yields don't move daily, so the
# infinite TTL is fine.
_DIV_YIELD_CACHE: Dict[str, float] = {}


def _dividend_yield(ticker: str) -> Optional[float]:
    """Annual dividend yield as a decimal (0.014 = 1.4%). yfinance returns
    None for non-dividend payers — we map to 0.0. Cached forever in-process."""
    key = ticker.upper()
    if key in _DIV_YIELD_CACHE:
        return _DIV_YIELD_CACHE[key]
    try:
        import yfinance as yf
        info = yf.Ticker(key).info or {}
        # yfinance shifted the field name between versions; check both.
        raw = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
        # yfinance has historically returned this as both a decimal (0.014)
        # AND a percent (1.4) depending on version — clamp to a plausible
        # decimal range.
        if raw is None:
            value = 0.0
        else:
            v = float(raw)
            # yfinance has historically returned this as both decimal
            # (0.014 = 1.4%) and percent-formatted (1.4). Anything above
            # 0.10 (10%) is almost certainly a percent and gets divided.
            # Anything above 25 (25 percent) we treat as garbage and zero.
            if v > 25:
                value = 0.0
            elif v > 0.10:
                value = v / 100.0
            else:
                value = v
    except Exception:
        value = 0.0
    _DIV_YIELD_CACHE[key] = value
    return value


def _earnings(ticker: str) -> tuple:
    """(earnings_days, earnings_today) — best effort from yfinance calendar."""
    try:
        import yfinance as yf

        cal = yf.Ticker(ticker).calendar
        ed = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            ed = (v[0] if isinstance(v, (list, tuple)) and v else v)
        if ed is None:
            return 999, False
        d = ed if isinstance(ed, date) else datetime.fromisoformat(str(ed)).date()
        days = (d - date.today()).days
        return (max(0, days), days == 0)
    except Exception:
        return 999, False


def _iv_rank_estimate(iv_atm: Optional[float]) -> int:
    """Map live ATM IV to a 0-100 estimate (true rank needs IV history)."""
    if not iv_atm:
        return 50
    floor, rng = TUNABLES.iv_rank_iv_floor, TUNABLES.iv_rank_iv_range
    return int(max(0, min(100, (iv_atm - floor) / rng * 100)))


def _iv_rank_with_history(ticker: str,
                                iv_atm: Optional[float]) -> tuple[int, bool]:
    """Return (rank, estimated) — prefers real percentile from
    ``iv_history`` table when we have enough samples, otherwise falls
    back to the linear estimator.

    Real percentile is computed over the last 252 trading days. Below
    ~20 samples it's noisy enough that the linear floor is more honest.
    """
    if iv_atm is None or iv_atm <= 0:
        return _iv_rank_estimate(iv_atm), True
    try:
        from backend.bot.data.iv_history import iv_percentile_rank
        result = iv_percentile_rank(ticker, float(iv_atm))
    except Exception as exc:
        logger.warning("iv_percentile_rank failed for %s: %s", ticker, exc)
        result = None
    if result is None:
        return _iv_rank_estimate(iv_atm), True
    return int(round(result.rank)), False


def _provider_chain() -> list:
    """Ordered list of (provider_name, fn) to try. Driven by ``OPTIONS_PROVIDER``.

    Each fn takes (ticker, spot) and returns the same atm dict shape or None.
    Order matters — the first non-None wins.

    Alpaca sits BETWEEN ThetaData and yfinance — it's a real broker-grade
    feed (not rate-limited like yfinance) but lacks ThetaData's depth.
    Only included when Alpaca creds are configured (else it's a no-op).
    """
    sel = _selected_provider()
    yf_fn = _atm_from_yfinance
    cboe_fn = _atm_from_cboe
    td_fn = _atm_from_thetadata
    try:
        from backend.bot.data.alpaca_options import atm_from_alpaca
        alpaca_fn = atm_from_alpaca
    except Exception:
        alpaca_fn = None
    if sel == "thetadata":
        return [("thetadata", td_fn)]
    if sel == "yfinance":
        return [("yfinance", yf_fn), ("cboe", cboe_fn)]
    # default: thetadata_first → alpaca → yfinance → cboe
    chain = [("thetadata", td_fn)]
    if alpaca_fn is not None:
        chain.append(("alpaca", alpaca_fn))
    chain.extend([("yfinance", yf_fn), ("cboe", cboe_fn)])
    return chain


# ── Per-ticker yfinance fallback rate-limiter ────────────────────────────
#
# yfinance is unauthenticated + aggressively rate-limited. When ThetaData
# rejects a quote (sanity gate), the chain falls through to yfinance for
# every cycle on every ticker — within minutes yfinance starts returning
# 429s for the whole session, and we log a warning every time. Cap each
# ticker to one yfinance attempt per cooldown window. After a 429, double
# the window (exponential backoff) up to a 1-hour ceiling.

_YF_NEXT_ATTEMPT: Dict[str, float] = {}
_YF_BACKOFF_SECONDS: Dict[str, float] = {}
_YF_BASE_COOLDOWN = 600.0    # 10 min between attempts in steady state
_YF_MAX_COOLDOWN = 3600.0    # 1 hour ceiling after repeated 429s


def _yf_should_attempt(ticker: str) -> bool:
    return time.monotonic() >= _YF_NEXT_ATTEMPT.get(ticker.upper(), 0.0)


def _yf_record_failure(ticker: str, *, transient: bool) -> None:
    key = ticker.upper()
    if transient:
        cur = _YF_BACKOFF_SECONDS.get(key, _YF_BASE_COOLDOWN)
        cur = min(_YF_MAX_COOLDOWN, cur * 2 if cur > 0 else _YF_BASE_COOLDOWN)
        _YF_BACKOFF_SECONDS[key] = cur
        _YF_NEXT_ATTEMPT[key] = time.monotonic() + cur
    else:
        _YF_NEXT_ATTEMPT[key] = time.monotonic() + _YF_BASE_COOLDOWN


def _yf_record_success(ticker: str) -> None:
    key = ticker.upper()
    _YF_BACKOFF_SECONDS.pop(key, None)
    _YF_NEXT_ATTEMPT.pop(key, None)


def _is_options_disabled(ticker: str) -> bool:
    """Per-ticker kill-switch for options pricing. Set on illiquid names
    where the chain is structurally untradeable (wide spreads every
    cycle) — we still scan the stock, just skip options entirely."""
    try:
        from backend.db import session_scope
        from backend.models.watchlist import WatchlistItem
        with session_scope() as session:
            row = session.query(WatchlistItem).filter(
                WatchlistItem.ticker == ticker.upper()
            ).first()
            return bool(row and row.options_disabled)
    except Exception:
        return False


def options_snapshot(ticker: str, spot: float) -> dict:
    """Real options inputs for the strategies; cached, never raises."""
    key = ticker.upper()
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    out = {"has_options": False, "iv_rank": 50, "implied_move": 0.07,
           "earnings_days": 999, "earnings_today": False, "iv_rank_estimated": True,
           # P1.2 sanity defaults — "unknown" when no provider succeeded, so
           # the chairman/risk layer can distinguish "we don't know" from
           # an actual high-confidence assertion.
           "data_confidence": "unknown", "sanity_flags": []}

    # Per-ticker options kill-switch. WULF and similar illiquid names go
    # here so we don't burn cycles asking ThetaData to reject the chain
    # every 30s. Stock signals still work — just no options pipeline.
    if _is_options_disabled(ticker):
        out["sanity_flags"] = ["options_disabled_per_watchlist"]
        _CACHE[key] = (now, out)
        return out

    try:
        atm = None
        chosen_provider = "none"
        for name, fn in _provider_chain():
            if name == "yfinance" and not _yf_should_attempt(ticker):
                # Still in backoff window from a recent 429 — skip silently.
                continue
            try:
                atm = fn(ticker, spot)
                if name == "yfinance" and atm is not None:
                    _yf_record_success(ticker)
            except Exception as exc:
                msg = str(exc)
                transient = (
                    "Too Many Requests" in msg
                    or "Rate limited" in msg
                    or "429" in msg
                )
                if name == "yfinance":
                    _yf_record_failure(ticker, transient=transient)
                    # Demote rate-limit noise to debug; only log on the
                    # FIRST 429 per ticker per session so the operator
                    # knows it happened without log-spam.
                    if transient:
                        logger.debug(
                            "yfinance options 429 for %s — backing off",
                            ticker,
                        )
                    else:
                        logger.warning(
                            "%s options failed for %s: %s",
                            name, ticker, exc,
                        )
                else:
                    logger.warning(
                        "%s options failed for %s: %s",
                        name, ticker, exc,
                    )
                atm = None
            if atm is not None:
                chosen_provider = name
                break
        _record_provider_hit(chosen_provider)
        if atm:
            _record_sanity_flags(atm.get("sanity_flags"))
            iv_rank_int, estimated = _iv_rank_with_history(ticker, atm["iv_atm"])
            out.update(has_options=True, iv_atm=atm["iv_atm"], implied_move=atm["implied_move"],
                       iv_rank=iv_rank_int, options_source=atm["source"],
                       option_expiry=atm["expiry"], iv_rank_estimated=estimated)
            # Carry sanity verdict through. yfinance/cboe paths don't
            # produce these — they default to "medium" since we trust
            # them less than sanity-verified ThetaData but they're not
            # outright "unknown" either.
            out["data_confidence"] = atm.get("data_confidence", "medium")
            out["sanity_flags"] = atm.get("sanity_flags") or []
            # Live capture: record today's IV so the corpus grows whether
            # or not a backfill has run. Idempotent on (ticker, date).
            try:
                if atm["iv_atm"] is not None and atm["iv_atm"] > 0:
                    from backend.bot.data.iv_history import record_today
                    exp_str = atm.get("expiry")
                    exp_d = None
                    if exp_str:
                        try:
                            exp_d = datetime.strptime(exp_str, "%Y-%m-%d").date()
                        except Exception:
                            exp_d = None
                    record_today(
                        ticker, atm["iv_atm"],
                        expiry_used=exp_d, dte_used=atm.get("dte"),
                        source="live",
                    )
            except Exception as exc:
                logger.debug("iv_history record_today failed: %s", exc)
        ed, today = _earnings(ticker)
        out["earnings_days"], out["earnings_today"] = ed, today
    except Exception as exc:
        logger.warning("options_snapshot failed for %s: %s", ticker, exc)
    _CACHE[key] = (now, out)
    return out


def premarket_volume(ticker: str) -> Optional[int]:
    """Total pre-09:30 ET volume for today via yfinance prepost bars."""
    try:
        import pandas as pd
        import yfinance as yf

        df = yf.download(ticker, period="1d", interval="5m", prepost=True, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            df.columns = [c[0] for c in df.columns]
        idx = df.index
        try:
            et = idx.tz_convert("America/New_York")
        except Exception:
            et = idx
        mins = et.hour * 60 + et.minute
        pre = df[mins.values < (9 * 60 + 30)] if hasattr(mins, "values") else df.iloc[:0]
        return int(pre["Volume"].sum()) if "Volume" in pre.columns else None
    except Exception:
        return None
