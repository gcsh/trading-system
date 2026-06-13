"""MITS Phase 12.1 — authoritative direction mapping for every detector.

Maps ``(pattern, features)`` → 'long' | 'short' | 'neutral' | None.

This module is the SINGLE SOURCE OF TRUTH for detector directionality.
Both the live detector emit path AND the backfill migration consult
``resolve_direction()`` so the legacy 228k observations get tagged
identically to anything emitted from this commit forward.

Rationale: the outcome_linker previously scored ``return_pct > 0`` as
"winner" for every observation regardless of intent. Bearish detectors
(wyckoff_distribution_phase, bear_flag, vwap_rejection, etc.) thus had
their win rates inverted — a wyckoff_distribution that correctly
predicted a 5 percent drop was scored as a loss. This module + the
direction-aware ``_compute_winner`` in outcome_linker close that bug.

The mapping below is the authoritative spec from the P12.1 plan. When
adding a new detector, register its directionality here AND set
``Observation.direction = resolve_direction(pattern, features)`` in
the emit path.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# Static (pattern → direction) for unambiguous mappings.
# Patterns whose direction depends on metadata are resolved by
# _DYNAMIC_RESOLVERS below.
STATIC_DIRECTION: Dict[str, Optional[str]] = {
    # ── Legacy candlestick / price-action ────────────────────────────
    "bull_flag": "long",
    "bear_flag": "short",
    "breakout": "long",
    "failed_breakdown": "long",   # bullish reversal
    "failed_breakout": "short",   # bearish reversal
    "pullback": "long",
    "pennant": None,              # continuation (direction follows trend)
    "consolidation": None,        # disabled (no directional bias)
    # TA-Lib singletons (already directional via name).
    "talib_doji": None,
    "talib_hammer": "long",
    "talib_inverted_hammer": "long",
    "talib_hanging_man": "short",
    "talib_shooting_star": "short",
    "talib_three_white_soldiers": "long",
    "talib_three_black_crows": "short",
    "talib_morning_star": "long",
    "talib_evening_star": "short",
    "talib_dark_cloud_cover": "short",
    "talib_piercing": "long",
    "talib_spinning_top": None,
    # ── VWAP ──────────────────────────────────────────────────────────
    "vwap_reclaim": "long",
    "vwap_rejection": "short",
    # ── Volume profile v1 ────────────────────────────────────────────
    "hvn_acceptance": None,       # support/resistance — direction depends
    # lvn_rejection direction is in features.
    # ── Options-intel ────────────────────────────────────────────────
    "gex_acceleration": None,
    "iv_compression": None,
    "iv_expansion": None,
    # ── Wyckoff ──────────────────────────────────────────────────────
    "wyckoff_accumulation_phase": "long",
    "wyckoff_distribution_phase": "short",
    "wyckoff_spring": "long",
    "wyckoff_sos": "long",
    "wyckoff_upthrust": "short",
    # ── Volume Profile v2 ────────────────────────────────────────────
    "poc_retest": None,
    "composite_value_area": None,
    # ── Macro Regime ─────────────────────────────────────────────────
    # Cross-asset signals are broad equity bias (SPX direction).
    "yield_curve_inversion": "short",       # recession signal
    "credit_spread_widening": "short",      # risk-off
    # dollar_strength_shift + composite_macro_regime resolved dynamically.
    # ── Quantitative ─────────────────────────────────────────────────
    "sector_dispersion": None,
}


# Patterns where direction lives in features. Each resolver takes the
# features dict and returns 'long' | 'short' | 'neutral' | None.
def _resolve_from_direction_field(feats: Dict[str, Any]) -> Optional[str]:
    """Common case — features carry an explicit 'direction' key.
    Accept the canonical 'bullish'/'bearish' values plus pass-through
    of already-normalised 'long'/'short'/'neutral'."""
    d = (feats or {}).get("direction")
    if not d:
        return None
    d = str(d).lower()
    if d in ("bullish", "bullish_flip", "long"):
        return "long"
    if d in ("bearish", "bearish_flip", "short"):
        return "short"
    if d == "neutral":
        return "neutral"
    return None


def _resolve_talib_engulfing(feats: Dict[str, Any]) -> Optional[str]:
    """TA-Lib engulfing returns +100 (bullish) or -100 (bearish)."""
    sign = (feats or {}).get("sign") or (feats or {}).get("talib_value")
    if sign is None:
        # Fallback to explicit kind from price_action_flags.
        kind = (feats or {}).get("kind") or (feats or {}).get("type")
        if kind:
            kind = str(kind).lower()
            if "bull" in kind:
                return "long"
            if "bear" in kind:
                return "short"
        return None
    try:
        s = float(sign)
        if s > 0:
            return "long"
        if s < 0:
            return "short"
    except Exception:
        return None
    return None


def _resolve_talib_harami(feats: Dict[str, Any]) -> Optional[str]:
    return _resolve_talib_engulfing(feats)


def _resolve_talib_marubozu(feats: Dict[str, Any]) -> Optional[str]:
    return _resolve_talib_engulfing(feats)


def _resolve_liquidity_sweep(feats: Dict[str, Any]) -> Optional[str]:
    """liquidity_sweep + stop_hunt (legacy). 'side' = 'high' → short
    (sweep above HH then revert), 'low' → long (sweep below LL)."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    side = (feats or {}).get("side") or (feats or {}).get("swept_side")
    if not side:
        return None
    side = str(side).lower()
    if "high" in side or "above" in side or side == "hh":
        return "short"
    if "low" in side or "below" in side or side == "ll":
        return "long"
    return None


