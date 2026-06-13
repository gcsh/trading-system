"""MITS Phase 16.C — Correlation-cap continuous sizing.

Soft-cap zone (0.5 < |rho| < rho_thr) emits sizing_multiplier between
1.0 and 0.3 via linear interpolation. Hard cap (|rho| >= rho_thr) keeps
14.B behavior — blocked=True + sizing_multiplier=0.0 + hard_block=True.
"""
from __future__ import annotations

import pytest

from backend.bot.gates.correlation_cap_gate import (
    CorrelationCapResult,
    check_correlation_cap,
)
from backend.bot.portfolio_intel.portfolio_context import PortfolioContext


def _stock(ticker, qty=10, price=100.0):
    return {
        "ticker": ticker, "kind": "stock", "quantity": qty,
        "avg_cost": price, "current_price": price,
        "market_value": qty * price,
    }


def test_rho_065_yields_soft_multiplier():
    """|rho|=0.65 is in soft zone; multiplier interpolated between 1.0
    and 0.3 over (0.5, 0.85)."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.65}, "CAND": {"NVDA": 0.65},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    assert result.hard_block is False
    # progress = (0.65 - 0.5) / (0.85 - 0.5) = 0.4286
    # multiplier = 1.0 - 0.7 * 0.4286 = 0.7
    assert 0.55 <= result.sizing_multiplier <= 0.75
    assert "soft cap" in result.reason


def test_rho_055_just_into_soft_zone():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.55}, "CAND": {"NVDA": 0.55},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    assert result.hard_block is False
    # progress = 0.05 / 0.35 = 0.143 → multiplier = 1 - 0.7*0.143 = 0.9
    assert result.sizing_multiplier > 0.85
    assert result.sizing_multiplier < 1.0


def test_rho_080_near_hard_cap_floor():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.80}, "CAND": {"NVDA": 0.80},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    assert result.hard_block is False
    # progress = 0.3 / 0.35 = 0.857 → multiplier = 1 - 0.7*0.857 ≈ 0.4
    assert 0.30 <= result.sizing_multiplier <= 0.50


def test_rho_090_hard_block_full_zero():
    """|rho|=0.90 >= rho_thr 0.85 → hard block + multiplier 0."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.90}, "CAND": {"NVDA": 0.90},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is True
    assert result.hard_block is True
    assert result.sizing_multiplier == 0.0
    assert "correlation cap" in result.reason


def test_rho_at_threshold_is_hard_block():
    """|rho| == rho_thr (0.85) inclusive → hard block."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.85}, "CAND": {"NVDA": 0.85},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is True
    assert result.hard_block is True
    assert result.sizing_multiplier == 0.0


def test_low_rho_full_size():
    """|rho| <= 0.5 → no haircut, multiplier 1.0."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.30}, "CAND": {"NVDA": 0.30},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    assert result.hard_block is False
    assert result.sizing_multiplier == 1.0


def test_empty_portfolio_full_size():
    pctx = PortfolioContext(equity=10_000.0)
    result = check_correlation_cap(
        candidate_ticker="ANY",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[],
    )
    assert result.blocked is False
    assert result.hard_block is False
    assert result.sizing_multiplier == 1.0


def test_opposite_direction_hedge_full_size():
    """Hedge stays at full size even when raw rho is high."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.95}, "CAND": {"NVDA": 0.95},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="SHORT",
        portfolio_context=pctx,
        positions=[_stock("NVDA", qty=10)],
    )
    assert result.blocked is False
    assert result.hard_block is False
    assert result.sizing_multiplier == 1.0


def test_monotone_decrease_through_soft_zone():
    """As |rho| grows through the soft zone, multiplier monotonically
    decreases."""
    pctx_lo = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={"NVDA": {"CAND": 0.6}, "CAND": {"NVDA": 0.6}},
    )
    pctx_hi = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={"NVDA": {"CAND": 0.75}, "CAND": {"NVDA": 0.75}},
    )
    lo = check_correlation_cap(
        candidate_ticker="CAND", candidate_direction="LONG",
        portfolio_context=pctx_lo, positions=[_stock("NVDA")],
    )
    hi = check_correlation_cap(
        candidate_ticker="CAND", candidate_direction="LONG",
        portfolio_context=pctx_hi, positions=[_stock("NVDA")],
    )
    assert lo.sizing_multiplier > hi.sizing_multiplier


def test_to_dict_round_trip_carries_new_fields():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={"NVDA": {"CAND": 0.7}, "CAND": {"NVDA": 0.7}},
    )
    result = check_correlation_cap(
        candidate_ticker="CAND", candidate_direction="LONG",
        portfolio_context=pctx, positions=[_stock("NVDA")],
    )
    d = result.to_dict()
    assert "sizing_multiplier" in d
    assert "hard_block" in d
    assert d["hard_block"] is False
    assert d["blocked"] is False
    assert isinstance(d["sizing_multiplier"], float)
