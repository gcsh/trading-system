"""MITS-5 — winner-profile builder.

`build_winner_profile(pattern, regime, ...)` walks the
`market_observations + market_outcomes` corpus, filters to winners of
the (pattern, regime) cohort, and aggregates trajectory stats into a
`WinnerProfile`.

Idempotent and cheap (single SELECT + in-memory aggregation). Cached
process-locally keyed on (pattern, regime, horizon, ticker) — TTL 1h so
the profile picks up nightly aggregator updates without restarting the
bot.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.thesis.winner_profile import (
    KNOWN_TRAITS,
    TRAIT_HELD_FLAG_LOW,
    TRAIT_HELD_VWAP,
    TRAIT_HIT_PEAK_EARLY,
    TRAIT_IV_COMPRESSION,
    TRAIT_IV_EXPANSION,
    WinnerProfile,
)
from backend.db import session_scope
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome

logger = logging.getLogger(__name__)


# (pattern, regime, horizon, ticker_or_none) → (built_at_epoch, profile)
_PROFILE_CACHE: Dict[Tuple[str, str, str, Optional[str]],
                         Tuple[float, WinnerProfile]] = {}
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE_TTL_SEC = 3600.0  # 1h — picks up nightly recompute_cells.


_HORIZON_TO_MINUTES = {
    "5min": 5.0,
    "30min": 30.0,
    "60min": 60.0,
    "1d": 60.0 * 6.5,   # one RTH session
    "5d": 60.0 * 6.5 * 5,
    "20d": 60.0 * 6.5 * 20,
}


def _decode_features(raw: Any) -> Dict[str, Any]:
    """Observation.features is stored as a JSON string."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _confidence_from_n(n: int) -> float:
    """Saturating: 100 winners → confidence ~= 0.9, 30 → ~= 0.6, 5 → ~= 0.20."""
    if n <= 0:
        return 0.0
    # Asymptotic: n / (n + half_n_saturation). 30 → 0.6 means half_n = 20.
    return min(1.0, n / float(n + 20))


