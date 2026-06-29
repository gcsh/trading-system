"""MITS Phase 5 (P5.3) — conviction-weighted position sizing for
EOD-bias-sourced trades.

Pure functions so they're trivially testable in isolation. The engine
calls ``apply_conviction_sizing`` after the regular RiskManager.evaluate
on EOD-bias trades. The multiplier is then applied to ``decision.quantity``.

Three knobs drive sizing:

  * ``rank``       (1-based position in the day's EOD analysis ranking)
  * ``high_conviction_open`` (count of already-open EOD-bias trades that
    qualified for the rank_1 or rank_2_3 multiplier — beyond the
    ``eod_max_concurrent_high_conviction`` cap, the next high-conviction
    trade collapses to rank_4_plus regardless of its true rank).
  * ``daily_notional_used`` (sum of notional from already-executed EOD-bias
    trades today). Capped at ``eod_max_daily_notional_pct`` × equity —
    trades that would push past the cap get truncated to whatever budget
    remains; if the cap is already exhausted, the multiplier returns 0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    multiplier: float
    notional_cap_remaining: float
    cap_reason: Optional[str] = None
    rank_tier: str = "rank_1"

    def to_dict(self) -> dict:
        return {
            "multiplier": round(float(self.multiplier), 4),
            "notional_cap_remaining": round(float(self.notional_cap_remaining), 2),
            "cap_reason": self.cap_reason,
            "rank_tier": self.rank_tier,
        }


def _rank_tier(rank: int, high_conviction_open: int) -> str:
    cap = int(getattr(TUNABLES, "eod_max_concurrent_high_conviction", 3))
    # Beyond the concurrent cap, force the rank_4_plus tier so we don't
    # stack the book in a single thesis no matter how attractive the
    # rank ordering says it is.
    if high_conviction_open >= cap and rank <= 3:
        return "rank_4_plus"
    if rank <= 1:
        return "rank_1"
    if rank <= 3:
        return "rank_2_3"
    return "rank_4_plus"


def conviction_multiplier(rank: int,
                              *,
                              high_conviction_open: int = 0) -> float:
    """Return the size multiplier for the given rank position."""
    tier = _rank_tier(int(rank or 999), int(high_conviction_open or 0))
    if tier == "rank_1":
        return float(getattr(TUNABLES, "eod_size_multiplier_rank_1", 1.5))
    if tier == "rank_2_3":
        return float(getattr(TUNABLES, "eod_size_multiplier_rank_2_3", 1.0))
    return float(getattr(TUNABLES, "eod_size_multiplier_rank_4_plus", 0.5))


def apply_conviction_sizing(*,
                                rank: int,
                                high_conviction_open: int,
                                daily_notional_used: float,
                                equity: float,
                                proposed_notional: float,
                                catalyst_multiplier: float = 1.0,
                                ) -> SizingResult:
    """Compute the final size multiplier + remaining notional headroom.

    Workflow:
      1. Resolve the rank tier and look up the base multiplier.
      2. Multiply by the catalyst_gate's conviction modifier (≤1.0
         shrinks size around earnings / FOMC; ==0 = abstain).
      3. Truncate so we never breach the daily notional cap.
    """
    cap_pct = float(getattr(TUNABLES, "eod_max_daily_notional_pct", 0.30))
    notional_cap = max(0.0, float(equity or 0.0) * cap_pct)
    remaining = max(0.0, notional_cap - float(daily_notional_used or 0.0))

    base = conviction_multiplier(rank, high_conviction_open=high_conviction_open)
    cm = catalyst_multiplier if catalyst_multiplier is not None else 1.0
    multiplier = float(base) * float(cm)
    if multiplier <= 0:
        return SizingResult(
            multiplier=0.0, notional_cap_remaining=remaining,
            cap_reason="catalyst_abstain",
            rank_tier=_rank_tier(int(rank or 999),
                                    int(high_conviction_open or 0)),
        )

    proposed_after_mult = float(proposed_notional or 0.0) * multiplier
    if proposed_after_mult <= remaining or remaining <= 0:
        # Either the trade fits in the remaining budget OR we have NO
        # budget left at all — the caller decides whether to skip when
        # remaining == 0.
        if remaining <= 0:
            return SizingResult(
                multiplier=0.0,
                notional_cap_remaining=0.0,
                cap_reason="daily_notional_cap_exhausted",
                rank_tier=_rank_tier(int(rank or 999),
                                          int(high_conviction_open or 0)),
            )
        return SizingResult(
            multiplier=round(multiplier, 4),
            notional_cap_remaining=remaining,
            cap_reason=None,
            rank_tier=_rank_tier(int(rank or 999),
                                      int(high_conviction_open or 0)),
        )

    # Truncate the multiplier so the order respects the cap.
    if proposed_notional > 0:
        truncated = remaining / float(proposed_notional)
    else:
        truncated = 0.0
    return SizingResult(
        multiplier=round(max(0.0, truncated), 4),
        notional_cap_remaining=remaining,
        cap_reason="daily_notional_cap_truncated",
        rank_tier=_rank_tier(int(rank or 999),
                                  int(high_conviction_open or 0)),
    )


@dataclass
class OpportunisticSizingResult:
    """MITS Phase 7.5 — inverted sizing on crisis-opportunity.

    Returned by :func:`opportunistic_sizing`. The discretionary layer's
    sizing decisions are intentionally separate from
    :class:`SizingResult` so the trial scorecard can attribute P&L back
    to the right layer.
    """
    multiplier: float
    notional_cap_remaining: float
    cap_reason: Optional[str] = None
    concurrency_limited: bool = False

    def to_dict(self) -> dict:
        return {
            "multiplier": round(float(self.multiplier), 4),
            "notional_cap_remaining": round(
                float(self.notional_cap_remaining), 2),
            "cap_reason": self.cap_reason,
            "concurrency_limited": self.concurrency_limited,
        }


# Regimes where the inverted (more aggressive) multiplier kicks in.
_CRISIS_REGIMES = {"panic", "capitulation", "squeeze"}
_TRENDING_REGIMES = {"trending_up", "trending_down"}


def opportunistic_multiplier(*,
                                conviction: float,
                                regime: str) -> float:
    """Pure size multiplier lookup for the opportunistic path.

    Crisis regimes (panic/capitulation/squeeze) with high conviction
    earn the FULL ``opportunistic_size_multiplier`` (default 2.0) —
    this is the directional inversion of the statistical layer's
    "shrink on crisis" default. Trending regimes get the smaller
    ``opportunistic_trending_size_multiplier`` (default 1.5). Everything
    else stays neutral at 1.0.
    """
    regime = (regime or "normal").lower()
    high_conv = float(getattr(
        TUNABLES, "opportunistic_high_conviction_threshold", 0.70))
    if conviction is None or float(conviction) < high_conv:
        return 1.0
    if regime in _CRISIS_REGIMES:
        return float(getattr(TUNABLES, "opportunistic_size_multiplier", 2.0))
    if regime in _TRENDING_REGIMES:
        return float(getattr(
            TUNABLES, "opportunistic_trending_size_multiplier", 1.5))
    return 1.0


def opportunistic_sizing(*,
                            conviction: float,
                            regime: str,
                            equity: float,
                            proposed_notional: float,
                            daily_notional_used: float = 0.0,
                            concurrent_open: int = 0,
                            catalyst_multiplier: float = 1.0,
                            ) -> OpportunisticSizingResult:
    """Full opportunistic sizing pass: multiplier × catalyst × caps.

    Caps:
      * Single trade notional ≤ ``opportunistic_max_single_notional_pct``
        × equity (default 0.50)
      * Daily total opportunistic notional ≤
        ``opportunistic_max_daily_notional_pct`` × equity (default 1.0)
      * Max ``opportunistic_max_concurrent`` open positions (default 3)
    """
    cap_pct = float(getattr(
        TUNABLES, "opportunistic_max_daily_notional_pct", 1.0))
    single_pct = float(getattr(
        TUNABLES, "opportunistic_max_single_notional_pct", 0.5))
    max_open = int(getattr(TUNABLES, "opportunistic_max_concurrent", 3))

    notional_cap_total = max(0.0, float(equity or 0.0) * cap_pct)
    single_cap = max(0.0, float(equity or 0.0) * single_pct)
    remaining = max(0.0, notional_cap_total - float(daily_notional_used or 0.0))

    # Concurrency cap.
    if concurrent_open >= max_open:
        return OpportunisticSizingResult(
            multiplier=0.0, notional_cap_remaining=remaining,
            cap_reason="opportunistic_max_concurrent_reached",
            concurrency_limited=True,
        )

    base = opportunistic_multiplier(conviction=conviction, regime=regime)
    cm = float(catalyst_multiplier if catalyst_multiplier is not None else 1.0)
    multiplier = base * cm
    if multiplier <= 0:
        return OpportunisticSizingResult(
            multiplier=0.0, notional_cap_remaining=remaining,
            cap_reason="catalyst_abstain",
        )

    proposed_after_mult = float(proposed_notional or 0.0) * multiplier

    # Daily cap exhausted.
    if remaining <= 0:
        return OpportunisticSizingResult(
            multiplier=0.0, notional_cap_remaining=0.0,
            cap_reason="opportunistic_daily_cap_exhausted",
        )

    # Per-trade cap: never let a single trade exceed single_cap.
    per_trade_target = min(remaining, single_cap)

    if proposed_after_mult > per_trade_target and float(proposed_notional) > 0:
        truncated = per_trade_target / float(proposed_notional)
        return OpportunisticSizingResult(
            multiplier=round(max(0.0, truncated), 4),
            notional_cap_remaining=remaining,
            cap_reason="opportunistic_single_notional_truncated",
        )

    return OpportunisticSizingResult(
        multiplier=round(multiplier, 4),
        notional_cap_remaining=remaining,
        cap_reason=None,
    )


__all__ = [
    "SizingResult", "conviction_multiplier", "apply_conviction_sizing",
    "OpportunisticSizingResult", "opportunistic_multiplier",
    "opportunistic_sizing",
]
