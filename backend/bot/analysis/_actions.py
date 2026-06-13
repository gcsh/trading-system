"""Shared suggested-action helper for fast + deep composers.

Reuses the chain-resolved strike + posterior/sample gate so the action
card surfaced by Phase 14.A's fast path is identical to what the deep
Claude path emits when its `suggested_action` survives validation.

The bullish/bearish direction map is derived from the authoritative
``backend.bot.detectors.direction`` module (no hardcoded copy).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from backend.bot.detectors.direction import STATIC_DIRECTION


SUGGESTED_ACTION_MIN_POSTERIOR = 0.60
SUGGESTED_ACTION_MIN_SAMPLES = 30


def is_bullish_pattern(pattern: str) -> bool:
    """Authoritative bullish check.

    Uses the static map in detectors/direction.py. Returns False for
    None / neutral / unknown so the caller's `elif is_bearish` path
    cleanly catches the other side.
    """
    return STATIC_DIRECTION.get(pattern) == "long"


def is_bearish_pattern(pattern: str) -> bool:
    return STATIC_DIRECTION.get(pattern) == "short"


def resolve_suggested_strike(
    ticker: str, spot: float, direction: str, dte_target: int,
) -> Tuple[Optional[float], str]:
    """Chain-aware strike resolver. Returns (strike, source) with
    source ∈ {'chain', 'snap_fallback'}.
    """
    if not spot or spot <= 0:
        return None, "snap_fallback"
    kind = "call" if direction in ("long_call", "call_spread") else "put"
    sign = +1.0 if kind == "call" else -1.0
    target_delta = 0.40
    moneyness = 0.01 * sign
    try:
        from backend.bot.data.options import chain_strike, snap_strike
    except Exception:
        return None, "snap_fallback"
    try:
        listed = chain_strike(
            ticker, spot, kind,
            moneyness=moneyness,
            target_dte=int(dte_target),
            target_delta=target_delta,
        )
        if listed and listed > 0:
            arithmetic = snap_strike(spot, kind, moneyness)
            if abs(listed - arithmetic) < 1e-6:
                return float(listed), "snap_fallback"
            return float(listed), "chain"
    except Exception:
        pass
    try:
        return float(snap_strike(spot, kind, moneyness)), "snap_fallback"
    except Exception:
        return None, "snap_fallback"


def build_suggested_action(
    *, pattern: str, knowledge: Dict[str, Any], ticker: str,
    spot: Optional[float], dte_target: int = 30,
) -> Optional[Dict[str, Any]]:
    """Heuristic action card. Posterior + sample-size gated. Returns
    None when the cohort doesn't clear the floor or the pattern has no
    directional bias.
    """
    post = float(knowledge.get("posterior_win_rate") or 0.0)
    n = int(knowledge.get("sample_size") or 0)
    if post < SUGGESTED_ACTION_MIN_POSTERIOR or n < SUGGESTED_ACTION_MIN_SAMPLES:
        return None
    if is_bearish_pattern(pattern):
        action = "BUY_PUT"
        direction = "long_put"
    elif is_bullish_pattern(pattern):
        action = "BUY_CALL"
        direction = "long_call"
    else:
        return None
    strike, strike_source = resolve_suggested_strike(
        ticker, float(spot or 0.0), direction, dte_target,
    )
    return {
        "action": action,
        "direction": direction,
        "strike": strike,
        "strike_source": strike_source,
        "dte": dte_target,
        "dte_target": dte_target,
        "target_premium_pct": 50,
        "stop_premium_pct": 30,
        "rationale": (
            f"Historical {pattern} on {ticker} won {post*100:.0f}% of the "
            f"time (N={n}); risk one unit, target 50% on the option, "
            f"stop 30%."
        ),
    }


__all__ = [
    "SUGGESTED_ACTION_MIN_POSTERIOR",
    "SUGGESTED_ACTION_MIN_SAMPLES",
    "is_bullish_pattern",
    "is_bearish_pattern",
    "resolve_suggested_strike",
    "build_suggested_action",
]
