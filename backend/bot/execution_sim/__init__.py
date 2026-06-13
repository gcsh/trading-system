"""Stage-2 execution simulator — partial fills + multi-leg atomicity failures.

Stock and option orders don't always fill the way the engine asks. The
simulator answers three questions for backtests + paper:

  1. **Did the order fill in full?** A single bar can absorb only so much
     volume — a 20 000-share market order on a name doing 50 000 shares per
     5-min bar will partial-fill. We cap each fill at a configurable share
     of bar volume and slice the remainder forward.

  2. **What price did each slice fill at?** Half-spread + slippage from the
     cost model (consistent with ``execution_costs``).

  3. **Did all legs of a spread fill, or did one leg fail after another
     already filled?** For brokers without combo orders (Robinhood), there's
     a configurable per-leg failure probability — the simulator returns the
     atomicity event so backtests can model the loss of running half a
     spread.

Pure given inputs (no DB, no network). Deterministic when ``rng_seed`` is set.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from backend.bot.execution_costs import estimate_slippage_bps, estimate_spread_bps
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class FillSlice:
    quantity: float
    price: float
    bar_index: int = 0
    cost_bps: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"quantity": self.quantity, "price": round(self.price, 4),
                 "bar_index": self.bar_index, "cost_bps": self.cost_bps}


@dataclass
class FillResult:
    requested_quantity: float
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    slices: List[FillSlice] = field(default_factory=list)
    partial: bool = False
    bars_used: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_quantity": self.requested_quantity,
            "filled_quantity": round(self.filled_quantity, 6),
            "avg_fill_price": round(self.avg_fill_price, 4),
            "slices": [s.to_dict() for s in self.slices],
            "partial": self.partial,
            "bars_used": self.bars_used,
            "notes": self.notes,
        }


@dataclass
class LegResult:
    leg: Dict[str, Any]
    filled: bool
    fill: Optional[FillResult] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"leg": self.leg, "filled": self.filled,
                 "fill": self.fill.to_dict() if self.fill else None,
                 "reason": self.reason}


@dataclass
class AtomicityResult:
    """One run through a multi-leg spread. ``atomic_failure`` is True when
    SOME legs filled and OTHERS didn't — the worst-case partial spread."""
    atomic: bool
    atomic_failure: bool
    legs: List[LegResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"atomic": self.atomic, "atomic_failure": self.atomic_failure,
                 "legs": [l.to_dict() for l in self.legs], "notes": self.notes}


# ── partial fills over a sequence of bars ──────────────────────────────────


def simulate_fill(
    *,
    side: str,
    quantity: float,
    bars: Sequence[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]] = None,
    instrument: str = "stock",
    volume_share_cap: Optional[float] = None,
    max_bars: int = 10,
) -> FillResult:
    """Walk forward through ``bars`` until ``quantity`` is filled or the cap
    runs out. Each bar can absorb ``volume_share_cap`` × bar_volume of order
    flow. Slices are priced at bar VWAP-ish + spread + slippage.

    Args:
        bars: a sequence of dicts with keys ``open``, ``close``, ``high``,
              ``low``, ``volume``. Bars BEFORE the entry decision aren't
              passed in; this is forward-only.

    Returns ``FillResult`` even if nothing filled (``filled_quantity == 0``).
    """
    requested = abs(float(quantity))
    if requested <= 0 or not bars:
        return FillResult(requested_quantity=requested, partial=False)

    cap = volume_share_cap if volume_share_cap is not None else float(
        getattr(TUNABLES, "fill_volume_share_cap", 0.10)
    )
    snapshot = snapshot or {}
    side_sign = 1 if str(side).upper() == "BUY" else -1
    spread_bps = estimate_spread_bps(snapshot)

    remaining = requested
    slices: List[FillSlice] = []
    notes: List[str] = []

    for i, bar in enumerate(bars[:max_bars]):
        if remaining <= 0:
            break
        bar_volume = float(bar.get("volume") or 0.0)
        ref_price = float(bar.get("close") or bar.get("open") or 0.0)
        if ref_price <= 0:
            continue

        # Stock fills are bounded by bar volume × cap. Option fills aren't
        # easily volume-bounded (one bar's options volume isn't in our data),
        # so for now we fill them all in one bar with just cost adjustment.
        if instrument == "stock" and bar_volume > 0:
            absorb = bar_volume * cap
        else:
            absorb = remaining

        fill_qty = min(remaining, absorb)
        if fill_qty <= 0:
            continue

        # Per-slice slippage scales with the slice's notional — slicing
        # smaller orders meaningfully reduces total slippage.
        slip_bps = estimate_slippage_bps(fill_qty * ref_price, snapshot=snapshot)
        adj = side_sign * (spread_bps + slip_bps) / 1e4
        fill_price = ref_price * (1 + adj)

        slices.append(FillSlice(quantity=fill_qty * side_sign, price=fill_price,
                                  bar_index=i,
                                  cost_bps=round(spread_bps + slip_bps, 2)))
        remaining -= fill_qty

    filled = sum(abs(s.quantity) for s in slices)
    avg_price = (sum(abs(s.quantity) * s.price for s in slices) / filled
                  if filled > 0 else 0.0)
    partial = filled < requested - 1e-9
    if partial:
        notes.append(f"only {filled:.4f} of {requested:.4f} filled in "
                      f"{len(slices)} bar(s)")
    return FillResult(requested_quantity=requested, filled_quantity=filled,
                       avg_fill_price=avg_price, slices=slices,
                       partial=partial, bars_used=len(slices), notes=notes)


# ── multi-leg atomicity ────────────────────────────────────────────────────


def simulate_legs(
    legs: Sequence[Dict[str, Any]],
    *,
    atomicity_supported: bool,
    leg_fail_prob: float = 0.0,
    rng_seed: Optional[int] = None,
) -> AtomicityResult:
    """Simulate a spread fill across multiple legs.

    When ``atomicity_supported`` is True (Alpaca, IBKR), all legs either fill
    together or none do — no atomicity failure case.

    When ``atomicity_supported`` is False (Robinhood spreads), legs are
    submitted sequentially and each has ``leg_fail_prob`` of failing AFTER
    the previous already filled. That's the worst case for the trader: stuck
    with one leg of a hedge.
    """
    legs = list(legs)
    if not legs:
        return AtomicityResult(atomic=True, atomic_failure=False, legs=[])

    if atomicity_supported:
        results = [LegResult(leg=l, filled=True, reason="combo order") for l in legs]
        return AtomicityResult(atomic=True, atomic_failure=False, legs=results,
                                notes=["broker supports combo orders — atomic"])

    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
    results: List[LegResult] = []
    any_filled = False
    any_failed = False
    for l in legs:
        if rng.random() < max(0.0, min(1.0, leg_fail_prob)):
            results.append(LegResult(leg=l, filled=False,
                                       reason="leg fill failure (sequential broker)"))
            any_failed = True
        else:
            results.append(LegResult(leg=l, filled=True,
                                       reason="sequential fill ok"))
            any_filled = True

    return AtomicityResult(
        atomic=not (any_filled and any_failed),
        atomic_failure=(any_filled and any_failed),
        legs=results,
        notes=["sequential leg submission" if not (any_filled and any_failed)
                else "ATOMICITY FAILURE — some legs filled, others did not. "
                      "Position is now naked on the unfilled leg(s)."],
    )
