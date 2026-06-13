"""MITS Phase 14.A — hybrid ensemble fast/deep disagreement signal."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.bot.analysis.hybrid import (
    DISAGREEMENT_RANK_DELTA,
    compose_hybrid,
)


pytestmark = [pytest.mark.unit]


def _stub_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


class _StubClient:
    def __init__(self, text: str):
        self.text = text
        self.messages = SimpleNamespace(create=lambda **kw: _stub_response(self.text))


def _kn(post=0.65, n=200, lo=0.6, hi=0.7, pattern="bull_flag"):
    return {
        pattern: {
            "posterior_win_rate": post,
            "sample_size": n,
            "confidence_lower": lo,
            "confidence_upper": hi,
            "regime": "trending_up",
        }
    }


def _bars():
    return [{"close": 100.0}]


def _deep_payload(*, pattern="bull_flag", action="BUY_CALL", conf=0.6,
                       headline="Bull Flag on NVDA — 65% over 200",
                       paragraph="The bull flag posterior reads 65% over 200 historical trades."):
    if action is None:
        sa = "null"
    else:
        sa = (
            '{"action": "' + action + '", "strike": 101.0, '
            '"expiry": "30", "rationale": "x"}'
        )
    return (
        '{"summary": "ok", "theses": {"' + pattern + '": {'
        '"headline": "' + headline + '",'
        '"thesis_paragraph": "' + paragraph + '",'
        '"suggested_action": ' + sa + ','
        '"invalidation": ["close below VWAP"],'
        '"confidence_self_assessment": ' + f"{conf:.2f}" +
        '}}}'
    )


def test_no_deep_call_when_knowledge_empty():
    ensemble = compose_hybrid(
        ticker="NVDA", window="today", knowledge={},
        observations=[], bars=_bars(),
    )
    assert ensemble.deep is None
    assert ensemble.fast == {}
    assert ensemble.uncertainty_signal == {}


def test_fast_only_when_no_api_key():
    knowledge = _kn(post=0.65, n=200)
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=None,
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(),
        )
    assert ensemble.deep is None
    assert "bull_flag" in ensemble.chosen
    assert ensemble.chosen["bull_flag"]["source"] == "fast"


def test_disagreement_flagged_when_actions_diverge():
    knowledge = _kn(post=0.65, n=200, lo=0.6, hi=0.7, pattern="bull_flag")
    # Deep returns BUY_PUT — opposite of fast's BUY_CALL.
    payload = _deep_payload(action="BUY_PUT", conf=0.6)
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(),
        )
    sig = ensemble.uncertainty_signal["bull_flag"]
    assert sig["direction_match"] is False
    assert sig["flagged"] is True


def test_disagreement_flagged_when_rank_delta_large():
    """Fast rank is high (~0.5 for post=0.65 n=200) but deep
    self-confidence is very low → flagged."""
    knowledge = _kn(post=0.65, n=200, lo=0.6, hi=0.7, pattern="bull_flag")
    payload = _deep_payload(action="BUY_CALL", conf=0.05)
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(),
        )
    sig = ensemble.uncertainty_signal["bull_flag"]
    assert sig["direction_match"] is True
    assert sig["rank_delta"] is not None
    assert sig["rank_delta"] > DISAGREEMENT_RANK_DELTA
    assert sig["flagged"] is True


def test_aligned_fast_and_deep_not_flagged():
    """When fast and deep both call BUY_CALL with similar
    confidence, the signal is not flagged."""
    knowledge = _kn(post=0.65, n=200, lo=0.6, hi=0.7, pattern="bull_flag")
    payload = _deep_payload(action="BUY_CALL", conf=0.50)
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(),
        )
    sig = ensemble.uncertainty_signal["bull_flag"]
    assert sig["direction_match"] is True
    assert sig["flagged"] is False


def test_chosen_uses_deep_when_available():
    knowledge = _kn(post=0.65, n=200, lo=0.6, hi=0.7)
    payload = _deep_payload(action="BUY_CALL", conf=0.5,
                                  headline="Custom Deep Headline",
                                  paragraph="Deep custom paragraph that is long enough to satisfy schema.")
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(),
        )
    chosen = ensemble.chosen["bull_flag"]
    assert chosen["source"] == "deep"
    assert "Custom Deep Headline" in chosen["headline"]


def test_chosen_falls_back_to_fast_when_deep_skips_pattern():
    """If the deep call only produced a thesis for a subset, fast fills
    in the rest."""
    knowledge = {
        "bull_flag": _kn(post=0.65, n=200)["bull_flag"],
        "bear_flag": _kn(post=0.62, n=120, pattern="bear_flag")["bear_flag"],
    }
    # Deep only emits bull_flag.
    payload = _deep_payload(action="BUY_CALL", conf=0.5)
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(), deep_top_n=2,
        )
    assert ensemble.chosen["bull_flag"]["source"] == "deep"
    assert ensemble.chosen["bear_flag"]["source"] == "fast"
    # Bear flag has no deep counterpart → direction_match is None
    # (no deep action to compare with).
    assert ensemble.uncertainty_signal["bear_flag"]["direction_match"] is None


def test_to_dict_carries_top_level_keys():
    knowledge = _kn(post=0.65, n=200)
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=None,
    ):
        ensemble = compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(),
        )
    d = ensemble.to_dict()
    for key in ("fast", "deep", "chosen", "summary", "uncertainty_signal"):
        assert key in d


def test_deep_top_n_picks_highest_ranked_patterns():
    """When more patterns than top_n exist, only the highest-ranked
    fast results get the deep treatment."""
    knowledge = {
        "bull_flag": {"posterior_win_rate": 0.8, "sample_size": 300,
                       "confidence_lower": 0.75, "confidence_upper": 0.85,
                       "regime": "trending_up"},
        "bear_flag": {"posterior_win_rate": 0.55, "sample_size": 50,
                       "confidence_lower": 0.45, "confidence_upper": 0.65,
                       "regime": "trending_up"},
    }
    seen_keys = {}

    def _client_factory():
        return _StubClient(_deep_payload(action="BUY_CALL", conf=0.7))

    real_compose = None

    def _spy_compose(**kwargs):
        seen_keys["k"] = list((kwargs.get("knowledge") or {}).keys())
        return None  # pretend deep failed so we don't have to mock its parsing

    with patch(
        "backend.bot.analysis.deep_composer.deep_compose",
        side_effect=_spy_compose,
    ):
        compose_hybrid(
            ticker="NVDA", window="today", knowledge=knowledge,
            observations=[], bars=_bars(), deep_top_n=1,
        )
    assert seen_keys["k"] == ["bull_flag"]
