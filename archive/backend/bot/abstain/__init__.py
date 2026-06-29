"""Stage-9 selective-abstention gate.

The engine's run_cycle is full of "hard" gates (audit, event-risk, kill
switch). Those say "no, never". Abstain logic is the "soft" gate that says
"this signal is too marginal to act on — log it for cohort analysis but
don't fire an order".

Three rules, applied in order:

  1. **No-trade confidence band** — if calibrated probability is in
     ``[band_lo, band_hi]`` (default 0.50–0.58) AND the relative
     execution cost exceeds a threshold, force REJECT. This catches the
     "edge ≤ cost" trap that bleeds the equity curve.

  2. **Regime-transition strictness** — when the cross-asset regime flipped
     recently OR the snapshot reports a "transition" state, demand higher
     confluence + tighter spread thresholds. Most losses cluster around
     these flips.

  3. **Recent cohort floor** — if the (strategy × regime) cohort win-rate
     over the last N closed trades is below a floor, throttle: clip size,
     or in the worst case convert the BUY into a "monitor-only" event.

The result is a structured ``AbstainDecision`` the engine treats like an
event-risk hold, so the existing emit path + cohort analysis just work.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class AbstainDecision:
    abstain: bool = False
    reasons: List[str] = field(default_factory=list)
    size_multiplier: float = 1.0          # final mult applied to recommended size
    monitor_only: bool = False             # if True, do NOT submit any order
    triggered_rules: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── rule 1: no-trade confidence band ─────────────────────────────────────


def in_no_trade_band(probability: float,
                       *, lo: float = 0.50, hi: float = 0.58) -> bool:
    """True when ``probability`` is inside the no-trade band."""
    if probability is None:
        return False
    return lo <= float(probability) <= hi


def cost_exceeds_edge(*, probability: float, expected_move_pct: Optional[float],
                        total_cost_bps: float, edge_factor: float = 0.5) -> bool:
    """Approximate test: when the probability is in the marginal band and
    round-trip cost in bps exceeds ``edge_factor × |probability − 0.5| × expected_move_bps``,
    the edge isn't economic. Returns True ⇒ should abstain."""
    if probability is None or total_cost_bps <= 0:
        return False
    move_bps = float(expected_move_pct or 0.0) * 1e4 if expected_move_pct else 100.0
    edge_bps = abs(float(probability) - 0.5) * 2 * move_bps
    return total_cost_bps > edge_factor * edge_bps


# ── rule 2: regime-transition strictness ──────────────────────────────────


def is_regime_transition(regime_label: Optional[str],
                           snapshot: Optional[Dict[str, Any]] = None) -> bool:
    """True when the cross-asset regime is in flux (any 'mixed' / 'transition'
    label OR the snapshot tags it explicitly)."""
    snapshot = snapshot or {}
    if snapshot.get("regime_transition"):
        return True
    if not regime_label:
        return False
    label = str(regime_label).lower()
    return any(token in label for token in
                 ("mixed", "transition", "rally_with_fear", "tighten_pressure"))


# ── rule 3: recent cohort floor ──────────────────────────────────────────


def cohort_below_floor(*, cohort_win_rate: Optional[float],
                          cohort_closed: int,
                          floor: float = 0.40,
                          min_sample: int = 10) -> bool:
    """True when we have enough cohort samples AND the rolling win rate has
    fallen below ``floor``."""
    if cohort_closed < min_sample or cohort_win_rate is None:
        return False
    return cohort_win_rate < floor


# ── orchestrator ─────────────────────────────────────────────────────────


def abstain_and_throttle(
    *,
    action: str,
    probability: Optional[float] = None,
    expected_move_pct: Optional[float] = None,
    total_cost_bps: float = 0.0,
    regime_label: Optional[str] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    cohort_win_rate: Optional[float] = None,
    cohort_closed: int = 0,
    band_lo: Optional[float] = None,
    band_hi: Optional[float] = None,
) -> AbstainDecision:
    """Run every rule against the signal context and combine into an
    ``AbstainDecision``. Engine treats ``abstain=True`` like event-risk
    holds — the BUY is converted to a ``status="abstain"`` event.

    18-FU Gap R1 — ``band_lo`` / ``band_hi`` override
    ``TUNABLES.abstain_band_lo`` / ``TUNABLES.abstain_band_hi`` when the
    caller has resolved an operator-approved threshold via
    ``backend.bot.learning.policy_apply.resolve_threshold``. ``None``
    keeps the legacy TUNABLES lookup (back-compat for tests + any
    caller that doesn't thread the override).
    """
    decision = AbstainDecision()
    if not action.startswith("BUY"):
        return decision         # abstain only applies to opening orders

    if band_lo is None:
        band_lo = float(getattr(TUNABLES, "abstain_band_lo", 0.50))
    else:
        band_lo = float(band_lo)
    if band_hi is None:
        band_hi = float(getattr(TUNABLES, "abstain_band_hi", 0.58))
    else:
        band_hi = float(band_hi)

    # Rule 1
    if in_no_trade_band(probability, lo=band_lo, hi=band_hi) and \
       cost_exceeds_edge(probability=probability,
                            expected_move_pct=expected_move_pct,
                            total_cost_bps=total_cost_bps):
        decision.abstain = True
        decision.monitor_only = True
        decision.reasons.append(
            f"calibrated p={probability:.2f} in no-trade band [{band_lo:.2f},"
            f"{band_hi:.2f}] and cost {total_cost_bps:.1f}bps exceeds edge"
        )
        decision.triggered_rules.append("no_trade_band")

    # Rule 2 — regime transition: do not abstain outright, but throttle
    if is_regime_transition(regime_label, snapshot):
        decision.size_multiplier *= float(getattr(
            TUNABLES, "abstain_transition_size_mult", 0.5
        ))
        decision.reasons.append(
            f"regime transition '{regime_label}' — size × "
            f"{decision.size_multiplier:.2f}"
        )
        decision.triggered_rules.append("regime_transition")

    # Rule 3 — cohort floor: throttle or monitor-only depending on severity
    floor = float(getattr(TUNABLES, "abstain_cohort_floor", 0.40))
    if cohort_below_floor(cohort_win_rate=cohort_win_rate,
                            cohort_closed=cohort_closed,
                            floor=floor):
        # If the cohort is REALLY bad, full monitor-only; otherwise size × 0.5
        if cohort_win_rate is not None and cohort_win_rate < floor - 0.10:
            decision.abstain = True
            decision.monitor_only = True
            decision.reasons.append(
                f"cohort win rate {cohort_win_rate:.0%} ≪ floor {floor:.0%} "
                f"(closed={cohort_closed}) — monitor only"
            )
        else:
            decision.size_multiplier *= 0.5
            decision.reasons.append(
                f"cohort win rate {cohort_win_rate:.0%} < floor {floor:.0%} "
                f"(closed={cohort_closed}) — size × {decision.size_multiplier:.2f}"
            )
        decision.triggered_rules.append("cohort_floor")

    return decision
