"""MITS Phase 6 — Detector + trial scorecard helpers."""

from backend.bot.scorecard.detector_scorecard import (
    build_detector_scorecard,
    build_leaderboard,
    cumulative_pnl_series,
)
from backend.bot.scorecard.suggestions import (
    run_suggestions_pass,
)

__all__ = [
    "build_detector_scorecard",
    "build_leaderboard",
    "cumulative_pnl_series",
    "run_suggestions_pass",
]
