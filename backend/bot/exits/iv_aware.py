"""Stage-10 item 13 — IV-aware DTE management for options.

When implied volatility is elevated AND a vol-crush event is imminent
(earnings 0-1 day out, OPEX week), the bot should TIGHTEN take-profit and
stop-loss bands. Two reasons:

  • Vol crush rips the time-value out of long options overnight — exiting
    earlier preserves more of the realized gain
  • Wider bands rely on theta-friendly markets that simply don't exist
    around scheduled vol events

The function is pure given inputs: snapshot fields (``iv_rank``,
``earnings_days``, ``opex_week``) → adjusted (tp_pct, sl_pct, reasoning).
Caller (strategy or engine) applies the adjusted bands.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class IVAdjustedExit:
    take_profit_pct: float
    stop_loss_pct: float
    tighten_factor: float = 1.0     # 1.0 = no change; <1.0 = tighter
    reasoning: List[str] = field(default_factory=list)
    crush_risk: str = "low"          # low | moderate | high

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _crush_risk(iv_rank: Optional[float], earnings_days: Optional[float],
                  opex_week: bool) -> str:
    """Categorize crush risk from snapshot fields."""
    high_iv = iv_rank is not None and float(iv_rank) >= 60
    earn_close = earnings_days is not None and 0 <= float(earnings_days) <= 1
    if (high_iv and earn_close) or (earn_close and opex_week):
        return "high"
    if high_iv or earn_close or opex_week:
        return "moderate"
    return "low"


def adjust_tp_sl_for_iv_crush(
    *,
    take_profit_pct: float,
    stop_loss_pct: float,
    iv_rank: Optional[float] = None,
    earnings_days: Optional[float] = None,
    opex_week: bool = False,
) -> IVAdjustedExit:
    """Tighten TP/SL bands proportional to IV-crush risk.

    Behaviour:
      • low risk     → no change
      • moderate     → multiply both bands by 0.70 (snap profits faster)
      • high         → multiply by 0.50 + add explicit reason line
    """
    risk = _crush_risk(iv_rank, earnings_days, opex_week)
    reasoning: List[str] = []
    factor = 1.0

    if risk == "high":
        factor = float(getattr(TUNABLES, "iv_aware_tighten_high", 0.50))
        reasoning.append(
            f"HIGH crush risk: IV rank {iv_rank} + earnings in "
            f"{earnings_days}d + opex={opex_week}"
        )
    elif risk == "moderate":
        factor = float(getattr(TUNABLES, "iv_aware_tighten_moderate", 0.70))
        flagged: List[str] = []
        if iv_rank is not None and float(iv_rank) >= 60:
            flagged.append(f"IV rank {iv_rank}")
        if earnings_days is not None and 0 <= float(earnings_days) <= 1:
            flagged.append(f"earnings in {earnings_days}d")
        if opex_week:
            flagged.append("OPEX week")
        reasoning.append(f"moderate crush risk: {', '.join(flagged)}")

    if factor != 1.0:
        reasoning.append(
            f"tightening bands by × {factor:.2f} "
            f"(TP {take_profit_pct:.2%} → {take_profit_pct * factor:.2%}; "
            f"SL {stop_loss_pct:.2%} → {stop_loss_pct * factor:.2%})"
        )
    else:
        reasoning.append("no IV-crush triggers — bands unchanged")

    return IVAdjustedExit(
        take_profit_pct=round(take_profit_pct * factor, 6),
        stop_loss_pct=round(stop_loss_pct * factor, 6),
        tighten_factor=factor,
        reasoning=reasoning,
        crush_risk=risk,
    )
