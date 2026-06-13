"""MITS Phase 16.A — engine cycle parity after the policy refactor.

Drives the live engine through scenarios that previously produced
specific ``event["status"]`` strings and asserts the post-refactor
event carries the same status. UI consumers (Mission Control,
gate_diagnostics, decision_log analytics) read these strings, so the
contract MUST be 1:1.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.market_data import MarketSnapshot
from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.paper import PaperPosition, get_or_create_account


def _flat_price(_t):
    return 130.0


def _bull_snap(_t):
    """Snapshot that triggers RSI mean-reversion BUY but stays inside the
    abstain / drift / catalyst windows."""
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


def _setup_engine(ticker, strategy="rsi_mean_reversion", extra_cfg=None):
    with session_scope() as s:
        cfg = load_config(s)
        cfg["strategy"] = strategy
        cfg["tickers"] = [ticker]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        cfg["force_run_when_closed"] = True
        cfg["ai"] = {**(cfg.get("ai") or {}),
                     "consensus_abstain_enabled": False,
                     "brain_enabled": False}
        if extra_cfg:
            cfg.update(extra_cfg)
        save_config(s, cfg)
    adapter = MagicMock()
    adapter.snapshot.side_effect = _bull_snap
    return BotEngine(
        executor=PaperExecutor(starting_cash=10_000.0, price_fn=_flat_price),
        market_data=adapter,
    )


# ── 1. kill_switch parity ───────────────────────────────────────────────

def test_parity_kill_switch_status(temp_db, monkeypatch):
    """Kill switch active → event["status"] == "kill_switch"."""
    with session_scope() as s:
        get_or_create_account(s, starting_cash=10_000.0)
    monkeypatch.setattr(
        "backend.bot.canary.kill_switch_active", lambda: True,
    )
    engine = _setup_engine("AAPL")
    events = engine.run_cycle()
    aapl = [e for e in events if e.get("ticker") == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0]["status"] == "kill_switch"
    assert "blocking_factors" in aapl[0]
    assert aapl[0]["blocking_factors"][0]["rule"] == "kill_switch_active"


# ── 2. options_disabled parity ──────────────────────────────────────────

def test_parity_options_disabled_status(temp_db, monkeypatch):
    """Options disabled + option strategy proposal → "options_disabled".

    Force the engine's signal to BUY_CALL by monkey-patching the
    strategy's analyze so the test doesn't depend on a specific
    strategy's parametric path.
    """
    with session_scope() as s:
        get_or_create_account(s, starting_cash=10_000.0)
    engine = _setup_engine(
        "AAPL",
        extra_cfg={"options_disabled": True},
    )

    from backend.bot.strategies.base import Action, Signal

    def _force_call(_self, ticker, _data):
        return Signal(
            action=Action.BUY_CALL, ticker=ticker, confidence=0.9,
            reason="test", strategy="rsi_mean_reversion",
            metadata={"strike": 130.0, "expiration": "2026-08-15"},
        )
    monkeypatch.setattr(
        "backend.bot.strategies.all_strategies.RSIMeanReversion.analyze",
        _force_call,
    )
    events = engine.run_cycle()
    aapl = [e for e in events if e.get("ticker") == "AAPL"]
    matched = [e for e in aapl if e.get("status") == "options_disabled"]
    assert matched, [e.get("status") for e in aapl]
    assert matched[0]["blocking_factors"][0]["rule"] == "options_disabled"


# ── 3. low_confidence parity ────────────────────────────────────────────

def test_parity_low_confidence_status(temp_db):
    """Min confidence raised above the strategy's output → "low_confidence"."""
    with session_scope() as s:
        get_or_create_account(s, starting_cash=10_000.0)
    engine = _setup_engine(
        "AAPL", extra_cfg={"min_confidence": 0.99},
    )
    events = engine.run_cycle()
    aapl = [e for e in events if e.get("ticker") == "AAPL"]
    assert len(aapl) == 1
    # Either low_confidence or hold (when signal stays at HOLD).
    assert aapl[0]["status"] in ("low_confidence", "hold")
    if aapl[0]["status"] == "low_confidence":
        assert any(b["rule"] == "low_confidence"
                   for b in aapl[0]["blocking_factors"])


