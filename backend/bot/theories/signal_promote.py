"""MITS-P10.2 — Stock → Option signal promotion.

When a theory emits a stock BUY/SELL we ask: should this be expressed
through the option chain instead? The decision is IV-rank +
catalyst-distance + regime aware.

References:

  * Sheldon Natenberg, *Option Volatility & Pricing* (McGraw-Hill, 2nd
    ed. 1994) — Chapter 6 "Volatility Strategies": when implied
    volatility is BELOW its historical range, long premium (naked
    calls / puts) wins; when ABOVE, short premium (verticals, iron
    condors) wins. The IV-rank thresholds (30 / 70) used below are
    Natenberg's "low / mid / high" rule of thumb.
  * Larry McMillan, *Options as a Strategic Investment* (NYIF, 5th ed.
    2012) — the catalyst rule: never sell premium into earnings; never
    buy premium that decays through earnings unless the move is the
    thesis. 14-day buffer used below is McMillan's "earnings window".
  * tastytrade research notes (Tom Sosnoff / Tony Battista, 2015) —
    the modern 30 / 70 IV-rank dichotomy adopted by retail brokerages.

Usage:

  >>> from backend.bot.theories.signal_promote import promote
  >>> sig = promote(stock_buy_signal,
  ...               market_context={"iv_rank": 22, "days_to_earnings": 30,
  ...                               "regime": "trending"})
  >>> sig.action
  'BUY_CALL'

The promote() function is *opt-in* — each theory's analyze() passes
``params.get("promote_options", True)`` to decide whether to wrap the
signal.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional

from .schema import Signal


# IV-rank dichotomy — Natenberg / tastytrade convention.
IV_LOW_THRESHOLD = 30.0
IV_HIGH_THRESHOLD = 70.0
# McMillan earnings buffer — beyond this, premium decay assumptions hold.
EARNINGS_BUFFER_DAYS = 14
# Default DTE windows.
DTE_LONG_PREMIUM = 30          # naked calls / puts when IV is cheap.
DTE_DEFINED_RISK = 21          # verticals / iron condors when IV is rich.


def promote(
    signal: Signal,
    market_context: Optional[Dict[str, Any]] = None,
    *,
    enabled: bool = True,
) -> Signal:
    """Return an option-promoted signal when conditions favour, else
    return the underlying stock signal unchanged.

    The decision matrix:

        action  | IV rank | days_to_earnings | promotion
        ------- | ------- | ---------------- | -------------------
        BUY     |  < 30   |      > 14        | BUY_CALL (long DTE)
        BUY     |  > 70   |      > 14        | BUY_VERTICAL_CALL
        BUY     |  any    |      ≤ 14        | (stay stock — no IV bets near earnings)
        SELL    |  < 30   |      > 14        | BUY_PUT
        SELL    |  > 70   |      > 14        | IRON_CONDOR (range bet)
        SELL    |  any    |      ≤ 14        | (stay stock)
        other   |  any    |      any         | (untouched)
    """
    if not enabled:
        return signal
    if signal.action not in ("BUY", "SELL"):
        return signal
    ctx = dict(market_context or {})
    iv_rank = float(ctx.get("iv_rank") if ctx.get("iv_rank") is not None else 50.0)
    dte_earn = float(
        ctx.get("days_to_earnings")
        if ctx.get("days_to_earnings") is not None
        else 365.0
    )

    # Catalyst gate — McMillan: stay stock through earnings.
    if dte_earn <= EARNINGS_BUFFER_DAYS:
        return signal

    if signal.action == "BUY":
        if iv_rank < IV_LOW_THRESHOLD:
            return replace(
                signal,
                action="BUY_CALL",
                instrument="call",
                dte_target=DTE_LONG_PREMIUM,
                strike=signal.price,
                reasoning=(
                    signal.reasoning
                    + f"  Promoted to long call — IV-rank {iv_rank:.0f} is "
                      "below the Natenberg 30 threshold, so premium is "
                      "cheap and long-vega exposure pays."
                ),
                theory_anchor={
                    **(signal.theory_anchor or {}),
                    "promotion": {"iv_rank": iv_rank, "rationale": "low_iv_long_call"},
                },
            )
        if iv_rank > IV_HIGH_THRESHOLD:
            return replace(
                signal,
                action="BUY_VERTICAL_CALL",
                instrument="spread",
                dte_target=DTE_DEFINED_RISK,
                strike=signal.price,
                reasoning=(
                    signal.reasoning
                    + f"  Promoted to vertical call spread — IV-rank "
                      f"{iv_rank:.0f} is above 70, so naked premium is "
                      "expensive; the spread defines max loss and finances "
                      "part of the long leg with a short higher strike."
                ),
                theory_anchor={
                    **(signal.theory_anchor or {}),
                    "promotion": {"iv_rank": iv_rank,
                                  "rationale": "high_iv_vertical_call"},
                },
            )
        return signal

    if signal.action == "SELL":
        if iv_rank < IV_LOW_THRESHOLD:
            return replace(
                signal,
                action="BUY_PUT",
                instrument="put",
                dte_target=DTE_LONG_PREMIUM,
                strike=signal.price,
                reasoning=(
                    signal.reasoning
                    + f"  Promoted to long put — IV-rank {iv_rank:.0f} is "
                      "below 30, so puts are cheap and long-vega "
                      "exposure on a downside thesis pays."
                ),
                theory_anchor={
                    **(signal.theory_anchor or {}),
                    "promotion": {"iv_rank": iv_rank, "rationale": "low_iv_long_put"},
                },
            )
        if iv_rank > IV_HIGH_THRESHOLD:
            return replace(
                signal,
                action="IRON_CONDOR",
                instrument="spread",
                dte_target=DTE_DEFINED_RISK,
                strike=signal.price,
                reasoning=(
                    signal.reasoning
                    + f"  Promoted to iron condor — IV-rank {iv_rank:.0f} "
                      "is above 70 and the bearish thesis is range-bound; "
                      "sell both wings to collect rich premium with "
                      "defined max loss."
                ),
                theory_anchor={
                    **(signal.theory_anchor or {}),
                    "promotion": {"iv_rank": iv_rank,
                                  "rationale": "high_iv_iron_condor"},
                },
            )
        return signal

    return signal


def promote_all(
    signals,
    market_context: Optional[Dict[str, Any]] = None,
    *,
    enabled: bool = True,
):
    """Bulk-apply ``promote()`` over a list of signals."""
    return [promote(s, market_context, enabled=enabled) for s in (signals or [])]


__all__ = ["promote", "promote_all",
           "IV_LOW_THRESHOLD", "IV_HIGH_THRESHOLD",
           "EARNINGS_BUFFER_DAYS",
           "DTE_LONG_PREMIUM", "DTE_DEFINED_RISK"]