def build_winner_profile(
    pattern: str,
    regime: Optional[str] = None,
    *,
    horizon: str = "1d",
    ticker: Optional[str] = None,
    use_cache: bool = True,
) -> WinnerProfile:
    """Aggregate historical winners into a WinnerProfile.

    `pattern` MUST be a detector slug (bull_flag, breakout, ...).
    `regime` is optional — when None, every regime is included.
    `ticker` is optional — when given, restricts to one symbol's winners.

    Returns a fresh, possibly low-confidence profile (sample_size==0)
    when no winners match — callers should test `profile.is_trustworthy`.
    """
    if not pattern:
        return WinnerProfile(pattern="", regime=regime or "")
    pattern = str(pattern).strip()
    regime_key = (regime or "").strip()
    cache_key = (pattern, regime_key, horizon, ticker)
    now = time.monotonic()
    if use_cache:
        with _PROFILE_CACHE_LOCK:
            hit = _PROFILE_CACHE.get(cache_key)
            if hit is not None and (now - hit[0]) < _PROFILE_CACHE_TTL_SEC:
                return hit[1]

    rows: List[Dict[str, Any]] = []
    try:
        with session_scope() as s:
            q = (
                select(
                    MarketObservation.ticker,
                    MarketObservation.pattern,
                    MarketObservation.regime,
                    MarketObservation.features,
                    MarketObservation.timestamp,
                    MarketObservation.spot,
                    MarketOutcome.horizon,
                    MarketOutcome.return_pct,
                    MarketOutcome.was_winner,
                )
                .join(MarketOutcome,
                          MarketOutcome.observation_id == MarketObservation.id)
                .where(MarketObservation.pattern == pattern)
                .where(MarketOutcome.horizon == horizon)
            )
            if regime_key:
                q = q.where(MarketObservation.regime == regime_key)
            if ticker:
                q = q.where(MarketObservation.ticker == ticker.upper().strip())
            for row in s.execute(q).all():
                (tkr, pat, reg, feats, ts, spot, hor, ret, won) = row
                rows.append({
                    "ticker": tkr,
                    "pattern": pat,
                    "regime": reg,
                    "features": _decode_features(feats),
                    "timestamp": ts,
                    "spot": float(spot) if spot is not None else None,
                    "horizon": hor,
                    "return_pct": float(ret) if ret is not None else 0.0,
                    "was_winner": bool(won) if won is not None else False,
                })
    except Exception:
        logger.exception("winner-profile fetch failed for %s/%s",
                              pattern, regime_key)
        return WinnerProfile(pattern=pattern, regime=regime_key)

    winners = [r for r in rows if r["was_winner"]]
    n = len(winners)
    if n == 0:
        profile = WinnerProfile(pattern=pattern, regime=regime_key,
                                          sample_size=0)
        with _PROFILE_CACHE_LOCK:
            _PROFILE_CACHE[cache_key] = (now, profile)
        return profile

    # Trait frequencies — fraction of winners exhibiting each trait.
    # We map known trait names to feature checks that detectors set on
    # the observation. Detector authors are free to expose new keys;
    # unknown traits stay at 0.0 frequency.
    trait_counts: Dict[str, int] = {t: 0 for t in KNOWN_TRAITS}
    for w in winners:
        f = w["features"] or {}
        if f.get("price_vs_vwap") and float(f["price_vs_vwap"]) > 0:
            trait_counts[TRAIT_HELD_VWAP] += 1
        if f.get("price_vs_flag_low") and float(f["price_vs_flag_low"]) > 0:
            trait_counts[TRAIT_HELD_FLAG_LOW] += 1
        if f.get("price_vs_bos_pivot") and float(f["price_vs_bos_pivot"]) > 0:
            trait_counts["held_bos_pivot"] += 1
        if f.get("iv_jump_pct") and float(f["iv_jump_pct"]) > 0:
            trait_counts[TRAIT_IV_EXPANSION] += 1
        if f.get("iv_drop_pct") and float(f["iv_drop_pct"]) > 0:
            trait_counts[TRAIT_IV_COMPRESSION] += 1
        # "Hit peak early" is inferred when avg_hold_minutes is encoded
        # on the observation. Most detectors don't populate this today;
        # the trait stays at 0.0 frequency until they do, which means
        # the calculator gives it zero weight (correctly).

    trait_frequencies = {t: (trait_counts.get(t, 0) / n) for t in KNOWN_TRAITS}
    common_traits = [t for t, freq in trait_frequencies.items() if freq > 0.5]
    common_traits.sort()

    # Average minutes-to-peak. We don't have per-bar trajectories in
    # market_outcomes (only horizon return), so we proxy by mapping
    # horizon → minutes. Refines as outcome_linker captures finer
    # trajectory data downstream.
    horizon_minutes = _HORIZON_TO_MINUTES.get(horizon, 0.0)
    # Assume winners take ~60% of the horizon to peak on average.
    avg_minutes_to_peak = horizon_minutes * 0.6

    # Average max drawdown during hold — we don't have intrabar in the
    # corpus yet, so approximate from return_pct. A weak proxy: assume
    # winners endured ~25% of their realized move as max drawdown
    # before completing. Captured as a NEGATIVE number per docstring.
    avg_return = sum(w["return_pct"] for w in winners) / n
    avg_max_drawdown = -0.25 * abs(avg_return)

    confidence = _confidence_from_n(n)

    profile = WinnerProfile(
        pattern=pattern,
        regime=regime_key,
        sample_size=n,
        avg_minutes_to_peak=round(avg_minutes_to_peak, 2),
        avg_max_drawdown_during_hold=round(avg_max_drawdown, 4),
        common_traits=common_traits,
        trait_frequencies={k: round(v, 3) for k, v in trait_frequencies.items()},
        confidence=round(confidence, 3),
    )
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE[cache_key] = (now, profile)
    return profile


def clear_profile_cache() -> None:
    """Drop the entire process-local profile cache.

    Called from tests; also useful after a manual `recompute_cells` if
    the operator wants the next bot cycle to read the fresh aggregate
    without waiting an hour.
    """
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE.clear()