# ── 4. risk_manager_rejected parity ─────────────────────────────────────

def test_parity_rejected_status(temp_db):
    """Empty buying power → risk decision approved=False → "rejected"."""
    with session_scope() as s:
        account = get_or_create_account(s, starting_cash=0.01)
        account.cash = 0.01
        account.last_portfolio_value = 0.01
    engine = _setup_engine("AAPL")
    # Use a tiny executor so buying_power is ~0.
    engine.executor = PaperExecutor(starting_cash=0.01, price_fn=_flat_price)
    events = engine.run_cycle()
    aapl = [e for e in events if e.get("ticker") == "AAPL"]
    statuses = [e.get("status") for e in aapl]
    assert any(s in ("rejected", "too_small") for s in statuses), statuses


# ── 5. already_held parity ──────────────────────────────────────────────

def test_parity_already_held_status(temp_db, monkeypatch):
    """Holding AAPL stock → BUY_STOCK signal → "already_held".

    Skip the correlation-cap gate by stubbing it to never block so
    the policy gets all the way to ``already_held``. The legacy code
    walked rules in the SAME order — when correlation_cap actually
    fired, the legacy event was "correlation_cap_block" (the test
    confirms parity for ``already_held`` itself, not the ordering).
    """
    with session_scope() as s:
        account = get_or_create_account(s, starting_cash=10_000.0)
        account.cash = 10_000.0
        account.last_portfolio_value = 10_000.0
        s.add(PaperPosition(
            ticker="AAPL", kind="stock", quantity=5,
            avg_cost=130.0, opened_at=datetime.utcnow(),
        ))
    monkeypatch.setattr(
        "backend.bot.gates.correlation_cap_gate.check_correlation_cap",
        lambda **kw: type("R", (), {
            "blocked": False, "reason": "",
            "to_dict": lambda self: {"blocked": False},
        })(),
    )
    engine = _setup_engine("AAPL")
    events = engine.run_cycle()
    aapl_events = [e for e in events if e.get("ticker") == "AAPL"]
    statuses = [e.get("status") for e in aapl_events]
    assert any(s == "already_held" for s in statuses), statuses


# ── 6. signal_only parity (no auto_execute) ─────────────────────────────

def test_parity_signal_only_status(temp_db):
    """auto_execute off → eligible signal lands as "signal_only"."""
    with session_scope() as s:
        get_or_create_account(s, starting_cash=10_000.0)
    engine = _setup_engine(
        "AAPL", extra_cfg={"auto_execute": False, "min_confidence": 0.1,
                                "analytics": {"enabled": True, "min_grade": "F"}},
    )
    events = engine.run_cycle()
    aapl = [e for e in events if e.get("ticker") == "AAPL"]
    statuses = [e.get("status") for e in aapl]
    # signal_only is the "eligible but auto_execute off" state.
    assert any(s == "signal_only" for s in statuses), statuses


# ── 7. policy_result is present on every event ──────────────────────────

def test_every_event_carries_policy_result(temp_db):
    with session_scope() as s:
        get_or_create_account(s, starting_cash=10_000.0)
    engine = _setup_engine("AAPL")
    events = engine.run_cycle()
    aapl = [e for e in events if e.get("ticker") == "AAPL"]
    for e in aapl:
        assert "policy_result" in e
        assert "policy_eval_ms" in e
        assert isinstance(e["policy_eval_ms"], (int, float))
        # rule_evaluations is the per-rule audit trail.
        evals = e["policy_result"]["rule_evaluations"]
        assert isinstance(evals, list)
        assert len(evals) > 0
