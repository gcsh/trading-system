"""MITS Phase 4 (P4.4) — chain_strike integration in suggested actions.

Pins:
  1. ``_resolve_suggested_strike`` calls into ``chain_strike`` and tags
     the returned strike with ``source='chain'`` when chain_strike
     yields a listed strike distinct from the arithmetic snap.
  2. Falls back to ``snap_fallback`` cleanly when chain_strike returns
     the same value as ``snap_strike`` (path that took the heuristic).
  3. Suggested action dict carries ``strike_source`` + ``direction``
     + ``dte_target`` keys.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def test_resolve_suggested_strike_chain_path():
    from backend.api.routes import analysis as analysis_routes
    # chain_strike returns a strike clearly different from snap_strike
    # (which for spot=100 + 1% OTM moneyness = ~101 with default 1.0 step
    # → snap returns 101; we patch chain_strike to return 102.5 listed).
    with patch(
        "backend.api.routes.analysis.chain_strike",
        create=True,
    ):
        pass  # Just ensure import works
    with patch("backend.bot.data.options.chain_strike", return_value=102.5), \
            patch("backend.bot.data.options.snap_strike", return_value=101.0):
        strike, source = analysis_routes._resolve_suggested_strike(
            "SPY", 100.0, "long_call", 30,
        )
    assert strike == 102.5
    assert source == "chain"


def test_resolve_suggested_strike_falls_back_to_snap():
    from backend.api.routes import analysis as analysis_routes
    with patch("backend.bot.data.options.chain_strike", return_value=101.0), \
            patch("backend.bot.data.options.snap_strike", return_value=101.0):
        strike, source = analysis_routes._resolve_suggested_strike(
            "SPY", 100.0, "long_call", 30,
        )
    assert strike == 101.0
    assert source == "snap_fallback"


def test_resolve_suggested_strike_zero_spot():
    from backend.api.routes import analysis as analysis_routes
    strike, source = analysis_routes._resolve_suggested_strike(
        "SPY", 0.0, "long_call", 30,
    )
    assert strike is None
    assert source == "snap_fallback"


def test_resolve_suggested_strike_chain_raises_falls_back():
    """When chain_strike blows up (ThetaData not running), we still
    surface a strike via snap_strike + ``snap_fallback``."""
    from backend.api.routes import analysis as analysis_routes
    with patch("backend.bot.data.options.chain_strike",
                  side_effect=RuntimeError("theta down")), \
            patch("backend.bot.data.options.snap_strike", return_value=101.0):
        strike, source = analysis_routes._resolve_suggested_strike(
            "SPY", 100.0, "long_call", 30,
        )
    assert strike == 101.0
    assert source == "snap_fallback"


def test_suggested_action_for_carries_strike_source_metadata():
    """When the cohort qualifies, the suggested_action dict MUST carry
    direction + strike_source + dte_target."""
    from backend.api.routes import analysis as analysis_routes
    k = {
        "posterior_win_rate": 0.72, "sample_size": 100,
        "regime": "trending_up",
    }
    with patch("backend.bot.data.options.chain_strike", return_value=105.0), \
            patch("backend.bot.data.options.snap_strike", return_value=101.0):
        sa = analysis_routes._suggested_action_for(
            "bull_flag", k, "NVDA", 100.0,
        )
    assert sa is not None
    assert sa["action"] == "BUY_CALL"
    assert sa["direction"] == "long_call"
    assert sa["strike_source"] == "chain"
    assert sa["strike"] == 105.0
    assert sa["dte_target"] == 30


def test_suggested_action_for_below_threshold_returns_none():
    """Posterior < 0.60 → no suggested action regardless of strike."""
    from backend.api.routes import analysis as analysis_routes
    k = {
        "posterior_win_rate": 0.45, "sample_size": 100,
        "regime": "choppy",
    }
    sa = analysis_routes._suggested_action_for(
        "bull_flag", k, "NVDA", 100.0,
    )
    assert sa is None


def test_eod_resolve_suggested_strike_chain_path():
    """Mirror of the analysis-route test but on the EOD helper."""
    from backend.bot import eod_analysis as eod
    with patch("backend.bot.data.options.chain_strike", return_value=102.5), \
            patch("backend.bot.data.options.snap_strike", return_value=101.0):
        strike, source = eod._resolve_suggested_strike(
            "SPY", 100.0, "long_put", 30,
        )
    assert strike == 102.5
    assert source == "chain"


def test_eod_suggested_action_carries_strike_source_metadata():
    from backend.bot import eod_analysis as eod
    cohort = {
        "posterior_win_rate": 0.75, "sample_size": 200,
        "regime": "trending_up",
    }
    with patch("backend.bot.data.options.chain_strike", return_value=105.0), \
            patch("backend.bot.data.options.snap_strike", return_value=101.0):
        sa = eod._suggested_action(
            "breakout", cohort, "NVDA", 100.0,
        )
    assert sa is not None
    assert sa["strike_source"] == "chain"
    assert sa["direction"] == "long_call"
    assert sa["dte_target"] == 30
