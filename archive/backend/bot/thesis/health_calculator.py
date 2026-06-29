"""MITS-5 — thesis-health calculator.

`calculate_health(open_position, current_bars, winner_profile)`
returns a `ThesisHealth` score (0-100) measuring how well the live
position's trajectory matches the historical winner profile.

Per-trait check (each weighted by the trait's frequency in winners):

  held_vwap          — current price > current session VWAP
  held_flag_low      — current price > stored flag_low (if any)
  held_bos_pivot     — current price > stored BOS pivot (if any)
  held_peak_drawdown — current drawdown from peak premium <= winner avg
  iv_expansion       — current_iv >= entry_iv (winner expected expansion)
  iv_compression     — current_iv <= entry_iv (winner expected compression)
  hit_peak_early     — peak premium achieved within
                         `winner_profile.avg_minutes_to_peak`

Score is the weighted sum of intact traits divided by the weighted sum
of applicable (winner-defining) traits. Multiplied by
`winner_profile.confidence` so a thin-corpus profile produces a softer
score (closer to 50). Returned as a 0-100 number.

Failure modes (return abstain-shaped result):
  - winner_profile is None or untrustworthy
  - open_position has no entry_price / current_price
  - no traits applicable

This module is intentionally pure: zero DB calls, zero network. The
caller (engine + agent) supplies the snapshot. Easy to unit-test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.thesis.winner_profile import (
    KNOWN_TRAITS,
    TRAIT_HELD_BOS_PIVOT,
    TRAIT_HELD_FLAG_LOW,
    TRAIT_HELD_PEAK_DRAWDOWN,
    TRAIT_HELD_VWAP,
    TRAIT_HIT_PEAK_EARLY,
    TRAIT_IV_COMPRESSION,
    TRAIT_IV_EXPANSION,
    WinnerProfile,
)


@dataclass
class ThesisHealth:
    score: float                              # 0-100
    reason: str
    intact_traits: List[str] = field(default_factory=list)
    degraded_traits: List[str] = field(default_factory=list)
    abstain: bool = False                    # True ⇒ no signal
    profile_sample_size: int = 0
    profile_confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _check_trait(trait: str, position: Dict[str, Any],
                       bars: Optional[Any] = None) -> Optional[bool]:
    """Return True/False if the trait is intact/degraded, or None when
    the data needed to evaluate it isn't present (e.g. no flag_low on
    the position — the trait is "not applicable", not degraded)."""
    if not position:
        return None
    cur = position.get("current_price") or position.get("mark")
    try:
        cur = float(cur) if cur is not None else None
    except Exception:
        cur = None

    if trait == TRAIT_HELD_VWAP:
        vwap = position.get("vwap") or position.get("current_vwap")
        try:
            vwap = float(vwap) if vwap is not None else None
        except Exception:
            vwap = None
        if cur is None or vwap is None:
            return None
        return cur > vwap

    if trait == TRAIT_HELD_FLAG_LOW:
        # PosMeta may carry flag_low from the original detector hit.
        flag_low = (position.get("flag_low")
                          or (position.get("meta") or {}).get("flag_low")
                          or (position.get("entry_features") or {}).get("flag_low"))
        try:
            flag_low = float(flag_low) if flag_low is not None else None
        except Exception:
            flag_low = None
        if cur is None or flag_low is None:
            return None
        return cur > flag_low

    if trait == TRAIT_HELD_BOS_PIVOT:
        pivot = (position.get("bos_pivot")
                       or (position.get("meta") or {}).get("bos_pivot")
                       or (position.get("entry_features") or {}).get("bos_pivot"))
        try:
            pivot = float(pivot) if pivot is not None else None
        except Exception:
            pivot = None
        if cur is None or pivot is None:
            return None
        return cur > pivot

    if trait == TRAIT_HELD_PEAK_DRAWDOWN:
        # Drawdown from peak premium. "Intact" means the current dd
        # is shallower than the winner-average max drawdown.
        peak = position.get("peak_premium")
        try:
            peak = float(peak) if peak is not None else None
        except Exception:
            peak = None
        if cur is None or peak is None or peak <= 0:
            return None
        dd = (cur - peak) / peak  # negative when below peak
        winner_avg_dd = position.get("winner_avg_max_dd") or 0.0
        try:
            winner_avg_dd = float(winner_avg_dd)
        except Exception:
            winner_avg_dd = 0.0
        # winner_avg_dd is negative (e.g. -0.03). dd intact when
        # current dd is ABOVE (less negative than) winner_avg_dd.
        return dd >= winner_avg_dd

    if trait == TRAIT_IV_EXPANSION:
        entry_iv = position.get("entry_iv")
        cur_iv = (position.get("current_iv") or position.get("last_iv_seen")
                       or position.get("stored_iv"))
        try:
            entry_iv = float(entry_iv) if entry_iv is not None else None
            cur_iv = float(cur_iv) if cur_iv is not None else None
        except Exception:
            entry_iv = None
            cur_iv = None
        if entry_iv is None or cur_iv is None or entry_iv <= 0:
            return None
        return cur_iv >= entry_iv * 0.95  # tolerate 5% noise

    if trait == TRAIT_IV_COMPRESSION:
        entry_iv = position.get("entry_iv")
        cur_iv = (position.get("current_iv") or position.get("last_iv_seen")
                       or position.get("stored_iv"))
        try:
            entry_iv = float(entry_iv) if entry_iv is not None else None
            cur_iv = float(cur_iv) if cur_iv is not None else None
        except Exception:
            entry_iv = None
            cur_iv = None
        if entry_iv is None or cur_iv is None or entry_iv <= 0:
            return None
        return cur_iv <= entry_iv * 1.05

    if trait == TRAIT_HIT_PEAK_EARLY:
        hold_minutes = position.get("hold_minutes")
        avg_to_peak = position.get("winner_avg_minutes_to_peak") or 0.0
        peak_reached_at = position.get("peak_reached_minutes")
        try:
            hold_minutes = float(hold_minutes) if hold_minutes is not None else None
            avg_to_peak = float(avg_to_peak)
            peak_reached_at = (float(peak_reached_at)
                                       if peak_reached_at is not None else None)
        except Exception:
            return None
        if hold_minutes is None or avg_to_peak <= 0:
            return None
        # If we already hit peak before the avg-winner timestamp, intact.
        if peak_reached_at is not None and peak_reached_at <= avg_to_peak:
            return True
        # Past the average peak time without hitting peak — degraded.
        if peak_reached_at is None and hold_minutes > avg_to_peak * 1.5:
            return False
        return None  # mid-window — not yet conclusive

    return None  # unknown trait


def calculate_health(
    open_position: Optional[Dict[str, Any]],
    current_bars: Optional[Any],
    winner_profile: Optional[WinnerProfile],
) -> ThesisHealth:
    """Score the live position against the historical winner profile."""
    if winner_profile is None or not winner_profile.is_trustworthy:
        return ThesisHealth(
            score=50.0,
            reason="winner profile unavailable or thin corpus — abstain",
            abstain=True,
            profile_sample_size=(winner_profile.sample_size
                                          if winner_profile else 0),
            profile_confidence=(winner_profile.confidence
                                          if winner_profile else 0.0),
        )

    if not open_position:
        return ThesisHealth(
            score=50.0, reason="no open position context", abstain=True,
            profile_sample_size=winner_profile.sample_size,
            profile_confidence=winner_profile.confidence,
        )

    # Hydrate the position with profile-derived expectations the trait
    # checks need. (Doesn't mutate the caller's dict — copy.)
    pos = dict(open_position)
    pos.setdefault("winner_avg_max_dd",
                       winner_profile.avg_max_drawdown_during_hold)
    pos.setdefault("winner_avg_minutes_to_peak",
                       winner_profile.avg_minutes_to_peak)

    intact: List[str] = []
    degraded: List[str] = []
    weight_intact = 0.0
    weight_total = 0.0
    for trait in KNOWN_TRAITS:
        freq = float(winner_profile.trait_frequencies.get(trait, 0.0))
        if freq <= 0.0:
            continue  # not a defining trait — skip
        verdict = _check_trait(trait, pos, current_bars)
        if verdict is None:
            continue  # not enough data to evaluate
        weight_total += freq
        if verdict:
            intact.append(trait)
            weight_intact += freq
        else:
            degraded.append(trait)

    if weight_total <= 0.0:
        return ThesisHealth(
            score=50.0,
            reason="no applicable traits to evaluate",
            abstain=True,
            profile_sample_size=winner_profile.sample_size,
            profile_confidence=winner_profile.confidence,
        )

    # Score = (% traits intact) × profile confidence.
    raw = (weight_intact / weight_total) * 100.0
    # Blend with neutral 50 by (1 - confidence). High-confidence
    # profile → score swings toward the raw measurement. Low-confidence
    # → score stays nearer 50 so the agent doesn't fire prematurely on
    # thin evidence.
    blended = (raw * winner_profile.confidence
                  + 50.0 * (1.0 - winner_profile.confidence))

    if degraded:
        reason = (
            "trade trajectory degrading vs winners — "
            + ", ".join(sorted(degraded))
        )
    else:
        reason = "trade trajectory matches historical winners"

    return ThesisHealth(
        score=round(blended, 2),
        reason=reason,
        intact_traits=sorted(intact),
        degraded_traits=sorted(degraded),
        abstain=False,
        profile_sample_size=winner_profile.sample_size,
        profile_confidence=winner_profile.confidence,
    )
