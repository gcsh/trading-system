"""MITS-5 — Thesis-health exit monitor.

Architecture:

  * `winner_profile.WinnerProfile` — dataclass: average trajectory of
    historical WINNERS for a (pattern, regime) cohort.
  * `profile_builder.build_winner_profile()` — walks
    `market_observations + market_outcomes`, filters to winners,
    aggregates into a `WinnerProfile`. Cached.
  * `health_calculator.calculate_health()` — scores a live open position
    against the winner profile. Returns `ThesisHealth` with score
    (0-100), reason, and degraded trait list.
  * Wired as the 7th council agent (`agent_thesis_health` in
    `backend/bot/agents/thesis_health.py`). Engine consults BEFORE
    EXIT.1's mechanical safety net runs.

The whole subsystem fails open: if the corpus is thin, the agent
abstains. EXIT.1's trailing stop continues to protect the trade.
"""
from backend.bot.thesis.winner_profile import WinnerProfile
from backend.bot.thesis.profile_builder import build_winner_profile
from backend.bot.thesis.health_calculator import (
    ThesisHealth,
    calculate_health,
)

__all__ = [
    "WinnerProfile",
    "build_winner_profile",
    "ThesisHealth",
    "calculate_health",
]
