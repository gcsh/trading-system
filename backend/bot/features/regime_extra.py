"""Stage-10 items 17 + 18 — extra regime-discriminating features.

Two functions, both pure given the snapshot series:

  • ``gex_dprice_slope`` — derivative of dealer gamma exposure with respect
    to spot price. Steeply NEGATIVE slope = dealers are hedging FAST
    against price moves (each $1 up in spot drops dealer net delta a lot)
    which is the regime where mean-reversion strategies work best. POSITIVE
    or flat slope = dealers are unwound or directionally aligned with the
    tape — momentum / breakout strategies dominate.

  • ``intraday_vol_of_vol`` — rolling stddev of a rolling-stddev return
    series. Spikes BEFORE volatility itself spikes; a powerful early
    warning for regime transitions (chop → trend, calm → blow-up).

Both are intended to be added to the ``build_features`` pipeline as
optional inputs the ranker can blend into composite_bias.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


def gex_dprice_slope(
    snapshots: Sequence[Dict[str, Any]],
    *,
    min_obs: int = 3,
) -> Optional[float]:
    """Linear-regression slope of ``gex_total`` against ``price`` over the
    provided snapshot history (most recent N entries).

    Each snapshot must have ``price`` and ``gex_total`` keys. Returns slope
    in units of (GEX-units / $1 of spot). ``None`` when fewer than
    ``min_obs`` valid observations exist.
    """
    pairs: List[tuple] = []
    for s in snapshots:
        price = s.get("price")
        gex = s.get("gex_total")
        if price is None or gex is None:
            continue
        try:
            pairs.append((float(price), float(gex)))
        except (TypeError, ValueError):
            continue
    if len(pairs) < min_obs:
        return None
    n = len(pairs)
    sum_x = sum(p for p, _ in pairs)
    sum_y = sum(g for _, g in pairs)
    sum_xx = sum(p * p for p, _ in pairs)
    sum_xy = sum(p * g for p, g in pairs)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    return round((n * sum_xy - sum_x * sum_y) / denom, 6)


def intraday_vol_of_vol(
    returns: Sequence[float],
    *,
    inner_window: int = 12,
    outer_window: int = 24,
) -> Optional[float]:
    """Stddev of rolling stddev of returns — measures how stable the
    volatility regime itself is.

    Args:
        returns: per-bar (e.g. 5-min) returns as decimals.
        inner_window: bars in each inner-stddev window (rolling vol).
        outer_window: how many inner-stddev observations to compute the
            outer stddev over.

    Returns None when there aren't enough bars (need at least
    ``inner_window + outer_window``).
    """
    if len(returns) < inner_window + outer_window:
        return None
    inner_vols: List[float] = []
    for i in range(inner_window, len(returns) + 1):
        window = returns[i - inner_window:i]
        if len(window) < 2:
            continue
        mu = sum(window) / len(window)
        var = sum((r - mu) ** 2 for r in window) / (len(window) - 1)
        inner_vols.append(math.sqrt(var))
    if len(inner_vols) < outer_window:
        return None
    recent = inner_vols[-outer_window:]
    mu = sum(recent) / len(recent)
    outer_var = sum((v - mu) ** 2 for v in recent) / (len(recent) - 1)
    return round(math.sqrt(outer_var), 8)
