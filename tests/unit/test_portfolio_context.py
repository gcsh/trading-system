"""MITS Phase 14.B — PortfolioContext: pairwise correlations + stress.

Seeds StockBar rows with deterministic synthetic returns so the Pearson
math has a known correct answer; also covers the sector/theme fallback
path when bars are missing.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from backend.bot.portfolio_intel.portfolio_context import (
    build_portfolio_context,
    _pearson,
    _proxy_correlation,
)
from backend.db import session_scope
from backend.models.stock_bar import StockBar


def _stock(ticker, qty=10, price=100.0):
    return {
        "ticker": ticker, "kind": "stock", "quantity": qty,
        "avg_cost": price, "current_price": price,
        "market_value": qty * price,
    }


def _seed_bars(session, ticker: str, closes: list[float]) -> None:
    """Write daily 1d bars ending today, one per consecutive trading day."""
    base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for i, close in enumerate(closes):
        offset_days = len(closes) - i
        session.add(StockBar(
            ticker=ticker.upper(), interval="1d",
            bar_ts=base - timedelta(days=offset_days),
            open=close, high=close, low=close, close=close,
            volume=1_000_000, source="test",
        ))


def test_pearson_known_values():
    a = [0.01, 0.02, -0.01, 0.005, 0.015, -0.02, 0.0, 0.01]
    b = [0.01, 0.02, -0.01, 0.005, 0.015, -0.02, 0.0, 0.01]
    rho = _pearson(a, b)
    assert rho is not None and abs(rho - 1.0) < 1e-6

    c = [-x for x in a]
    rho2 = _pearson(a, c)
    assert rho2 is not None and abs(rho2 + 1.0) < 1e-6


def test_pearson_returns_none_when_zero_variance():
    a = [0.0] * 10
    b = [0.01, 0.02, -0.01, 0.005, 0.015, -0.02, 0.0, 0.01, 0.0, -0.01]
    assert _pearson(a, b) is None


def test_proxy_correlation_known_buckets():
    # Same theme (AI infrastructure)
    assert _proxy_correlation("NVDA", "AMD") == 0.85
    # Same sector but no shared theme - JPM and GS are both Financials
    # but neither overlaps with "Banks" alone... JPM/GS share "Banks".
    # Use AAPL (Tech, Mag7) + ORCL (Tech, Cloud/software) — no shared theme
    # but same Tech sector.
    assert _proxy_correlation("AAPL", "ORCL") == 0.50
    # No theme + different sectors
    assert _proxy_correlation("XOM", "JNJ") == 0.10
    # Self
    assert _proxy_correlation("AAPL", "AAPL") == 1.0


def test_build_context_empty_positions(temp_db):
    ctx = build_portfolio_context(positions=[], equity=10_000.0)
    assert ctx.equity == 10_000.0
    assert ctx.net_long_notional == 0.0
    assert ctx.net_short_notional == 0.0
    assert ctx.leverage == 0.0
    assert ctx.pairwise_correlation == {}
    assert ctx.stress_spy_down_3pct_pnl == 0.0


def test_build_context_pairwise_from_bars(temp_db):
    """Two perfectly-correlated tickers via synthetic bars: rho should be ~1.0
    (price series proportional → return series identical)."""
    closes_a = [100.0 + i for i in range(40)]
    closes_b = [200.0 + 2 * i for i in range(40)]
    # B = 2A, so daily returns of A and B are NOT identical (different bases)
    # but they are perfectly correlated.
    with session_scope() as s:
        _seed_bars(s, "AAA", closes_a)
        _seed_bars(s, "BBB", closes_b)

    positions = [_stock("AAA", 10, 140), _stock("BBB", 5, 280)]
    ctx = build_portfolio_context(
        positions=positions, equity=10_000.0, lookback_days=40,
    )
    rho_ab = ctx.pairwise_correlation.get("AAA", {}).get("BBB")
    assert rho_ab is not None
    assert rho_ab > 0.99


def test_build_context_anti_correlated(temp_db):
    # Build A as oscillating gains/losses; B mirrors with sign flipped.
    base_a = 100.0
    base_b = 100.0
    closes_a = [base_a]
    closes_b = [base_b]
    for i in range(40):
        bump = 1.0 if i % 2 == 0 else -1.0
        base_a += bump
        base_b -= bump   # mirror
        closes_a.append(base_a)
        closes_b.append(base_b)
    with session_scope() as s:
        _seed_bars(s, "UPP", closes_a)
        _seed_bars(s, "DWN", closes_b)
    positions = [_stock("UPP", 10, 140), _stock("DWN", 10, 100)]
    ctx = build_portfolio_context(
        positions=positions, equity=10_000.0, lookback_days=40,
    )
    rho = ctx.pairwise_correlation["UPP"]["DWN"]
    # Mirrored returns: rho should be strongly negative.
    assert rho < -0.9


def test_candidate_max_correlation(temp_db):
    """Candidate vs held positions: surface the worst-correlated peer."""
    closes = [100.0 + i for i in range(40)]
    closes_low = [100.0 + (i * 0.1) + ((-1) ** i) for i in range(40)]
    with session_scope() as s:
        _seed_bars(s, "HELD1", closes)
        _seed_bars(s, "HELD2", closes_low)
        _seed_bars(s, "CAND", closes)  # perfectly correlated with HELD1

    positions = [_stock("HELD1", 10, 140), _stock("HELD2", 10, 105)]
    ctx = build_portfolio_context(
        positions=positions,
        equity=10_000.0,
        candidate_ticker="CAND",
        candidate_direction="LONG",
        lookback_days=40,
    )
    assert ctx.candidate_max_correlation is not None
    assert ctx.candidate_max_correlation > 0.95
    assert ctx.candidate_max_correlation_peer == "HELD1"


def test_proxy_fallback_when_bars_missing(temp_db):
    """No StockBar rows → proxy correlations fill the matrix instead."""
    positions = [_stock("NVDA", 10, 200), _stock("AMD", 10, 150)]
    ctx = build_portfolio_context(
        positions=positions, equity=10_000.0,
    )
    rho = ctx.pairwise_correlation["NVDA"]["AMD"]
    # Same AI infrastructure theme → 0.85 from proxy.
    assert rho == 0.85


def test_leverage_and_sector_weights(temp_db):
    positions = [_stock("NVDA", 10, 200), _stock("AMD", 10, 100)]
    ctx = build_portfolio_context(
        positions=positions, equity=5_000.0,
    )
    # Gross notional = 2000 + 1000 = 3000; equity = 5000 → leverage 0.6
    assert ctx.leverage == 0.6
    # Both Semis → sector weight 1.0
    assert ctx.by_sector["Semis"] == 1.0


def test_spy_stress_uses_beta(temp_db):
    """SPY -3% stress: NVDA β≈1.7 → -5.1% on $2k position = -$102."""
    positions = [_stock("NVDA", 10, 200)]
    ctx = build_portfolio_context(
        positions=positions, equity=5_000.0,
    )
    # Pnl = 2000 * 1.7 * -0.03 = -102
    assert math.isclose(ctx.stress_spy_down_3pct_pnl, -102.0, abs_tol=0.01)
    assert math.isclose(
        ctx.stress_spy_down_3pct_pct, -102.0 / 5000.0, abs_tol=0.001,
    )


def test_short_position_signed_notional(temp_db):
    """Short stock subtracts from net_long_notional and contributes negative
    signed exposure to the stress projection."""
    short = {
        "ticker": "AAPL", "kind": "stock", "quantity": -10,
        "avg_cost": 200.0, "current_price": 200.0,
        "market_value": 2000.0,
    }
    ctx = build_portfolio_context(positions=[short], equity=5_000.0)
    assert ctx.net_short_notional == 2000.0
    assert ctx.net_long_notional == 0.0
    # Short AAPL (β 1.2): SPY -3% → AAPL -3.6% → short gains $72
    assert ctx.stress_spy_down_3pct_pnl > 0
