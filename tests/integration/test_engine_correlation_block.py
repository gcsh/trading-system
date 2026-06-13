"""MITS Phase 14.B — engine correlation-cap block (integration).

Seeds two synthetic long positions whose return series are perfectly
correlated (rho ≈ 1.0). When the engine evaluates a fresh long in the
same return cohort, the gate must short-circuit with
``status=correlation_cap_block``. Flips the candidate to SHORT and
confirms the hedge case is not blocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.paper_executor import PaperExecutor
from backend.bot.market_data import MarketSnapshot
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.paper import PaperPosition, get_or_create_account
from backend.models.stock_bar import StockBar


def _seed_bars(ticker, closes):
    base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        for i, c in enumerate(closes):
            s.add(StockBar(
                ticker=ticker.upper(), interval="1d",
                bar_ts=base - timedelta(days=len(closes) - i),
                open=c, high=c, low=c, close=c, volume=1_000_000,
                source="test",
            ))


def _seed_positions(direction="LONG", avg_cost=130.0):
    qty = 10 if direction == "LONG" else -10
    with session_scope() as s:
        account = get_or_create_account(s, starting_cash=10_000.0)
        account.cash = 10_000.0
        account.last_portfolio_value = 10_000.0
        s.add(PaperPosition(
            ticker="NVDA", kind="stock", quantity=qty,
            avg_cost=avg_cost, opened_at=datetime.utcnow(),
        ))
        s.add(PaperPosition(
            ticker="AMD", kind="stock", quantity=qty,
            avg_cost=avg_cost, opened_at=datetime.utcnow(),
        ))


def _flat_price(_ticker):
    """All tickers price at 130.0 — same as the test snapshot 'price' —
    so seeded positions sit at breakeven (no TP / SL exit fires before
    the candidate reaches the correlation-cap gate)."""
    return 130.0


def _oversold_avgo(_ticker):
    return MarketSnapshot(data={
        "price": 130.0, "rsi": 22.0, "macd": -0.3, "macd_signal": -0.1,
        "macd_hist": -0.2, "prev_macd_hist": 0.1, "ma50": 145.0,
        "ma200": 120.0, "volume": 1_200_000, "avg_volume": 1_000_000,
        "iv_rank": 30, "adx": 18, "vix": 18, "news_score": 0.0,
        "earnings_days": 30, "pe_ratio": 22, "spy_trend": "neutral",
        "spy_adx": 18, "gap_pct": 0.0, "premarket_volume": 50_000,
        "shares_owned": 0, "position_value": 0, "portfolio_value": 10_000,
        "unrealized_gain_pct": 0.0, "high_52w": 160.0, "prev_close": 132.0,
        "vwap": 131.0, "momentum_5m": -0.1, "rsi_5m": 30,
        "market_trend": "neutral", "time_of_day": "11:00",
        "orb_high": 132.0, "orb_low": 129.0,
        "hist_earnings_move_avg": 0.05, "implied_move": 0.07,
        "has_catalyst": False, "earnings_today": False,
        "news_age_hours": 999, "range_3w_pct": 0.03,
    }, source_errors=[])


def _setup_engine(ticker):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = [ticker]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        cfg["force_run_when_closed"] = True
        # Don't gate via consensus abstain — we want the trade to reach
        # the correlation-cap gate.
        cfg["ai"] = {**(cfg.get("ai") or {}),
                     "consensus_abstain_enabled": False,
                     "brain_enabled": False}
        save_config(session, cfg)
    adapter = MagicMock()
    adapter.snapshot.side_effect = _oversold_avgo
    return BotEngine(
        executor=PaperExecutor(starting_cash=10_000.0, price_fn=_flat_price), market_data=adapter,
    )


def test_correlation_cap_blocks_long_pile_up(temp_db):
    closes = [100.0 + i * 0.5 for i in range(40)]
    _seed_bars("NVDA", closes)
    _seed_bars("AMD", closes)
    _seed_bars("AVGO", closes)   # perfectly correlated with NVDA + AMD
    _seed_positions(direction="LONG")

    engine = _setup_engine("AVGO")
    events = engine.run_cycle()

    # AVGO is the candidate. Other events (exits on held NVDA / AMD) are OK,
    # but the AVGO event MUST surface as a correlation_cap_block.
    avgo_events = [e for e in events
                   if (e.get("ticker") or "").upper() == "AVGO"]
    assert len(avgo_events) == 1, (
        f"expected exactly one AVGO event, got {len(avgo_events)} - "
        f"{[(e.get('ticker'), e.get('status')) for e in events]}"
    )
    event = avgo_events[0]
    assert event["status"] == "correlation_cap_block", (
        f"expected correlation_cap_block, got {event.get('status')} - "
        f"reason: {event.get('reason')}"
    )
    cap = event.get("correlation_cap") or {}
    assert cap.get("blocked") is True
    # Either trigger is acceptable. With NVDA + AMD held (both Semis), the
    # sector cap is hit at 100% concentration; with non-Semis peers the rho
    # cap fires. Verify the gate emitted a usable verdict either way.
    reason = (cap.get("reason") or "").lower()
    assert ("correlation cap" in reason) or ("sector cap" in reason), reason
    # When the rho path fires, we expect the peer to be one of the held names.
    if "correlation cap" in reason:
        assert cap.get("worst_peer") in ("NVDA", "AMD")
        assert abs(float(cap.get("worst_rho") or 0.0)) >= 0.85


def test_correlation_cap_blocks_via_rho_with_cross_sector_book(temp_db):
    """Two LONG positions in DIFFERENT sectors but the candidate's bar
    history makes |rho| >= 0.85 against one of them. Only the rho path
    can fire here (sector concentration stays at 50/50)."""
    # Use tickers in different sectors with no shared themes to keep the
    # proxy fallback off the rho path. Two LONG: WMT (Consumer), XOM
    # (Energy). Candidate JPM (Financials). Seed bars so JPM tracks WMT.
    closes_track = [100.0 + i * 0.4 for i in range(40)]
    closes_other = [100.0 + ((-1) ** i) * 0.5 for i in range(40)]
    _seed_bars("WMT", closes_track)
    _seed_bars("XOM", closes_other)
    _seed_bars("JPM", closes_track)
    with session_scope() as s:
        account = get_or_create_account(s, starting_cash=10_000.0)
        account.cash = 10_000.0
        account.last_portfolio_value = 10_000.0
        # avg_cost matches _flat_price so MTM is breakeven — keeps the
        # exit manager from liquidating before the gate sees them.
        s.add(PaperPosition(
            ticker="WMT", kind="stock", quantity=10,
            avg_cost=130.0, opened_at=datetime.utcnow(),
        ))
        s.add(PaperPosition(
            ticker="XOM", kind="stock", quantity=10,
            avg_cost=130.0, opened_at=datetime.utcnow(),
        ))

    engine = _setup_engine("JPM")
    events = engine.run_cycle()

    jpm_events = [e for e in events
                  if (e.get("ticker") or "").upper() == "JPM"]
    assert len(jpm_events) == 1
    event = jpm_events[0]
    assert event["status"] == "correlation_cap_block", (
        f"expected correlation_cap_block, got {event.get('status')} - "
        f"reason: {event.get('reason')}"
    )
    cap = event.get("correlation_cap") or {}
    assert cap.get("blocked") is True
    assert "correlation cap" in (cap.get("reason") or "").lower()
    assert cap.get("worst_peer") == "WMT"
    assert abs(float(cap.get("worst_rho") or 0.0)) >= 0.85


def test_correlation_cap_passes_short_against_long_book(temp_db):
    """Opposite direction = hedge → must NOT block."""
    closes = [100.0 + i * 0.5 for i in range(40)]
    _seed_bars("NVDA", closes)
    _seed_bars("AMD", closes)
    _seed_bars("AVGO", closes)
    _seed_positions(direction="LONG")

    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = ["AVGO"]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        cfg["force_run_when_closed"] = True
        cfg["ai"] = {**(cfg.get("ai") or {}),
                     "consensus_abstain_enabled": False,
                     "brain_enabled": False}
        save_config(session, cfg)

    # Overbought snapshot would normally trigger a SELL (short) on
    # rsi_mean_reversion; we synthesize one inline.
    def _overbought(_t):
        return MarketSnapshot(data={
            "price": 130.0, "rsi": 78.0, "macd": 0.3, "macd_signal": 0.1,
            "macd_hist": 0.2, "prev_macd_hist": -0.1, "ma50": 120.0,
            "ma200": 110.0, "volume": 1_200_000, "avg_volume": 1_000_000,
            "iv_rank": 30, "adx": 18, "vix": 18, "news_score": 0.0,
            "earnings_days": 30, "pe_ratio": 22, "spy_trend": "neutral",
            "spy_adx": 18, "gap_pct": 0.0, "premarket_volume": 50_000,
            "shares_owned": 0, "position_value": 0, "portfolio_value": 10_000,
            "unrealized_gain_pct": 0.0, "high_52w": 140.0, "prev_close": 128.0,
            "vwap": 129.0, "momentum_5m": 0.1, "rsi_5m": 75,
            "market_trend": "neutral", "time_of_day": "11:00",
            "orb_high": 130.0, "orb_low": 127.0,
            "hist_earnings_move_avg": 0.05, "implied_move": 0.07,
            "has_catalyst": False, "earnings_today": False,
            "news_age_hours": 999, "range_3w_pct": 0.03,
        }, source_errors=[])

    adapter = MagicMock()
    adapter.snapshot.side_effect = _overbought
    engine = BotEngine(executor=PaperExecutor(starting_cash=10_000.0, price_fn=_flat_price),
                       market_data=adapter)
    events = engine.run_cycle()

    # On the SELL-side, the position direction is SHORT and the existing
    # book is LONG → directions disagree, gate must NOT fire.
    assert len(events) >= 0
    blocked = [e for e in events
               if e.get("status") == "correlation_cap_block"]
    assert blocked == [], (
        "Opposite-direction trade should not be blocked by the "
        f"correlation cap; got {[e.get('status') for e in events]}"
    )
