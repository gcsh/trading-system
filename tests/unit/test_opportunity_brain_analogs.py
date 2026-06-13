"""MITS Phase 8.7 — Opportunity Brain vector-analog wiring tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.bot.ai import opportunity_brain as ob


@pytest.fixture()
def fake_vector_store(monkeypatch):
    """Stub vector_store.embed + similarity_search so the Brain's
    analog block builder runs deterministically."""
    import sys
    fake = MagicMock()
    fake.embed.return_value = [0.1] * 384
    fake.similarity_search.side_effect = lambda namespace, qv, k=10, min_cosine=0.7: (
        [
            SimpleNamespace(
                namespace=namespace, key="day_a", cosine=0.94,
                metadata={"date": "2020-03-12", "regime": "panic"},
            ),
            SimpleNamespace(
                namespace=namespace, key="day_b", cosine=0.89,
                metadata={"date": "2018-12-24", "regime": "panic"},
            ),
        ] if namespace == "regime_snapshots" else [
            SimpleNamespace(
                namespace=namespace, key="trade-1", cosine=0.72,
                metadata={
                    "ticker": "SPY", "strategy": "long_put",
                    "outcome": "win", "pnl": 217.0,
                },
            ),
        ]
    )
    # _fetch_analogs uses a lazy `from backend.bot.ai import vector_store`,
    # so install the stub at sys.modules AND on the package attribute.
    import backend.bot.ai as _ai_pkg
    orig = sys.modules.get("backend.bot.ai.vector_store")
    orig_attr = getattr(_ai_pkg, "vector_store", None)
    sys.modules["backend.bot.ai.vector_store"] = fake
    setattr(_ai_pkg, "vector_store", fake)
    yield fake
    if orig is not None:
        sys.modules["backend.bot.ai.vector_store"] = orig
    else:
        sys.modules.pop("backend.bot.ai.vector_store", None)
    if orig_attr is not None:
        setattr(_ai_pkg, "vector_store", orig_attr)


def test_fetch_analogs_returns_topn_with_trades(fake_vector_store):
    analogs = ob._fetch_analogs("panic", {
        "vix": 30.0, "breadth": 0.18, "put_call": 1.3,
        "spy_30m_change_pct": -1.5, "flow_summary": "QQQ 1DTE puts heavy",
    })
    assert len(analogs) >= 1
    first = analogs[0]
    assert first["date"] == "2020-03-12"
    assert "top_trades" in first
    assert first["cosine"] >= 0.7


def test_fetch_analogs_returns_empty_on_no_hits(monkeypatch):
    import sys
    import backend.bot.ai as _ai_pkg
    fake = MagicMock()
    fake.embed.return_value = [0.1] * 384
    fake.similarity_search.return_value = []
    orig = sys.modules.get("backend.bot.ai.vector_store")
    orig_attr = getattr(_ai_pkg, "vector_store", None)
    sys.modules["backend.bot.ai.vector_store"] = fake
    setattr(_ai_pkg, "vector_store", fake)
    try:
        analogs = ob._fetch_analogs("panic", {"vix": 30.0})
        assert analogs == []
    finally:
        if orig is not None:
            sys.modules["backend.bot.ai.vector_store"] = orig
        else:
            sys.modules.pop("backend.bot.ai.vector_store", None)
        if orig_attr is not None:
            setattr(_ai_pkg, "vector_store", orig_attr)


def test_format_analog_block_renders_dates():
    block = ob._format_analog_block([
        {"date": "2020-03-12", "regime": "panic", "cosine": 0.94,
            "top_trades": [
                {"ticker": "SPY", "strategy": "long_put",
                    "outcome": "win", "pnl": 217.0},
            ]},
        {"date": "2018-12-24", "regime": "panic", "cosine": 0.89,
            "top_trades": []},
    ])
    assert "Today most resembles" in block
    assert "2020-03-12" in block
    assert "2018-12-24" in block
    assert "SPY" in block


def test_format_analog_block_empty_returns_empty():
    assert ob._format_analog_block([]) == ""


def test_analyze_falls_back_when_no_anthropic(monkeypatch):
    """No API key → analyze returns None; analog code never runs."""
    brain = ob.OpportunityBrain(api_key=None, client=None)
    monkeypatch.setattr(ob, "anthropic_key", lambda: "", raising=False)
    monkeypatch.setattr(brain, "_key", lambda: "")
    assert brain.available is False
    assert brain.analyze("panic", {"vix": 30.0}) is None
