"""Stage-10 item 6 — per-theme edge heat score.

A theme is "hot" when its recent cohort wins more than baseline; "cold"
when it underperforms. ``theme_size_multiplier(ticker)`` returns a number
in ``[1 − max_swing, 1 + max_swing]`` (default ±0.30) that the portfolio
optimizer applies on top of every other sizing cap.

Heat is computed from the SAME labels Stage-1 + Stage-9 use, so a hot
theme is one with statistically observed edge in your DB — not a vibe.

Behaviour:
  • Below ``min_sample`` cohort closes → heat = 0 (no adjustment)
  • Above 60% win rate AND positive expectancy → heat > 0 (hot)
  • Below 40% win rate OR negative expectancy → heat < 0 (cold)

Plugs into `portfolio_optimizer.optimize_size` as a final multiplier
(applied after Kelly/CVaR/vol-target/drawdown, before the cluster check).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from backend.bot.cohort_matrix import _load_labels
from backend.bot.labeling import TradeLabel
from backend.bot.metrics import expectancy, win_rate
from backend.bot.portfolio_intel import themes_for

logger = logging.getLogger(__name__)

MIN_SAMPLE = 8
MAX_SWING = 0.30


@dataclass
class ThemeHeat:
    theme: str
    closed: int
    win_rate: float
    expectancy: float
    heat: float            # in [-MAX_SWING, +MAX_SWING]
    size_multiplier: float # 1.0 + heat

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── builders ─────────────────────────────────────────────────────────────


def _heat_from_cohort(*, wr: float, exp: float) -> float:
    """Map (win_rate, expectancy) → heat ∈ [-MAX_SWING, +MAX_SWING]."""
    # win-rate component: clip 0.3..0.7 around 0.5 → -1..+1
    wr_comp = max(-1.0, min(1.0, (wr - 0.5) * 5.0))
    # expectancy sign-only weighting — magnitude scales 0..0.5
    exp_comp = 0.5 if exp > 0 else (-0.5 if exp < 0 else 0.0)
    # combined: 60% win-rate + 40% expectancy direction
    blend = 0.6 * wr_comp + 0.4 * exp_comp
    return round(max(-1.0, min(1.0, blend)) * MAX_SWING, 4)


def compute_theme_heat(*, recent_n: int = 50) -> List[ThemeHeat]:
    """Aggregate the last ``recent_n`` closed trades by theme and compute
    heat scores. Tickers may belong to multiple themes (e.g. NVDA → Mag7 +
    AI infra + Semis) — each theme gets its own bucket."""
    labels = [l for l in _load_labels(limit=2000) if l.win is not None]
    labels = labels[:recent_n]

    by_theme: Dict[str, List[TradeLabel]] = {}
    for l in labels:
        for theme in themes_for(l.ticker) or ["__ungrouped__"]:
            by_theme.setdefault(theme, []).append(l)

    out: List[ThemeHeat] = []
    for theme, items in by_theme.items():
        pnls = [l.pnl for l in items if l.pnl is not None]
        if len(pnls) < MIN_SAMPLE:
            continue
        wr = win_rate(pnls) or 0.0
        exp = expectancy(pnls) or 0.0
        heat = _heat_from_cohort(wr=wr, exp=exp)
        out.append(ThemeHeat(
            theme=theme, closed=len(pnls),
            win_rate=wr, expectancy=exp, heat=heat,
            size_multiplier=round(1.0 + heat, 4),
        ))
    out.sort(key=lambda h: h.heat, reverse=True)
    return out


def theme_size_multiplier(ticker: str, *, recent_n: int = 50) -> float:
    """Single number the optimizer multiplies the recommended size by.
    When a ticker spans multiple themes, we take the LOWEST multiplier so
    the coldest cohort governs."""
    heats = {h.theme: h for h in compute_theme_heat(recent_n=recent_n)}
    themes = themes_for(ticker) or []
    relevant = [heats[t].size_multiplier for t in themes if t in heats]
    if not relevant:
        return 1.0
    return min(relevant)
