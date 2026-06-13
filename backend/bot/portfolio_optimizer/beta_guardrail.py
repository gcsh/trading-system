"""Stage-10 item 7 — net-beta guardrail under high-VIX regimes.

When the portfolio is already leveraged to the market (high net beta) AND
volatility is spiking, taking marginal trades amplifies drawdown
asymmetrically. The guardrail says: in those conditions, ONLY trade A or
A+ setups — let the bad-grade signals through and the equity curve bleeds
twice as fast.

Pure function — given the inputs, returns ``(min_grade_floor, reason)``.
Engine reads this and tightens its effective ``min_grade`` accordingly.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class BetaGuardrailDecision:
    triggered: bool
    min_grade_floor: Optional[str] = None
    reason: str = ""
    net_beta: float = 0.0
    vol_label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_beta_guardrail(*, net_beta: float,
                              vol_label: str,
                              beta_threshold: Optional[float] = None,
                              ) -> BetaGuardrailDecision:
    """Return a guardrail decision. When triggered, ``min_grade_floor`` is the
    minimum grade the engine should enforce ON TOP OF whatever the user
    configured.

    Default rules:
      • vol "spiking" + net_beta > 1.5 → require A+ only
      • vol "elevated" + net_beta > 1.5 → require A or better
      • vol "spiking" + net_beta > 1.0 → require A or better
      • everything else → no override
    """
    threshold = beta_threshold if beta_threshold is not None else float(
        getattr(TUNABLES, "beta_guardrail_threshold", 1.5)
    )
    vol = (vol_label or "").lower()
    triggered = False
    floor: Optional[str] = None
    reason = "no guardrail trigger"

    if vol == "spiking" and net_beta > threshold:
        triggered = True
        floor = "A+"
        reason = (f"net β {net_beta:.2f} > {threshold} AND vol spiking → "
                    f"A+ only")
    elif vol == "spiking" and net_beta > 1.0:
        triggered = True
        floor = "A"
        reason = (f"net β {net_beta:.2f} > 1.0 AND vol spiking → "
                    f"A or better")
    elif vol == "elevated" and net_beta > threshold:
        triggered = True
        floor = "A"
        reason = (f"net β {net_beta:.2f} > {threshold} AND vol elevated → "
                    f"A or better")

    return BetaGuardrailDecision(
        triggered=triggered, min_grade_floor=floor, reason=reason,
        net_beta=round(float(net_beta), 4), vol_label=vol_label or "",
    )
