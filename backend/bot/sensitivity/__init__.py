"""Stage-10 item 15 — slippage-shock backtest sensitivity.

A backtest that survives at quoted slippage but collapses when you add
±20bps of shock isn't actually edge — it's a calibration artefact. This
module wraps ``backtest.simulate_strategy`` with a configurable shock
applied to every fill, then runs a grid of shocks and reports:

  • Per-shock Sharpe + win_rate + total_return + max_dd
  • The "shock_breakdown_bps" — the smallest shock under which a key
    metric (default: Sharpe) collapses below an acceptance threshold
  • Whether the config is "robust" (Sharpe stays > 1.0 at ±20bps)

Designed as a nightly check that flags fragile configs BEFORE they get
promoted by Stage 8's canary process.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ShockPoint:
    shock_bps: float
    sharpe: Optional[float]
    win_rate: Optional[float]
    total_return_pct: float
    max_dd_pct: Optional[float]
    num_trades: int
    net_total_pnl: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ShockSensitivityReport:
    base_sharpe: Optional[float]
    base_win_rate: Optional[float]
    points: List[Dict[str, Any]] = field(default_factory=list)
    shock_breakdown_bps: Optional[float] = None
    robust: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── shock-applying simulator ─────────────────────────────────────────────


def _simulate_with_shock(
    *, strategy_name: str, ticker: str, df: Any,
    shock_bps: float,
) -> Dict[str, Any]:
    """Re-run simulate_strategy with an extra ``shock_bps`` slipping on every
    fill. Mutates the live TUNABLES singleton's ``spread_bps_floor`` in
    place (faster + safer than reloading the config module, which would
    leave stale TUNABLES references in modules that imported it at
    startup and break unrelated tests)."""
    from backend.bot.backtest import (
        compute_indicators, _candles_and_series, _resolve_strategy,
        simulate_strategy,
    )
    from backend.config import TUNABLES

    strategy = _resolve_strategy(strategy_name)
    ind = compute_indicators(df)
    _, _, closes, timestamps = _candles_and_series(df, ind)

    floor_val = max(1.0, abs(float(shock_bps)))
    prev_floor = TUNABLES.spread_bps_floor
    TUNABLES.spread_bps_floor = floor_val
    try:
        return simulate_strategy(
            strategy, ticker, df, ind, closes, timestamps,
            apply_realistic_costs=True,
        )
    finally:
        TUNABLES.spread_bps_floor = prev_floor


# ── grid runner ──────────────────────────────────────────────────────────


def shock_sensitivity_grid(
    *, strategy_name: str, ticker: str,
    period: str = "6mo", interval: str = "1d",
    shocks_bps: Optional[List[float]] = None,
    min_sharpe: float = 1.0,
) -> ShockSensitivityReport:
    """Run the backtest across a grid of shock_bps values + summarize."""
    from backend.bot.backtest import fetch_candles

    shocks_bps = shocks_bps or [0.0, 5.0, 10.0, 20.0, 40.0]
    df = fetch_candles(ticker, period=period, interval=interval)
    if df is None or df.empty:
        return ShockSensitivityReport(
            base_sharpe=None, base_win_rate=None,
            notes=[f"no candles for {ticker}"],
        )
    points: List[ShockPoint] = []
    base = None
    breakdown: Optional[float] = None
    for shock in shocks_bps:
        result = _simulate_with_shock(
            strategy_name=strategy_name, ticker=ticker, df=df,
            shock_bps=shock,
        )
        pt = ShockPoint(
            shock_bps=shock,
            sharpe=result.get("sharpe"),
            win_rate=result.get("win_rate"),
            total_return_pct=result.get("total_return_pct") or 0.0,
            max_dd_pct=result.get("max_drawdown_pct"),
            num_trades=result.get("num_trades") or 0,
            net_total_pnl=result.get("total_costs_dollar"),
        )
        points.append(pt)
        if base is None:
            base = pt
        # First shock under which Sharpe collapses below ``min_sharpe``
        if breakdown is None and pt.sharpe is not None and pt.sharpe < min_sharpe:
            breakdown = shock
    robust = (base is not None and base.sharpe is not None
                and base.sharpe >= min_sharpe and breakdown is None)
    return ShockSensitivityReport(
        base_sharpe=base.sharpe if base else None,
        base_win_rate=base.win_rate if base else None,
        points=[p.to_dict() for p in points],
        shock_breakdown_bps=breakdown,
        robust=robust,
        notes=([f"strategy '{strategy_name}' is robust to ±{max(shocks_bps):.0f}bps"]
                 if robust else
                 [f"strategy '{strategy_name}' collapses at "
                   f"+{breakdown:.0f}bps shock" if breakdown else
                   "insufficient shock coverage"]),
    )
