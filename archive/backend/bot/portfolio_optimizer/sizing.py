"""Stage-6 position-sizing math.

Four classical sizing primitives. Each is a pure function — given the same
inputs, returns the same dollar size + reasoning. Combined in
``optimizer.py`` to produce the final position size for a trade plan.

  1. Kelly fraction — proven edge → optimal growth-rate-maximizing size
  2. CVaR-conditioned — cap so worst-case loss respects the daily budget
  3. Volatility targeting — scale exposure inversely to realized vol
  4. Drawdown-conditioned — cut size after drawdown exceeds threshold

All return a fraction of equity ∈ [0, 1] plus the reasoning trail. The
caller (optimizer) takes the MIN of every applicable cap so no single
algorithm can override a more conservative one.

Constants live in ``TUNABLES`` — never magic numbers in logic.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES


@dataclass
class SizingResult:
    fraction: float = 0.0          # of total equity, ∈ [0, 1]
    dollar_size: float = 0.0
    reason: str = ""
    components: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Kelly ──────────────────────────────────────────────────────────────────


def kelly_fraction(*, win_rate: float, avg_win: float,
                    avg_loss: float, fraction: Optional[float] = None) -> float:
    """Fractional Kelly. Returns f* × ``fraction`` (defaults to 0.25× = quarter
    Kelly for safety).

    f* = (b·p - q) / b
    where:
      b = |avg_win / avg_loss|  (payoff ratio)
      p = win_rate
      q = 1 - p
    """
    if win_rate is None or avg_win is None or avg_loss is None:
        return 0.0
    if avg_win <= 0 or avg_loss >= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    payoff = avg_win / abs(avg_loss)
    if payoff <= 0:
        return 0.0
    q = 1.0 - win_rate
    f_star = (payoff * win_rate - q) / payoff
    if f_star <= 0:
        return 0.0
    frac = fraction if fraction is not None else float(
        getattr(TUNABLES, "kelly_fraction", 0.25)
    )
    return round(max(0.0, min(1.0, f_star * frac)), 4)


# ── CVaR-conditioned ─────────────────────────────────────────────────────


def cvar_size_fraction(*, equity: float, daily_loss_budget: float,
                        sigma_pct: float, confidence: float = 0.95,
                        holding_days: float = 1.0) -> float:
    """Position size such that the conditional expected shortfall at
    ``confidence`` doesn't exceed ``daily_loss_budget``.

    For a normal return distribution, CVaR at confidence α is
        CVaR = -μ + σ · φ(z_α) / (1 - α)
    For paper simplicity we use the simpler tail-VaR: position fraction =
    daily_loss_budget / (equity × σ × z_α × √holding_days).
    """
    if equity <= 0 or sigma_pct <= 0 or daily_loss_budget <= 0:
        return 0.0
    # z-score for α=0.95 ≈ 1.645; α=0.99 ≈ 2.326
    z = 2.326 if confidence >= 0.99 else (1.96 if confidence >= 0.975 else 1.645)
    tail_loss_pct = sigma_pct * z * math.sqrt(max(holding_days, 1.0))
    if tail_loss_pct <= 0:
        return 0.0
    fraction = (daily_loss_budget / equity) / tail_loss_pct
    return round(max(0.0, min(1.0, fraction)), 4)


# ── Volatility-targeting ─────────────────────────────────────────────────


def vol_target_fraction(*, target_vol: float, asset_vol: float) -> float:
    """Scale exposure inversely to the asset's realized volatility so the
    contribution to portfolio vol stays at ``target_vol`` (annualized,
    decimal — 0.15 = 15%)."""
    if target_vol <= 0 or asset_vol <= 0:
        return 0.0
    return round(max(0.0, min(1.0, target_vol / asset_vol)), 4)


# ── Drawdown-conditioned ────────────────────────────────────────────────


def drawdown_size_multiplier(*, current_drawdown_pct: float,
                              cut_threshold: Optional[float] = None,
                              hard_floor: Optional[float] = None) -> float:
    """Return a multiplier ∈ [floor, 1] to apply to the un-conditioned size.

    Rules (config-driven):
      • Above cut threshold → linearly scale down to ``hard_floor``
      • Default cut at 5% DD, floor at 0.25× when DD hits 20%
    """
    cut = cut_threshold if cut_threshold is not None else float(
        getattr(TUNABLES, "portfolio_max_drawdown_cut", 0.05)
    )
    floor = hard_floor if hard_floor is not None else float(
        getattr(TUNABLES, "portfolio_dd_size_floor", 0.25)
    )
    dd = max(0.0, current_drawdown_pct)
    if dd <= cut:
        return 1.0
    # linear ramp from 1.0 at cut to floor at 4× cut (typical 20% DD)
    span = max(cut, 0.0001)
    ratio = min(1.0, (dd - cut) / (3 * span))
    return round(max(floor, 1.0 - (1.0 - floor) * ratio), 4)


# ── combined size for one trade ────────────────────────────────────────────


def combine_size(*, equity: float,
                   kelly_frac: float = 0.0,
                   cvar_frac: float = 0.0,
                   vol_target_frac: float = 0.0,
                   dd_multiplier: float = 1.0,
                   strategy_allocation_frac: float = 1.0,
                   reasons: Optional[List[str]] = None) -> SizingResult:
    """Take the MIN of every applicable fraction cap, then apply the
    drawdown multiplier + the per-strategy allocation. Returns the final
    dollar size + transparency on which constraint was binding."""
    caps = {
        "kelly": kelly_frac,
        "cvar": cvar_frac,
        "vol_target": vol_target_frac,
        "strategy_allocation": strategy_allocation_frac,
    }
    active_caps = {k: v for k, v in caps.items() if v > 0}
    if not active_caps:
        return SizingResult(notes=["no active sizing rule produced a fraction"])

    binding_name = min(active_caps, key=lambda k: active_caps[k])
    base_fraction = active_caps[binding_name]
    final_fraction = base_fraction * dd_multiplier
    return SizingResult(
        fraction=round(final_fraction, 4),
        dollar_size=round(equity * final_fraction, 2),
        reason=f"binding cap: {binding_name} @ {base_fraction:.2%}; "
                 f"drawdown multiplier {dd_multiplier:.2f}",
        components={
            **caps, "dd_multiplier": dd_multiplier,
            "binding_cap": binding_name,
        },
        notes=reasons or [],
    )
