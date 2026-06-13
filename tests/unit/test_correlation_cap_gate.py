"""MITS Phase 14.B — correlation_cap_gate rules.

Same-direction high rho → block. Opposite direction → pass (hedge).
Sector cap exceeded → block.
"""
from __future__ import annotations

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


def test_blocks_when_same_direction_rho_above_threshold():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"AMD": 0.92},
            "AMD": {"NVDA": 0.92},
            "CAND": {"NVDA": 0.91, "AMD": 0.88},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA"), _stock("AMD")],
    )
    assert isinstance(result, CorrelationCapResult)
    assert result.blocked is True
    assert result.worst_peer == "NVDA"
    assert abs(result.worst_rho) >= 0.85
    assert result.candidate_direction == "LONG"
    assert "correlation cap" in result.reason


def test_does_not_block_when_opposite_direction():
    """Opposite direction is a hedge — should pass even at high rho."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.95},
            "CAND": {"NVDA": 0.95},
        },
    )
    # NVDA is LONG (positive qty); candidate is SHORT.
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="SHORT",
        portfolio_context=pctx,
        positions=[_stock("NVDA", qty=10)],
    )
    assert result.blocked is False
    assert result.candidate_direction == "SHORT"


def test_passes_below_threshold():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.50},
            "CAND": {"NVDA": 0.50},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    assert result.worst_peer == "NVDA"


def test_sector_cap_blocks_when_exposure_at_or_above_cap():
    pctx = PortfolioContext(
        equity=10_000.0,
        by_sector={"Semis": 0.55},   # > 0.50 cap default
        pairwise_correlation={
            "NVDA": {"CAND": 0.20},
            "CAND": {"NVDA": 0.20},
        },
    )
    # candidate is in Semis as well (AMD)
    result = check_correlation_cap(
        candidate_ticker="AMD",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is True
    assert "sector cap" in result.reason.lower()


def test_sector_cap_passes_under_cap():
    pctx = PortfolioContext(
        equity=10_000.0,
        by_sector={"Semis": 0.10},
        pairwise_correlation={
            "NVDA": {"AMD": 0.30},
            "AMD": {"NVDA": 0.30},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="AMD",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False


def test_custom_rho_threshold():
    """Override the rho threshold per-call."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.70},
            "CAND": {"NVDA": 0.70},
        },
    )
    # Default threshold 0.85 → would pass
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    # Tighten threshold to 0.6 → blocks
    tight = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
        rho_threshold=0.6,
    )
    assert tight.blocked is True


def test_empty_portfolio_never_blocks():
    pctx = PortfolioContext(equity=10_000.0)
    result = check_correlation_cap(
        candidate_ticker="ANY",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[],
    )
    assert result.blocked is False
    assert result.worst_peer is None


def test_short_peer_direction_inferred():
    """Negative-qty position must register as SHORT so a SHORT candidate
    pile-up is blocked while a LONG candidate is treated as a hedge."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.95},
            "CAND": {"NVDA": 0.95},
        },
    )
    short_nvda = {
        "ticker": "NVDA", "kind": "stock", "quantity": -10,
        "avg_cost": 200.0, "current_price": 200.0, "market_value": 2000.0,
    }
    # Candidate also SHORT — same direction, high rho → block.
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="SHORT",
        portfolio_context=pctx,
        positions=[short_nvda],
    )
    assert result.blocked is True

    # Candidate LONG, peer SHORT — opposite directions, treated as hedge.
    result2 = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[short_nvda],
    )
    assert result2.blocked is False
