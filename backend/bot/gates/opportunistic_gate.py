"""MITS Phase 7.3 — Opportunistic trade gate.

Vets an :class:`OpportunityHypothesis` for execution. This is the
DISCRETIONARY path — it deliberately accepts lower posteriors than the
statistical gate (``opportunistic_posterior_floor`` default 0.45 vs the
statistical layer's 0.60). On crisis days the statistical layer is too
cautious; the opportunistic gate is what flips the cautious behavior
into convex-payoff hunting.

DTE selection respects the regime:

  * ``panic`` / ``capitulation`` → 0DTE or 1DTE put preference
  * ``squeeze``                  → 0DTE or 1DTE call preference
  * ``trending_up/down``         → 3-5 DTE directional

Every opportunistic trade is marked ``must_exit_by_eod=True`` so the
executor's daily-close sweep closes it even if no other exit triggers.
Stop-loss is dynamic based on current ATR-30m rather than the historical
fixed-percent default — crisis vol means a 5% stop is meaningless.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


_CRISIS_REGIMES = {"panic", "capitulation"}
_SQUEEZE_REGIMES = {"squeeze"}
_TRENDING_REGIMES = {"trending_up", "trending_down"}


@dataclass
class OpportunisticGateResult:
    passes: bool = True
    reason: Optional[str] = None
    dte: int = 1  # concrete chosen DTE
    dte_bucket: str = "1d"
    instrument: str = "option"
    side: str = "long_put"  # long_put | long_call | iron_condor | long_straddle
    posterior_floor: float = 0.45
    must_exit_by_eod: bool = True
    stop_loss_pct: Optional[float] = None  # dynamic ATR-based stop
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passes": self.passes,
            "reason": self.reason,
            "dte": self.dte,
            "dte_bucket": self.dte_bucket,
            "instrument": self.instrument,
            "side": self.side,
            "posterior_floor": round(float(self.posterior_floor), 4),
            "must_exit_by_eod": self.must_exit_by_eod,
            "stop_loss_pct": (round(float(self.stop_loss_pct), 4)
                                  if self.stop_loss_pct is not None else None),
            "notes": list(self.notes),
        }


def _choose_dte(regime: str, hypothesis_bucket: str) -> tuple[int, str]:
    """Concrete (dte, bucket_label) chosen for the order. Crisis regimes
    cap at the operator-tunable ``opportunistic_crisis_dte_max`` (default 1).
    Trending regimes select within ``opportunistic_trending_dte_min..max``.
    """
    bucket = (hypothesis_bucket or "").lower()
    if regime in _CRISIS_REGIMES or regime in _SQUEEZE_REGIMES:
        max_dte = int(TUNABLES.opportunistic_crisis_dte_max)
        if "0d" in bucket:
            return 0, "0d"
        return min(1, max_dte), "1d"
    if regime in _TRENDING_REGIMES:
        lo = int(TUNABLES.opportunistic_trending_dte_min)
        hi = int(TUNABLES.opportunistic_trending_dte_max)
        # Honor explicit hypothesis bucket if it falls within range.
        if "3-5" in bucket:
            return max(lo, min(hi, 4)), "3-5d"
        if "7-14" in bucket:
            return 7, "7-14d"
        return max(lo, min(hi, 4)), "3-5d"
    # Default: 1d
    return 1, "1d"


def _side_for(regime: str, hypothesis_direction: str) -> str:
    direction = (hypothesis_direction or "").lower()
    if direction in {"long_put", "long_call", "iron_condor",
                        "long_straddle"}:
        return direction
    # Fallback from regime when the Brain didn't fill the field.
    if regime in _CRISIS_REGIMES:
        return "long_put"
    if regime in _SQUEEZE_REGIMES:
        return "long_call"
    if regime == "trending_up":
        return "long_call"
    if regime == "trending_down":
        return "long_put"
    return "long_call"


def _instrument_for(side: str) -> str:
    if side in {"iron_condor", "long_straddle"}:
        return "spread"
    return "option"


def _dynamic_stop_pct(context: Dict[str, Any]) -> Optional[float]:
    """ATR-30m based stop. ``context`` is the engine event-style dict;
    we look for ``atr_30m`` or ``atr`` and a current price to compute a
    percent. Returns None when neither is available so the executor's
    default stop policy applies."""
    try:
        atr = (context.get("atr_30m")
               or context.get("atr")
               or (context.get("snapshot") or {}).get("atr"))
        price = (context.get("price")
                 or (context.get("snapshot") or {}).get("price"))
        if not atr or not price or float(price) <= 0:
            return None
        mult = float(TUNABLES.opportunistic_atr_stop_multiplier)
        return round(float(atr) * mult / float(price) * 100.0, 3)
    except Exception:
        return None


def vet(hypothesis: Any, context: Optional[Dict[str, Any]] = None,
          *, regime_state: Optional[str] = None,
        ) -> OpportunisticGateResult:
    """Run the opportunistic vetting on a hypothesis.

    Pulling ``hypothesis`` as ``Any`` lets the gate accept both
    ``OpportunityHypothesis`` instances and plain dicts (handy for
    tests + json round-trips).
    """
    context = context or {}
    regime = (regime_state
              or getattr(hypothesis, "regime_state", None)
              or context.get("regime_state")
              or "normal").lower()

    direction = (getattr(hypothesis, "direction", None)
                 or (hypothesis.get("direction")
                     if isinstance(hypothesis, dict) else None)
                 or "skip")
    conviction = float(
        getattr(hypothesis, "conviction", None)
        if not isinstance(hypothesis, dict)
        else hypothesis.get("conviction") or 0.0
    )
    hypothesis_bucket = (
        getattr(hypothesis, "dte_bucket", None)
        if not isinstance(hypothesis, dict)
        else hypothesis.get("dte_bucket") or "1d"
    ) or "1d"

    floor = float(TUNABLES.opportunistic_posterior_floor)
    notes: List[str] = []

    # Skip-direction means the Brain saw the tape and recommended no
    # opportunity — the gate respects that.
    if direction == "skip":
        return OpportunisticGateResult(
            passes=False,
            reason="opportunity brain returned direction=skip",
            posterior_floor=floor,
        )

    # Conviction below the opportunistic posterior floor.
    if conviction < floor:
        return OpportunisticGateResult(
            passes=False,
            reason=(f"conviction {conviction:.2f} below "
                       f"opportunistic floor {floor:.2f}"),
            posterior_floor=floor,
        )

    side = _side_for(regime, direction)
    dte, bucket = _choose_dte(regime, hypothesis_bucket)
    instrument = _instrument_for(side)
    stop_pct = _dynamic_stop_pct(context)
    if stop_pct is None:
        notes.append("no ATR-30m available — executor default stop applies")
    else:
        notes.append(
            f"dynamic stop: {TUNABLES.opportunistic_atr_stop_multiplier:.1f}× "
            f"ATR-30m → {stop_pct:.2f}%"
        )
    notes.append("must_exit_by_eod=True (daily-close sweep enforces)")

    return OpportunisticGateResult(
        passes=True,
        reason=None,
        dte=dte,
        dte_bucket=bucket,
        instrument=instrument,
        side=side,
        posterior_floor=floor,
        must_exit_by_eod=True,
        stop_loss_pct=stop_pct,
        notes=notes,
    )


__all__ = ["OpportunisticGateResult", "vet"]
