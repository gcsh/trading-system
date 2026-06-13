"""Stage-2 execution-cost model — what does a trade ACTUALLY cost?

The bot's P&L numbers up to Stage 1 ignored commissions, bid-ask spread, and
size-driven slippage. That's why a "winning" strategy in paper can lose money
the moment it touches a live broker — the costs eat the edge. Stage 2's job
is to make every cost explicit, modeled, and bookable so backtests and paper
P&L reflect what a real execution would yield.

Three components, separately tested + separately surfaced:

  1. **Commission** — broker-specific schedule. $0 retail (Alpaca / Robinhood)
     is one row in the catalog; tiered IBKR-style is another.
  2. **Spread cost** — half the bid-ask quoted in basis points; you cross
     half on entry, half on exit. Estimated from ATR + volume when no live
     quote is available.
  3. **Slippage** — market impact as a function of order size relative to
     average daily volume (ADV), scaled by realized volatility. Square-root
     law-ish but capped + floored so backtest results stay sane.

Output is a single ``CostEstimate`` consumed by:
  • ``backtest.simulate_strategy`` — every entry/exit is debited the cost
  • ``/execution/costs/preview`` — UI shows expected cost before order fires
  • ``execution_sim`` — partial-fill / multi-leg simulator uses spread+slippage
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from backend.config import TUNABLES


@dataclass
class CostEstimate:
    """One canonical cost breakdown — every field in $."""
    commission: float = 0.0
    spread_cost: float = 0.0
    slippage: float = 0.0
    total: float = 0.0
    # Bps versions (per $ traded) for cross-asset comparison.
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    total_bps: float = 0.0
    notional: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── commission catalog ─────────────────────────────────────────────────────


@dataclass
class CommissionSchedule:
    """One row in the broker catalog. All costs in USD."""
    name: str
    stock_per_share: float = 0.0
    stock_minimum: float = 0.0
    stock_maximum_pct: float = 0.0           # e.g. 0.01 = cap at 1% of notional
    option_per_contract: float = 0.0
    option_minimum: float = 0.0

    def stock_cost(self, shares: float, notional: float) -> float:
        c = max(self.stock_minimum, self.stock_per_share * abs(shares))
        if self.stock_maximum_pct > 0:
            c = min(c, abs(notional) * self.stock_maximum_pct)
        return round(c, 4)

    def option_cost(self, contracts: float) -> float:
        return round(max(self.option_minimum,
                          self.option_per_contract * abs(contracts)), 4)


# Catalog: the rows are the broker contracts we actually plan to support.
# Edit here, not in callers — keeps the cost model auditable.
COMMISSION_CATALOG: Dict[str, CommissionSchedule] = {
    "local_paper": CommissionSchedule(name="local_paper"),
    "alpaca_paper": CommissionSchedule(name="alpaca_paper"),
    "alpaca_live": CommissionSchedule(
        name="alpaca_live",
        # Alpaca: $0 commissions on stocks/ETFs, $0 on equity options as of
        # 2024+ retail. Tiered fees apply to crypto; we don't trade crypto live.
    ),
    "robinhood": CommissionSchedule(name="robinhood"),
    "ibkr_lite": CommissionSchedule(
        name="ibkr_lite",
        # IBKR Lite — $0 stocks, but real options fees.
        option_per_contract=0.65,
        option_minimum=1.00,
    ),
    "ibkr_pro": CommissionSchedule(
        name="ibkr_pro",
        # IBKR Pro tiered — per-share + minimum.
        stock_per_share=0.0035,
        stock_minimum=0.35,
        stock_maximum_pct=0.01,
        option_per_contract=0.65,
        option_minimum=1.00,
    ),
}


def commission_for(broker: str, instrument: str, shares: float = 0,
                    contracts: float = 0, notional: float = 0.0) -> float:
    """Look up the broker schedule and price the trade. Falls back to free."""
    sched = COMMISSION_CATALOG.get(broker, COMMISSION_CATALOG["local_paper"])
    if instrument in ("option", "spread"):
        return sched.option_cost(contracts)
    return sched.stock_cost(shares, notional)


# ── spread + slippage estimators ───────────────────────────────────────────


def estimate_spread_bps(snapshot: Optional[Dict[str, Any]] = None,
                          *, price: Optional[float] = None,
                          atr: Optional[float] = None) -> float:
    """Approximate half-bid-ask spread in basis points (one side of the cross).

    Heuristic when no live quote is available:
      ``spread_bps = max(floor, atr_pct × multiplier)``

    Liquid names (SPY/QQQ): ~1 bp.  Thin-volume names: 50+ bps.  The model is
    intentionally conservative — better to overestimate cost than to claim a
    paper win that wouldn't hold.
    """
    snapshot = snapshot or {}
    if price is None:
        price = float(snapshot.get("price") or 0.0)
    if atr is None:
        atr = snapshot.get("atr")
    floor = float(getattr(TUNABLES, "spread_bps_floor", 1.0))
    mult = float(getattr(TUNABLES, "spread_atr_multiplier", 0.5))
    if price <= 0 or atr is None or atr <= 0:
        return max(floor, float(getattr(TUNABLES, "spread_bps_default", 5.0)))
    atr_pct = float(atr) / price
    return round(max(floor, atr_pct * 1e4 * mult), 2)


def estimate_slippage_bps(notional: float,
                            *, snapshot: Optional[Dict[str, Any]] = None,
                            adv_dollar: Optional[float] = None,
                            volatility: Optional[float] = None) -> float:
    """Slippage in bps as a function of order size relative to ADV, scaled by
    realized volatility. Square-root impact law, capped + floored.

    impact_bps = k × √(notional / ADV) × vol_factor
        where vol_factor = max(0.5, ann_vol / 0.20)
    """
    if notional <= 0:
        return 0.0
    snapshot = snapshot or {}
    if adv_dollar is None:
        # Best-effort: 10-day avg volume × price. Fallback baseline so the
        # model never DIV/0 on a low-data ticker.
        vol_avg = snapshot.get("volume_avg") or snapshot.get("volume") or 0.0
        price = float(snapshot.get("price") or 0.0)
        adv_dollar = float(vol_avg) * price if vol_avg and price else 0.0
    if volatility is None:
        # Use IV rank or ATR-implied move as a vol proxy.
        iv = snapshot.get("iv_rank")
        volatility = float(iv) / 100.0 if iv else None

    k = float(getattr(TUNABLES, "slippage_k_bps", 8.0))
    default_adv = float(getattr(TUNABLES, "slippage_default_adv_dollar", 5_000_000.0))
    eff_adv = max(adv_dollar or 0.0, 1.0)
    if eff_adv < 1_000:
        eff_adv = default_adv
    impact = k * math.sqrt(notional / eff_adv)
    vol_factor = max(0.5, (volatility or 0.20) / 0.20)
    bps = impact * vol_factor
    bps = max(0.0, min(bps, float(getattr(TUNABLES, "slippage_bps_cap", 200.0))))
    return round(bps, 2)


# ── top-level estimator ────────────────────────────────────────────────────


def estimate_total_cost(
    *,
    broker: str,
    instrument: str,
    side: str,                 # "BUY" | "SELL"
    quantity: float,           # shares for stock, contracts for option/spread
    price: float,
    snapshot: Optional[Dict[str, Any]] = None,
    strike: Optional[float] = None,
) -> CostEstimate:
    """Single entry-point. Returns a fully populated ``CostEstimate``.

    Notional definition:
      • stock        : ``shares × price``
      • option/spread: ``max(0.05, 0.03 × strike) × 100 × contracts``
                       (matches paper executor premium model; consistent with
                        ``backend/bot/labeling._entry_notional``)
    """
    snapshot = snapshot or {}
    notes: list[str] = []

    if instrument in ("option", "spread"):
        if not strike:
            strike = snapshot.get("price")
        per_share = max(0.05, 0.03 * float(strike or 0.0))
        notional = per_share * 100 * abs(quantity)
        commission = commission_for(broker, instrument, contracts=quantity,
                                      notional=notional)
    else:
        notional = abs(quantity) * abs(price)
        commission = commission_for(broker, "stock", shares=quantity,
                                      notional=notional)

    spread_bps = estimate_spread_bps(snapshot, price=price)
    spread_cost = notional * spread_bps / 1e4

    slippage_bps = estimate_slippage_bps(notional, snapshot=snapshot)
    slippage = notional * slippage_bps / 1e4

    total = commission + spread_cost + slippage
    total_bps = (total / notional * 1e4) if notional > 0 else 0.0

    if broker == "local_paper" and commission == 0:
        notes.append("local_paper has $0 commission; spread + slippage still apply")
    if notional > 100_000:
        notes.append("large order — slippage likely understated; prefer slicing")

    return CostEstimate(
        commission=round(commission, 4),
        spread_cost=round(spread_cost, 4),
        slippage=round(slippage, 4),
        total=round(total, 4),
        spread_bps=round(spread_bps, 2),
        slippage_bps=round(slippage_bps, 2),
        total_bps=round(total_bps, 2),
        notional=round(notional, 2),
        notes=notes,
    )