def _resolve_lvn_rejection(feats: Dict[str, Any]) -> Optional[str]:
    """Rejection AWAY from a low-volume node. 'side': 'above' → bullish
    rejection upward (long), 'below' → bearish rejection (short)."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    side = (feats or {}).get("side") or (feats or {}).get("rejection_side")
    if not side:
        return None
    side = str(side).lower()
    if "below" in side or "down" in side:
        return "short"
    if "above" in side or "up" in side:
        return "long"
    return None


def _resolve_bos(feats: Dict[str, Any]) -> Optional[str]:
    """break_of_structure — continuation in trend direction.
    feats may carry 'trend' or 'direction'."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    trend = (feats or {}).get("trend") or (feats or {}).get("prior_trend")
    if trend:
        t = str(trend).lower()
        if "up" in t or "bull" in t:
            return "long"
        if "down" in t or "bear" in t:
            return "short"
    return None


def _resolve_choch(feats: Dict[str, Any]) -> Optional[str]:
    """change_of_character — REVERSAL of trend.
    If prior trend was up → bearish_flip (short). Mirror."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    prior = (feats or {}).get("prior_trend") or (feats or {}).get("trend")
    if prior:
        t = str(prior).lower()
        if "up" in t or "bull" in t:
            return "short"
        if "down" in t or "bear" in t:
            return "long"
    return None


def _resolve_premium_discount_zone(feats: Dict[str, Any]) -> Optional[str]:
    zone = (feats or {}).get("zone")
    if zone == "discount":
        return "long"
    if zone == "premium":
        return "short"
    return _resolve_from_direction_field(feats)


def _resolve_value_area_rejection(feats: Dict[str, Any]) -> Optional[str]:
    """rejection at VAH (top of value) → short.
    rejection at VAL (bottom of value) → long."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    side = (feats or {}).get("rejection_side") or (feats or {}).get("level")
    if side:
        s = str(side).lower()
        if "vah" in s or "high" in s or "upper" in s:
            return "short"
        if "val" in s or "low" in s or "lower" in s:
            return "long"
    return None


