"""Stage-10 adaptive min_grade — auto-lower when calibration drifts.

When the live ECE rises above the gate threshold (Stage 1.5 contract: 0.05),
the calibration we trained on is no longer accurate. Until a retrain
restores the contract, automatically TIGHTEN the engine's ``min_grade`` so
we trade fewer, higher-confidence setups while the model catches up.

Mapping (more conservative as ECE worsens):
  • ECE ≤ 0.05         — no change; honor configured ``min_grade``
  • 0.05 < ECE ≤ 0.08  — raise floor to at least "B" (filter Cs)
  • 0.08 < ECE ≤ 0.12  — raise floor to at least "A" (filter Bs + Cs)
  • ECE > 0.12         — raise floor to "A+" (only the strongest setups)

The function is pure given inputs; the engine reads it on every cycle so
the floor adapts within ~30 s of a fresh metrics snapshot.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

GRADE_ORDER = ["A+", "A", "B", "C", "D"]


def _at_least(current: Optional[str], minimum: str) -> str:
    """Return the more conservative of ``current`` (or top of ladder if None)
    and ``minimum``. ``minimum`` is treated as the floor — anything weaker
    gets bumped up to it."""
    if current is None:
        return minimum
    try:
        cur_idx = GRADE_ORDER.index(current)
        min_idx = GRADE_ORDER.index(minimum)
    except ValueError:
        return minimum
    # smaller index = more conservative (A+ is index 0)
    return current if cur_idx <= min_idx else minimum


def adaptive_min_grade(*, configured_min_grade: Optional[str],
                         calibration_error: Optional[float],
                         brier: Optional[float] = None) -> str:
    """Compute the engine's effective ``min_grade`` for this cycle.

    Returns the configured floor when calibration is healthy; tightens it
    when ECE breaches the bands above. Brier is a tie-breaker — if Brier is
    bad we still tighten even when ECE is acceptable.
    """
    floor = configured_min_grade or "C"
    if calibration_error is None:
        return floor
    ece = float(calibration_error)
    if brier is not None and float(brier) > 0.25 and ece > 0.04:
        # Brier worse than coin-flip + drifting calibration → strongest only
        return _at_least(floor, "A+")
    if ece > 0.12:
        return _at_least(floor, "A+")
    if ece > 0.08:
        return _at_least(floor, "A")
    if ece > 0.05:
        return _at_least(floor, "B")
    return floor
