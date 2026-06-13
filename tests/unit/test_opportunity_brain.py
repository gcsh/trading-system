"""MITS Phase 7.2 — Opportunity Brain tests (mocked Claude)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.bot.ai.opportunity_brain import (
    OpportunityBrain,
    OpportunityHypothesis,
    _five_min_bucket,
)


def _mk_claude_client(reply: dict) -> MagicMock:
    """Build a fake Anthropic client that returns the given dict."""
    client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = json.dumps(reply)
    response = MagicMock()
    response.content = [text_block]
    # Cost meter introspects usage; provide a minimal shape.
    response.usage = MagicMock(
        input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    response.model = "claude-sonnet-4-6"
    client.messages.create.return_value = response
    return client


def test_returns_none_on_normal_regime():
    client = _mk_claude_client({
        "ticker": "SPY", "direction": "long_call",
        "dte_bucket": "1d", "conviction": 0.9, "thesis": "x", "notes": "y",
    })
    brain = OpportunityBrain(api_key="dummy", client=client)
    assert brain.analyze("normal", {"any": "ctx"}) is None
    # Even though the regime is normal, no Claude call fires.
    client.messages.create.assert_not_called()


def test_returns_hypothesis_on_panic_regime():
    payload = {
        "ticker": "SPY", "direction": "long_put",
        "dte_bucket": "0d", "conviction": 0.8,
        "thesis": "Cap rally, SPY bouncing on -2% open w/ VIX 30",
        "notes": "Invalidated if SPY reclaims VWAP",
    }
    client = _mk_claude_client(payload)
    brain = OpportunityBrain(api_key="dummy", client=client)
    hyp = brain.analyze("panic", {"spy_ticks_5min": []})
    assert isinstance(hyp, OpportunityHypothesis)
    assert hyp.regime_state == "panic"
    assert hyp.direction == "long_put"
    assert hyp.conviction == pytest.approx(0.8)
    assert hyp.from_cache is False


def test_cache_hit_avoids_duplicate_claude_call():
    payload = {
        "ticker": "SPY", "direction": "long_put",
        "dte_bucket": "0d", "conviction": 0.85, "thesis": "...", "notes": "...",
    }
    client = _mk_claude_client(payload)
    brain = OpportunityBrain(api_key="dummy", client=client)
    h1 = brain.analyze("panic", {})
    h2 = brain.analyze("panic", {})
    assert h1 is not None and h2 is not None
    # Same 5-min bucket → only one Claude call.
    assert client.messages.create.call_count == 1
    assert h2.from_cache is True
    assert h2.direction == "long_put"


def test_cache_is_per_regime_state():
    payload = {
        "ticker": "SPY", "direction": "long_put",
        "dte_bucket": "0d", "conviction": 0.85, "thesis": "...", "notes": "...",
    }
    client = _mk_claude_client(payload)
    brain = OpportunityBrain(api_key="dummy", client=client)
    brain.analyze("panic", {})
    brain.analyze("squeeze", {})
    # Different regime, different cache key → second Claude call fires.
    assert client.messages.create.call_count == 2


def test_conviction_floor_does_not_block_at_brain_layer():
    """Brain returns whatever Claude said; the gate enforces the floor."""
    payload = {
        "ticker": "SPY", "direction": "long_call",
        "dte_bucket": "1d", "conviction": 0.30,
        "thesis": "weak", "notes": "weak",
    }
    client = _mk_claude_client(payload)
    brain = OpportunityBrain(api_key="dummy", client=client)
    hyp = brain.analyze("panic", {})
    assert hyp.conviction == pytest.approx(0.30)


def test_to_dict_serializes_hypothesis():
    h = OpportunityHypothesis(
        ticker="SPY", direction="long_put", dte_bucket="0d",
        conviction=0.7, thesis="t", notes="n",
        regime_state="panic",
    )
    d = h.to_dict()
    assert d["ticker"] == "SPY"
    assert d["conviction"] == 0.7
    assert d["regime_state"] == "panic"


def test_five_min_bucket_advances_with_time():
    a = _five_min_bucket(now_ts=1_700_000_000)
    b = _five_min_bucket(now_ts=1_700_000_000 + 1)
    c = _five_min_bucket(now_ts=1_700_000_000 + 301)
    assert a == b
    assert c == a + 1


def test_handles_garbage_claude_response_gracefully():
    """Malformed JSON should yield None rather than raising."""
    bad = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "not json at all"
    response = MagicMock()
    response.content = [text_block]
    response.usage = MagicMock(
        input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    response.model = "claude-sonnet-4-6"
    bad.messages.create.return_value = response
    brain = OpportunityBrain(api_key="dummy", client=bad)
    assert brain.analyze("panic", {}) is None


def test_available_property_true_when_client_set():
    client = _mk_claude_client({
        "ticker": "X", "direction": "skip",
        "dte_bucket": "1d", "conviction": 0.0, "thesis": "x", "notes": "y",
    })
    brain = OpportunityBrain(api_key="dummy", client=client)
    assert brain.available is True


def test_available_false_with_no_key_and_no_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    brain = OpportunityBrain(api_key="")
    # Stub anthropic_key to return empty.
    import backend.config as cfg
    monkeypatch.setattr(cfg, "anthropic_key", lambda: "")
    assert brain.available is False