def _resolve_pead_drift(feats: Dict[str, Any]) -> Optional[str]:
    """Post-earnings drift. surprise > 0 → long, < 0 → short."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    surprise = (feats or {}).get("surprise") or (feats or {}).get("surprise_pct")
    if surprise is None:
        side = (feats or {}).get("side")
        if side:
            s = str(side).lower()
            if "pos" in s or "beat" in s or "up" in s:
                return "long"
            if "neg" in s or "miss" in s or "down" in s:
                return "short"
        return None
    try:
        v = float(surprise)
        if v > 0:
            return "long"
        if v < 0:
            return "short"
    except Exception:
        return None
    return None


def _resolve_insider_cluster(feats: Dict[str, Any]) -> Optional[str]:
    """3+ insider buys → long; 3+ insider sells → short."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    side = (feats or {}).get("side") or (feats or {}).get("cluster_kind")
    if side:
        s = str(side).lower()
        if "buy" in s or "long" in s or s in ("p", "a"):
            return "long"
        if "sell" in s or "short" in s or s == "s":
            return "short"
    return None


def _resolve_smart_money_inflow(feats: Dict[str, Any]) -> Optional[str]:
    """5+ top funds adding (long) vs trimming (short)."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    side = (feats or {}).get("side") or (feats or {}).get("flow")
    if side:
        s = str(side).lower()
        if "add" in s or "buy" in s or "inflow" in s or "long" in s:
            return "long"
        if "trim" in s or "sell" in s or "outflow" in s or "short" in s:
            return "short"
    return None


def _resolve_earnings_revision_shift(feats: Dict[str, Any]) -> Optional[str]:
    """Upward revision → long. Downward → short."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    side = (feats or {}).get("revision_direction") or (feats or {}).get("side")
    if side:
        s = str(side).lower()
        if "up" in s or "raise" in s or "positive" in s or "bull" in s:
            return "long"
        if "down" in s or "cut" in s or "negative" in s or "bear" in s:
            return "short"
    return None


def _resolve_dollar_strength_shift(feats: Dict[str, Any]) -> Optional[str]:
    """USD up → SPX down (short equity). USD down → SPX up (long equity)."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    z = (feats or {}).get("z_score") or (feats or {}).get("z")
    if z is not None:
        try:
            v = float(z)
            if v >= 2.0:
                return "short"   # USD strength → equity headwind
            if v <= -2.0:
                return "long"
        except Exception:
            pass
    side = (feats or {}).get("side")
    if side:
        s = str(side).lower()
        if "up" in s or "strong" in s:
            return "short"
        if "down" in s or "weak" in s:
            return "long"
    return None


def _resolve_composite_macro_regime(feats: Dict[str, Any]) -> Optional[str]:
    """Composite 0-100 risk score: >=60 defensive (short equity);
    <=30 risk-on (long equity); else neutral."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    score = (feats or {}).get("score") or (feats or {}).get("regime_score")
    if score is not None:
        try:
            v = float(score)
            if v >= 60:
                return "short"
            if v <= 30:
                return "long"
            return None
        except Exception:
            pass
    label = (feats or {}).get("regime") or (feats or {}).get("label")
    if label:
        s = str(label).lower()
        if "defensive" in s or "risk_off" in s or "risk-off" in s:
            return "short"
        if "risk_on" in s or "risk-on" in s or "offensive" in s:
            return "long"
    return None


