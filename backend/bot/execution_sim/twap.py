"""Stage-10 item 14 — TWAP slicing simulator.

A single market order of $50k assumes you can fill instantly at the
quoted price. Real desks slice large orders into N TWAP buckets to reduce
market impact + lower realized slippage. Paper-mode backtests that don't
model slicing systematically over-estimate edge.

This module slices ``total_quantity`` evenly across ``n_slices`` bars,
prices each slice at the bar's close + a tiny incremental impact term,
returns the volume-weighted average fill price.

Pure given the bar sequence — deterministic and testable.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TWAPSlice:
    slice_index: int
    bar_index: int
    quantity: float
    price: float
    slippage_bps: float
    timestamp: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TWAPResult:
    total_quantity: float
    n_slices: int
    avg_fill_price: float
    total_cost_bps: float          # cumulative slippage over all slices
    slices: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── public API ───────────────────────────────────────────────────────────


def simulate_twap(
    *,
    side: str,
    total_quantity: float,
    bars: List[Dict[str, Any]],
    n_slices: int = 5,
    base_slippage_bps: float = 2.0,
) -> TWAPResult:
    """Slice ``total_quantity`` across ``n_slices`` consecutive bars, price
    each slice at bar close ± a TWAP-inflated slippage.

    The slippage on slice ``k`` is ``base_slippage_bps × (1 + k × 0.05)``
    — incremental impact as the prior slices push the price.
    """
    n = min(n_slices, len(bars))
    if n <= 0 or total_quantity <= 0:
        return TWAPResult(total_quantity=total_quantity, n_slices=0,
                            avg_fill_price=0.0, total_cost_bps=0.0,
                            notes=["no bars or zero quantity"])
    side_sign = 1 if str(side).upper() == "BUY" else -1
    slice_qty = total_quantity / n
    slices: List[TWAPSlice] = []
    weighted_price_sum = 0.0
    total_bps = 0.0
    for k in range(n):
        bar = bars[k]
        close = float(bar.get("close") or bar.get("open") or 0.0)
        if close <= 0:
            continue
        slippage_bps = base_slippage_bps * (1.0 + k * 0.05)
        impact = side_sign * slippage_bps / 1e4
        fill_price = close * (1.0 + impact)
        slices.append(TWAPSlice(
            slice_index=k, bar_index=k, quantity=slice_qty,
            price=round(fill_price, 4),
            slippage_bps=round(slippage_bps, 2),
            timestamp=bar.get("timestamp"),
        ))
        weighted_price_sum += slice_qty * fill_price
        total_bps += slippage_bps
    filled_total = sum(s.quantity for s in slices)
    avg = (weighted_price_sum / filled_total) if filled_total else 0.0
    notes: List[str] = []
    if n < n_slices:
        notes.append(f"only {n} bars available for {n_slices} requested slices")
    return TWAPResult(
        total_quantity=total_quantity, n_slices=n,
        avg_fill_price=round(avg, 4),
        total_cost_bps=round(total_bps, 2),
        slices=[s.to_dict() for s in slices], notes=notes,
    )


# ── comparison helper ────────────────────────────────────────────────────


def market_vs_twap(*, side: str, total_quantity: float,
                       bars: List[Dict[str, Any]], n_slices: int = 5,
                       base_slippage_bps: float = 2.0,
                       market_slippage_bps: float = 25.0,
                       ) -> Dict[str, Any]:
    """Show the slippage savings of TWAP vs a single market order.

    Naive market order: assumes you cross the spread + eat the full
    ``market_slippage_bps`` of impact at the first bar.
    """
    twap = simulate_twap(
        side=side, total_quantity=total_quantity, bars=bars,
        n_slices=n_slices, base_slippage_bps=base_slippage_bps,
    )
    first_bar = bars[0] if bars else {}
    market_close = float(first_bar.get("close") or 0.0)
    side_sign = 1 if str(side).upper() == "BUY" else -1
    market_price = market_close * (1.0 + side_sign * market_slippage_bps / 1e4)
    savings_bps = market_slippage_bps - (twap.total_cost_bps / max(1, n_slices))
    return {
        "twap": twap.to_dict(),
        "market": {
            "fill_price": round(market_price, 4),
            "slippage_bps": market_slippage_bps,
        },
        "savings_bps": round(savings_bps, 2),
        "savings_dollar": round((market_price - twap.avg_fill_price) * total_quantity
                                  if side_sign > 0 else
                                  (twap.avg_fill_price - market_price) * total_quantity,
                                  2),
    }
