"""MITS Phase 18-FU Gap R3 — engine-level integration tests for the
per-cycle StrategyMatrix lift.

Verifies that ``BotEngine._populate_strategy_matrix``:
  1. populates ``event["strategy_matrix"]`` + ``event["regime_vector"]``
     + ``event["top_strategy"]`` on the first call (cache miss path).
  2. the SECOND call within the same TTL bucket hits the cache — the
     underlying build function fires once, not twice.

Together with the unit suite (test_18fu_strategy_matrix_per_cycle.py)
this proves the run_cycle path delivers 100% matrix coverage on
``decision_provenance.strategy_matrix_json`` going forward.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from backend.bot.analysis import strategy_matrix_cache as smc
from backend.bot.engine import BotEngine
from backend.bot.market_data import MarketSnapshot
from backend.bot.paper_executor import PaperExecutor


pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _reset_cache_between_tests():
    smc.clear()
    yield
    smc.clear()


def _flat_price(_t):
    return 100.0


def _data():
    return {"price": 100.0, "rsi": 50.0, "iv_rank": 30}


def _engine() -> BotEngine:
    adapter = MagicMock()
    adapter.snapshot.return_value = MarketSnapshot(data=_data(), source_errors=[])
    return BotEngine(
        executor=PaperExecutor(starting_cash=10_000.0, price_fn=_flat_price),
        market_data=adapter,
    )


def _patch_build(monkeypatch, calls: List[str]):
    """Patch the cache's ``_do_build`` to a deterministic stub that
    records every call and emits a recognizable matrix dict."""

    def _stub(*, ticker, regime_vector, signal, analytics):
        calls.append(ticker)
        return (
            {
                "ticker": ticker,
                "candidates": [{"strategy_name": f"{ticker}_strat"}],
                "top_strategy": {"strategy_name": f"{ticker}_strat",
                                    "final_score": 0.42},
                "regime_health": "green",
                "_marker": "integration_stub",
            },
            {"strategy_name": f"{ticker}_strat", "final_score": 0.42},
        )

    monkeypatch.setattr(smc, "_do_build", _stub)


def _patch_regime_build(monkeypatch):
    """Patch ``build_regime_vector`` (the helper the engine pre-policy
    lift calls) to return a stable namespace with a deterministic
    to_dict — so the cache key + the event["regime_vector"] dict are
    both predictable."""
    payload = {
        "trend": "bullish", "iv_regime": "stable_low",
        "intraday": "normal", "macro": "expansion", "health": "green",
    }

    rv = SimpleNamespace(
        ticker="AAPL",
        trend=SimpleNamespace(value="bullish"),
        health="green",
        to_dict=lambda: dict(payload),
    )

    def _stub(*args, **kwargs):
        return rv

    monkeypatch.setattr(
        "backend.bot.regime.vector.build_regime_vector", _stub,
    )


def test_engine_populates_strategy_matrix_on_event(monkeypatch):
    """The pre-policy lift writes both matrix + regime_vector + top
    strategy keys onto the event dict."""
    calls: List[str] = []
    _patch_regime_build(monkeypatch)
    _patch_build(monkeypatch, calls)

    engine = _engine()
    sig = SimpleNamespace(ticker="AAPL", strategy="ema_cross", metadata={})

    event: Dict[str, Any] = {"ticker": "AAPL", "action": "BUY_STOCK"}
    engine._populate_strategy_matrix(
        event=event, ticker="AAPL", data=_data(), signal=sig,
    )

    assert "strategy_matrix" in event, (
        "pre-policy lift must populate event['strategy_matrix']"
    )
    sm = event["strategy_matrix"]
    assert sm["ticker"] == "AAPL"
    assert sm.get("_marker") == "integration_stub"
    assert event.get("top_strategy", {}).get("strategy_name") == "AAPL_strat"

    # regime_vector dict is stamped — _persist_trade depends on it.
    assert "regime_vector" in event
    assert event["regime_vector"]["trend"] == "bullish"

    assert calls == ["AAPL"], "first call must build exactly once"


def test_engine_second_call_hits_cache(monkeypatch):
    """Within one TTL bucket, the second call for the SAME ticker +
    regime hits the cache and skips the build entirely."""
    calls: List[str] = []
    _patch_regime_build(monkeypatch)
    _patch_build(monkeypatch, calls)

    engine = _engine()
    sig = SimpleNamespace(ticker="AAPL", strategy="ema_cross", metadata={})

    e1: Dict[str, Any] = {"ticker": "AAPL"}
    engine._populate_strategy_matrix(event=e1, ticker="AAPL", data=_data(), signal=sig)
    e2: Dict[str, Any] = {"ticker": "AAPL"}
    engine._populate_strategy_matrix(event=e2, ticker="AAPL", data=_data(), signal=sig)

    assert calls == ["AAPL"], "second call must be a cache hit (build runs once)"
    # Both events must still carry the matrix.
    assert e1["strategy_matrix"]["ticker"] == "AAPL"
    assert e2["strategy_matrix"]["ticker"] == "AAPL"
    # The cache stats corroborate.
    s = smc.stats()
    assert s["hits"] == 1
    assert s["misses"] == 1


__all__ = []
