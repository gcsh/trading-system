"""Stage-10 item 20 — sweep/absorb ratio momentum.

Stage 4's MicrostructureSnapshot exposes ``sweep_probability`` and
``absorption_probability`` per-tick. The DIFFERENCE between them is a
tape-side bias signal (positive = aggressive buy / sweep-dominant tape,
negative = absorption / reversal-precursor tape). The MOMENTUM of that
difference is more powerful than the level — it tells you the tape is
TRENDING toward sweeps or reversing into absorption.

Pure functions over a sequence of (sweep_prob, absorb_prob) pairs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class SweepAbsorbMomentum:
    n: int                       # how many snapshots we read
    current_diff: float = 0.0    # latest sweep − absorb
    mean_diff: float = 0.0       # mean over the window
    slope: float = 0.0           # linear slope of (sweep − absorb) over time
    direction: str = "neutral"   # "trend_buy" | "trend_sell" | "neutral"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _slope(series: Sequence[float]) -> float:
    """Linear-regression slope of ``series`` against an evenly-spaced
    index. Returns 0 when fewer than 2 observations."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    sum_x = sum(xs)
    sum_y = sum(series)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, series))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def sweep_absorb_momentum(
    snapshots: Sequence[Dict[str, Any]],
    *,
    min_obs: int = 5,
    strong_slope: float = 0.05,
) -> Optional[SweepAbsorbMomentum]:
    """Compute the sweep − absorb momentum from a snapshot history.

    Each snapshot must have ``sweep_probability`` and
    ``absorption_probability``. Returns ``None`` when fewer than
    ``min_obs`` valid snapshots are supplied — honesty over noise.

    direction:
      • ``trend_buy`` when slope ≥ +``strong_slope``
      • ``trend_sell`` when slope ≤ -``strong_slope``
      • ``neutral`` otherwise
    """
    diffs: List[float] = []
    for s in snapshots:
        sweep = s.get("sweep_probability")
        absorb = s.get("absorption_probability")
        if sweep is None or absorb is None:
            continue
        try:
            diffs.append(float(sweep) - float(absorb))
        except (TypeError, ValueError):
            continue
    if len(diffs) < min_obs:
        return None
    slope = _slope(diffs)
    current = diffs[-1]
    mean = sum(diffs) / len(diffs)
    direction = ("trend_buy" if slope >= strong_slope
                  else "trend_sell" if slope <= -strong_slope
                  else "neutral")
    notes: List[str] = []
    if abs(current) > 0.5 and direction == "neutral":
        notes.append(
            f"strong level ({current:+.2f}) but flat slope — watching for "
            f"trend confirmation"
        )
    if direction == "trend_buy" and current > 0:
        notes.append("aggressive buy-side tape accelerating")
    if direction == "trend_sell" and current < 0:
        notes.append("absorption/reversal tape accelerating")
    return SweepAbsorbMomentum(
        n=len(diffs), current_diff=round(current, 4),
        mean_diff=round(mean, 4), slope=round(slope, 6),
        direction=direction, notes=notes,
    )
