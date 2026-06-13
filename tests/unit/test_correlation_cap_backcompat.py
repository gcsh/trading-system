"""MITS Phase 16.C — Correlation-cap back-compat guard.

14.B callers read ``result.blocked`` + ``result.reason``. The 16.C
refactor adds ``sizing_multiplier`` + ``hard_block`` ADDITIVELY — the
legacy fields keep the same semantics. These tests mirror the existing
14.B test_correlation_cap_gate.py cases to lock that contract in
place.
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


def test_blocked_field_still_true_at_hard_cap():
    """Existing 14.B reads `.blocked` — must stay True on hard cap."""
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
    # Equivalent to hard_block now, but legacy callers MUST keep working.
    assert result.worst_peer == "NVDA"
    assert abs(result.worst_rho) >= 0.85
    assert result.candidate_direction == "LONG"
    assert "correlation cap" in result.reason


def test_blocked_field_false_for_hedge():
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
    assert result.candidate_direction == "SHORT"


def test_blocked_field_false_at_low_rho():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.50}, "CAND": {"NVDA": 0.50},
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


def test_sector_cap_still_blocks_via_blocked_field():
    pctx = PortfolioContext(
        equity=10_000.0,
        by_sector={"Semis": 0.55},
        pairwise_correlation={
            "NVDA": {"CAND": 0.20}, "CAND": {"NVDA": 0.20},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="AMD",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is True
    assert "sector cap" in result.reason.lower()


def test_sector_cap_passes_under_cap_via_blocked_field():
    pctx = PortfolioContext(
        equity=10_000.0,
        by_sector={"Semis": 0.10},
        pairwise_correlation={
            "NVDA": {"AMD": 0.30}, "AMD": {"NVDA": 0.30},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="AMD",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False


def test_custom_threshold_blocked_field():
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.70}, "CAND": {"NVDA": 0.70},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    assert result.blocked is False
    tight = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
        rho_threshold=0.6,
    )
    assert tight.blocked is True


def test_empty_portfolio_blocked_field():
    pctx = PortfolioContext(equity=10_000.0)
    result = check_correlation_cap(
        candidate_ticker="ANY",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[],
    )
    assert result.blocked is False
    assert result.worst_peer is None


def test_short_peer_direction_blocked_field():
    """Mirror 14.B test_short_peer_direction_inferred — same .blocked
    contract."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.95}, "CAND": {"NVDA": 0.95},
        },
    )
    short_nvda = {
        "ticker": "NVDA", "kind": "stock", "quantity": -10,
        "avg_cost": 200.0, "current_price": 200.0, "market_value": 2000.0,
    }
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="SHORT",
        portfolio_context=pctx,
        positions=[short_nvda],
    )
    assert result.blocked is True
    result2 = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[short_nvda],
    )
    assert result2.blocked is False


def test_to_dict_preserves_legacy_keys():
    """The dict shape that the rule emits into event["correlation_cap"]
    MUST still carry blocked / reason / worst_peer / worst_rho /
    candidate_direction so the 14.B integration test reads still work."""
    pctx = PortfolioContext(
        equity=10_000.0,
        pairwise_correlation={
            "NVDA": {"CAND": 0.20}, "CAND": {"NVDA": 0.20},
        },
    )
    result = check_correlation_cap(
        candidate_ticker="CAND",
        candidate_direction="LONG",
        portfolio_context=pctx,
        positions=[_stock("NVDA")],
    )
    d = result.to_dict()
    for key in ("blocked", "reason", "worst_peer", "worst_rho",
                "candidate_direction"):
        assert key in d
