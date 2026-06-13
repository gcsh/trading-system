"""MITS-5 — Winner trajectory profile dataclass.

A `WinnerProfile` captures the AVERAGE trajectory of historical winners
for a (pattern, regime) cohort, plus the traits that characterize them
("held VWAP", "held the flag low", "saw IV expansion through the hold").

Built from the knowledge graph's `market_observations + market_outcomes`
by `profile_builder.build_winner_profile()`. Consumed by
`health_calculator.calculate_health()` to score an open position against
"what winners of this setup looked like during the hold".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# Canonical trait identifiers — keep stable; the health calculator and
# the agent's reason strings both reference them. New traits get appended
# here; never renamed (would invalidate historical profile caches).
TRAIT_HELD_VWAP = "held_vwap"
TRAIT_HELD_FLAG_LOW = "held_flag_low"
TRAIT_HELD_BOS_PIVOT = "held_bos_pivot"
TRAIT_HELD_PEAK_DRAWDOWN = "held_peak_drawdown"
TRAIT_IV_EXPANSION = "iv_expansion"
TRAIT_IV_COMPRESSION = "iv_compression"
TRAIT_HIT_PEAK_EARLY = "hit_peak_early"
# Trait set surfaced to the UI breakdown modal; keep in sync with
# `calculate_health` so we don't show traits with no checker.
KNOWN_TRAITS = (
    TRAIT_HELD_VWAP,
    TRAIT_HELD_FLAG_LOW,
    TRAIT_HELD_BOS_PIVOT,
    TRAIT_HELD_PEAK_DRAWDOWN,
    TRAIT_IV_EXPANSION,
    TRAIT_IV_COMPRESSION,
    TRAIT_HIT_PEAK_EARLY,
)


@dataclass
class WinnerProfile:
    """Average trajectory of historical winners for a (pattern, regime).

    Fields:
      pattern              — the detector slug (bull_flag, breakout, ...)
      regime               — the regime label at observation time
      sample_size          — count of winners aggregated
      avg_minutes_to_peak  — for winners, average bars-to-peak * bar_minutes
      avg_max_drawdown_during_hold — peak-to-trough during the hold
                              (negative number, e.g. -0.03 = -3%)
      common_traits        — traits that fired in >50% of winners
      trait_frequencies    — per-trait fraction of winners exhibiting it
                              (used by the calculator to weight each
                              trait by how characteristic it is)
      confidence           — 0-1, scales with sample_size (saturates at
                              ~100 winners).
    """
    pattern: str
    regime: str
    sample_size: int = 0
    avg_minutes_to_peak: float = 0.0
    avg_max_drawdown_during_hold: float = 0.0
    common_traits: List[str] = field(default_factory=list)
    trait_frequencies: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def is_trustworthy(self) -> bool:
        """Wrapper around the sample-size + confidence floor.

        Callers should treat `is_trustworthy=False` as "abstain — no
        signal to act on". The thesis-health agent flips to silent in
        that case.
        """
        return self.confidence >= 0.20 and self.sample_size >= 5