def _resolve_cross_sectional_momentum(feats: Dict[str, Any]) -> Optional[str]:
    """Top quintile → long, bottom quintile → short."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    q = (feats or {}).get("quintile") or (feats or {}).get("rank")
    if q is not None:
        try:
            v = int(q)
            if v >= 5 or v == 1:  # quintile labelling varies; we accept both
                # We map quintile=1 OR quintile=5 — figure out from
                # explicit 'tier'/'side' fields below before risky inference.
                pass
        except Exception:
            pass
    side = (feats or {}).get("side") or (feats or {}).get("tier")
    if side:
        s = str(side).lower()
        if "top" in s or "winner" in s or "long" in s:
            return "long"
        if "bottom" in s or "loser" in s or "short" in s:
            return "short"
    return None


def _resolve_mean_reversion_z(feats: Dict[str, Any]) -> Optional[str]:
    """z < -2 → oversold bounce (long); z > +2 → overbought reversal (short)."""
    direct = _resolve_from_direction_field(feats)
    if direct:
        return direct
    z = (feats or {}).get("z_score") or (feats or {}).get("z")
    if z is None:
        return None
    try:
        v = float(z)
        if v >= 2.0:
            return "short"
        if v <= -2.0:
            return "long"
    except Exception:
        return None
    return None


# ── flow_intel: name encodes direction ───────────────────────────────


_FLOW_LONG = {
    "flow_call_sweep_unusual",
    "flow_call_block_buy",
    "flow_dark_pool_call_lean",
}
_FLOW_SHORT = {
    "flow_put_sweep_unusual",
    "flow_put_block_buy",
    "flow_dark_pool_put_lean",
}


# Dispatch table for dynamic resolvers (pattern → resolver fn).
_DYNAMIC_RESOLVERS = {
    "talib_engulfing": _resolve_talib_engulfing,
    "talib_harami": _resolve_talib_harami,
    "talib_marubozu": _resolve_talib_marubozu,
    "liquidity_sweep": _resolve_liquidity_sweep,
    "stop_hunt": _resolve_liquidity_sweep,
    "lvn_rejection": _resolve_lvn_rejection,
    "break_of_structure": _resolve_bos,
    "bos": _resolve_bos,
    "change_of_character": _resolve_choch,
    "choch": _resolve_choch,
    # SMC v2 (features carry direction).
    "order_block": _resolve_from_direction_field,
    "fair_value_gap": _resolve_from_direction_field,
    "liquidity_sweep_v2": _resolve_from_direction_field,
    "stop_hunt_v2": _resolve_from_direction_field,
    "premium_discount_zone": _resolve_premium_discount_zone,
    "market_structure_shift_v2": _resolve_from_direction_field,
    # Volume Profile v2.
    "value_area_rejection": _resolve_value_area_rejection,
    # Catalyst.
    "pead_drift": _resolve_pead_drift,
    "insider_cluster": _resolve_insider_cluster,
    "smart_money_inflow": _resolve_smart_money_inflow,
    "earnings_revision_shift": _resolve_earnings_revision_shift,
    # Macro.
    "dollar_strength_shift": _resolve_dollar_strength_shift,
    "composite_macro_regime": _resolve_composite_macro_regime,
    # Quantitative.
    "cross_sectional_momentum": _resolve_cross_sectional_momentum,
    "mean_reversion_z": _resolve_mean_reversion_z,
}


def resolve_direction(pattern: str,
                                features: Optional[Dict[str, Any]] = None,
                                ) -> Optional[str]:
    """Authoritative direction resolver. Returns 'long' | 'short' |
    'neutral' | None.

    ``features`` is the detector-emitted features dict (the same one
    persisted to ``market_observations.features`` as JSON). Resolvers
    consult typed keys like ``direction``, ``side``, ``z_score``,
    ``surprise``, etc.

    Pattern fallback order:
      1. Static map (unambiguous patterns).
      2. Flow-intel name-based mapping.
      3. Dynamic resolver from features.
      4. Common 'direction' field on features.
      5. None (legacy fallback in outcome_linker).
    """
    if not pattern:
        return None
    p = str(pattern)
    if p in STATIC_DIRECTION:
        return STATIC_DIRECTION[p]
    if p in _FLOW_LONG:
        return "long"
    if p in _FLOW_SHORT:
        return "short"
    resolver = _DYNAMIC_RESOLVERS.get(p)
    if resolver is not None:
        try:
            return resolver(features or {})
        except Exception:
            return None
    # Pine-imported custom detectors expose direction via features.
    if features:
        return _resolve_from_direction_field(features)
    return None


__all__ = ["resolve_direction", "STATIC_DIRECTION"]
