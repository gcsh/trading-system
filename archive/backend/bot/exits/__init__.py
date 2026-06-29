"""Stage-10 staged exit policies — TP1 partial + ATR trail + time stop.

Replaces the binary take-profit / stop-loss model with three composable
policies that, applied together, give the bot the "let winners run" + "cut
linger-and-die" behaviour institutional desks rely on:

  1. **StagedExitPolicy**     — TP1 partial-close (default 50%) at the
     conventional take-profit level; remainder runs under the trailing rule.
  2. **ATRTrailPolicy**       — trailing stop at ``trail_multiplier × ATR``
     under the position's high-water mark.
  3. **TimeStopPolicy**       — flatten if MFE < ``min_mfe_pct`` after
     ``max_hold_minutes`` (the "linger and die" trades that drag accuracy).

Each policy returns an ``ExitAction`` describing what to do next. The
combiner ``evaluate_policies`` picks the most aggressive applicable action
(time stop > stop loss > TP1 > trail > hold) so the manager always errs
toward closing.

State per position is stored in a small dict (``ExitState``) the engine
persists alongside the trade — no DB schema change needed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── state + actions ──────────────────────────────────────────────────────


@dataclass
class ExitState:
    """Per-position exit bookkeeping. Lives in PaperPosition.meta or
    Trade.detail_json so it survives restart."""
    tp1_taken: bool = False
    tp1_qty: float = 0.0
    high_water_price: float = 0.0
    opened_at: Optional[str] = None
    last_mfe_pct: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExitAction:
    """What the manager should do this cycle."""
    action: str          # "hold" | "tp1_partial" | "trail_close" | "time_stop" | "stop_loss"
    close_fraction: float = 0.0   # 0.0 = hold; 1.0 = close all; 0.5 = TP1
    reason: str = ""
    new_state: Optional[ExitState] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "close_fraction": self.close_fraction,
                 "reason": self.reason,
                 "new_state": self.new_state.to_dict() if self.new_state else None}


# ── individual policies ──────────────────────────────────────────────────


def stop_loss_policy(*, entry_price: float, current_price: float,
                       stop_pct: float, state: ExitState) -> Optional[ExitAction]:
    """Hard stop. Closes 100% of the REMAINING quantity (so if TP1 already
    fired, this only closes the trailing half)."""
    if stop_pct <= 0 or entry_price <= 0 or current_price <= 0:
        return None
    drop_pct = (current_price - entry_price) / entry_price
    if drop_pct <= -stop_pct:
        return ExitAction(
            action="stop_loss",
            close_fraction=1.0,        # full remainder
            reason=f"stop-loss hit: {drop_pct * 100:.1f}% ≤ -{stop_pct * 100:.0f}%",
            new_state=state,
        )
    return None


def staged_tp_policy(*, entry_price: float, current_price: float,
                       take_profit_pct: float, state: ExitState,
                       tp1_fraction: Optional[float] = None
                       ) -> Optional[ExitAction]:
    """TP1: take ``tp1_fraction`` (default 50%) out at the conventional TP
    level; leave the rest under the trailing rule."""
    tp1_frac = tp1_fraction if tp1_fraction is not None else float(
        getattr(TUNABLES, "staged_exit_tp1_fraction", 0.5)
    )
    if state.tp1_taken or take_profit_pct <= 0 or entry_price <= 0:
        return None
    gain_pct = (current_price - entry_price) / entry_price
    if gain_pct >= take_profit_pct:
        new_state = ExitState(**state.to_dict())
        new_state.tp1_taken = True
        new_state.high_water_price = max(state.high_water_price, current_price)
        new_state.notes.append(f"TP1 fired @ {gain_pct * 100:.1f}%")
        return ExitAction(
            action="tp1_partial",
            close_fraction=tp1_frac,
            reason=f"TP1 hit: +{gain_pct * 100:.1f}% ≥ {take_profit_pct * 100:.0f}%; "
                     f"taking {tp1_frac * 100:.0f}% off",
            new_state=new_state,
        )
    return None


def atr_trail_policy(*, current_price: float, atr: float, state: ExitState,
                       trail_multiplier: Optional[float] = None
                       ) -> Optional[ExitAction]:
    """Trail the remainder after TP1. Closes when price falls
    ``trail_multiplier × atr`` below the high-water mark.

    Only active AFTER TP1 has fired (the runner half).
    """
    mult = trail_multiplier if trail_multiplier is not None else float(
        getattr(TUNABLES, "staged_exit_trail_atr_mult", 2.0)
    )
    if not state.tp1_taken or atr <= 0 or current_price <= 0:
        return None
    new_state = ExitState(**state.to_dict())
    new_state.high_water_price = max(state.high_water_price, current_price)

    trail_stop = new_state.high_water_price - mult * atr
    if current_price <= trail_stop:
        return ExitAction(
            action="trail_close",
            close_fraction=1.0,            # close the runner
            reason=f"ATR trail hit: price {current_price:.2f} ≤ "
                     f"HWM {new_state.high_water_price:.2f} − "
                     f"{mult:.1f}×ATR ({trail_stop:.2f})",
            new_state=new_state,
        )
    # No close, but we still want to persist the updated HWM
    return ExitAction(action="hold", close_fraction=0.0,
                         reason="trail watching", new_state=new_state)


def time_stop_policy(*, entry_price: float, current_price: float,
                       state: ExitState,
                       now: Optional[datetime] = None,
                       max_hold_minutes: Optional[int] = None,
                       min_mfe_pct: Optional[float] = None
                       ) -> Optional[ExitAction]:
    """Flatten if MFE < ``min_mfe_pct`` after ``max_hold_minutes``. Catches
    the "linger and die" trades that bleed the equity curve."""
    max_min = max_hold_minutes if max_hold_minutes is not None else int(
        getattr(TUNABLES, "staged_exit_time_stop_minutes", 240)
    )
    min_mfe = min_mfe_pct if min_mfe_pct is not None else float(
        getattr(TUNABLES, "staged_exit_min_mfe_pct", 0.005)
    )
    if not state.opened_at or entry_price <= 0:
        return None
    try:
        opened = datetime.fromisoformat(state.opened_at)
    except Exception:
        return None
    now = now or datetime.utcnow()
    held_min = (now - opened).total_seconds() / 60.0
    if held_min < max_min:
        return None
    # Highest MFE seen vs entry; track via state.last_mfe_pct
    mfe_pct = max(state.last_mfe_pct,
                   (current_price - entry_price) / entry_price)
    if mfe_pct < min_mfe:
        new_state = ExitState(**state.to_dict())
        new_state.last_mfe_pct = mfe_pct
        return ExitAction(
            action="time_stop",
            close_fraction=1.0,
            reason=f"time stop: held {held_min:.0f} min, max MFE "
                     f"{mfe_pct * 100:.2f}% < min {min_mfe * 100:.2f}%",
            new_state=new_state,
        )
    return None


# ── combiner ─────────────────────────────────────────────────────────────


def evaluate_policies(*,
                        entry_price: float,
                        current_price: float,
                        stop_pct: float,
                        take_profit_pct: float,
                        atr: float = 0.0,
                        state: Optional[ExitState] = None,
                        now: Optional[datetime] = None,
                        max_hold_minutes: Optional[int] = None,
                        ) -> ExitAction:
    """Apply policies in priority order; return the first non-hold action.

    Priority (most aggressive first):
      time_stop > stop_loss > tp1_partial > trail_close > hold

    The combiner always returns an ExitAction (never None) so callers can
    rely on .action.
    """
    state = state or ExitState()
    # Always update HWM
    if state.high_water_price < current_price:
        state.high_water_price = current_price
    # Always update MFE
    if entry_price > 0:
        cur_mfe = (current_price - entry_price) / entry_price
        if cur_mfe > state.last_mfe_pct:
            state.last_mfe_pct = cur_mfe

    for policy in (
        lambda: time_stop_policy(entry_price=entry_price, current_price=current_price,
                                    state=state, now=now,
                                    max_hold_minutes=max_hold_minutes),
        lambda: stop_loss_policy(entry_price=entry_price, current_price=current_price,
                                    stop_pct=stop_pct, state=state),
        lambda: staged_tp_policy(entry_price=entry_price, current_price=current_price,
                                    take_profit_pct=take_profit_pct, state=state),
        lambda: atr_trail_policy(current_price=current_price, atr=atr, state=state),
    ):
        result = policy()
        if result is None:
            continue
        if result.action != "hold":
            return result
    return ExitAction(action="hold", close_fraction=0.0,
                         reason="all policies hold", new_state=state)
