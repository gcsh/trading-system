"""Stage-6 top-level portfolio optimizer.

Single entry point. Given:
  • a candidate trade plan
  • the current account state (equity, drawdown)
  • current positions
  • recent metrics (per-strategy)
  • the strategy's metrics (win_rate, expectancy, avg_win/avg_loss)
  • an asset volatility estimate
… return the recommended dollar size + a transparent reasoning trail.

The optimizer never INCREASES size — only ratchets down based on
portfolio-level constraints. Existing per-trade risk evaluation runs
first; the optimizer is a second-pass cap.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.portfolio_optimizer.allocator import (
    StrategyAllocation,
    allocate_capital,
)
from backend.bot.portfolio_optimizer.correlation import check_cluster_cap
from backend.bot.portfolio_optimizer.sizing import (
    SizingResult,
    combine_size,
    cvar_size_fraction,
    drawdown_size_multiplier,
    kelly_fraction,
    vol_target_fraction,
)
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class OptimizerDecision:
    ticker: str
    requested_dollar: float
    recommended_dollar: float
    cluster_blocked: bool = False
    cluster_after_pct: float = 0.0
    cluster_cap_pct: float = 0.0
    sizing: Dict[str, Any] = field(default_factory=dict)
    strategy_share: float = 0.0
    drawdown_pct: float = 0.0
    drawdown_multiplier: float = 1.0
    reasoning: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def optimize_size(*,
                    ticker: str,
                    strategy: str,
                    requested_dollar: float,
                    equity: float,
                    drawdown_pct: float = 0.0,
                    positions: Optional[List[Dict[str, Any]]] = None,
                    by_strategy_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
                    asset_volatility: Optional[float] = None,
                    daily_loss_budget: Optional[float] = None,
                    ) -> OptimizerDecision:
    """Run the full optimizer pipeline for a single candidate trade."""
    positions = positions or []
    by_strategy_metrics = by_strategy_metrics or {}
    reasoning: List[str] = []

    if equity <= 0 or requested_dollar <= 0:
        return OptimizerDecision(
            ticker=ticker.upper(), requested_dollar=requested_dollar,
            recommended_dollar=0.0,
            reasoning=["equity or requested size is zero"],
        )

    # 1. Per-strategy allocation share
    allocations = allocate_capital(by_strategy_metrics)
    strategy_share = next(
        (a.share for a in allocations if a.strategy == strategy), 0.0
    )
    if not strategy_share:
        # No metrics for this strategy yet — apply the floor
        strategy_share = float(getattr(TUNABLES, "strategy_min_allocation", 0.05))
        reasoning.append(f"strategy '{strategy}' has no metrics — using floor "
                          f"{strategy_share:.0%}")

    # 2. Sizing primitives
    s_metrics = by_strategy_metrics.get(strategy, {})
    closed = int(s_metrics.get("closed") or 0)
    win_rate = float(s_metrics.get("win_rate") or 0.0)
    avg_win = float(s_metrics.get("avg_win") or 0.0) if s_metrics.get("avg_win") else 0.0
    avg_loss = float(s_metrics.get("avg_loss") or 0.0) if s_metrics.get("avg_loss") else 0.0

    kelly = kelly_fraction(win_rate=win_rate, avg_win=avg_win,
                            avg_loss=avg_loss) if closed >= 10 else 0.0
    if kelly == 0.0:
        reasoning.append("Kelly inactive (< 10 closed trades or non-positive edge)")

    cvar = 0.0
    if asset_volatility and daily_loss_budget:
        cvar = cvar_size_fraction(
            equity=equity, daily_loss_budget=daily_loss_budget,
            sigma_pct=asset_volatility,
        )
        reasoning.append(f"CVaR cap @ 95%: {cvar:.2%} of equity")

    vol_t = 0.0
    if asset_volatility:
        target_vol = float(getattr(TUNABLES, "portfolio_target_vol", 0.15))
        vol_t = vol_target_fraction(target_vol=target_vol, asset_vol=asset_volatility)
        reasoning.append(f"vol-target cap (target {target_vol:.0%} / asset "
                          f"{asset_volatility:.0%}): {vol_t:.2%}")

    dd_mult = drawdown_size_multiplier(current_drawdown_pct=drawdown_pct)
    if dd_mult < 1.0:
        reasoning.append(f"drawdown {drawdown_pct:.1%} → size × {dd_mult:.2f}")

    sizing = combine_size(
        equity=equity,
        kelly_frac=kelly, cvar_frac=cvar, vol_target_frac=vol_t,
        strategy_allocation_frac=strategy_share, dd_multiplier=dd_mult,
        reasons=reasoning,
    )

    # 3. Cluster-cap check (using the SIZED order, not the requested one)
    cluster_check = check_cluster_cap(
        ticker=ticker, new_value=min(requested_dollar, sizing.dollar_size),
        positions=positions, equity=equity,
    )
    if cluster_check.blocked:
        reasoning.append(
            f"cluster '{cluster_check.cluster_label}' cap binding — "
            f"max allowed ${cluster_check.allowed_value:.2f}"
        )
        recommended = min(sizing.dollar_size, cluster_check.allowed_value,
                            requested_dollar)
    else:
        recommended = min(sizing.dollar_size, requested_dollar)

    return OptimizerDecision(
        ticker=ticker.upper(),
        requested_dollar=round(requested_dollar, 2),
        recommended_dollar=round(max(0.0, recommended), 2),
        cluster_blocked=cluster_check.blocked,
        cluster_after_pct=cluster_check.cluster_after,
        cluster_cap_pct=cluster_check.cluster_cap,
        sizing=sizing.to_dict(),
        strategy_share=strategy_share,
        drawdown_pct=drawdown_pct,
        drawdown_multiplier=dd_mult,
        reasoning=reasoning,
    )
