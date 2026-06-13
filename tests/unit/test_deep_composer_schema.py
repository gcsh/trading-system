"""MITS Phase 14.A — deep composer pydantic validation."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.bot.analysis.deep_composer import (
    DeepComposerOutput,
    DeepThesisSchema,
    SuggestedActionSchema,
    deep_compose,
)


pytestmark = [pytest.mark.unit]


def _stub_response(text: str):
    """Build a stub anthropic response object whose .content[0].text
    yields ``text``."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


class _StubClient:
    def __init__(self, text: str):
        self.text = text
        self.messages = SimpleNamespace(create=lambda **kw: _stub_response(self.text))


@pytest.fixture(autouse=True)
def _mute_anthropic_key():
    """Force `_claude_client` to return our stub by monkeypatching at
    the import site."""
    yield


def _kn(post=0.65, n=200, lo=0.58, hi=0.72):
    return {
        "bull_flag": {
            "posterior_win_rate": post,
            "sample_size": n,
            "confidence_lower": lo,
            "confidence_upper": hi,
            "regime": "trending_up",
        }
    }


def _bars():
    return [{"close": 100.0}]


def test_pydantic_rejects_garbage_json():
    """Claude returns text that is not JSON — pydantic path → None."""
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient("this is not JSON at all"),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is None


def test_pydantic_rejects_missing_required_fields():
    """JSON missing `theses` should still yield a DeepComposerOutput
    only when summary parses cleanly, but a thesis missing its required
    keys is dropped."""
    bad = (
        '{"summary": "hello world", '
        '"theses": {"bull_flag": {"headline": "too_short"}}}'
    )
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(bad),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is not None
    assert out.theses == {}


def test_pydantic_rejects_short_headline():
    """headline min_length=10 — anything shorter is dropped from theses."""
    payload = (
        '{"summary": "ok", "theses": {"bull_flag": {'
        '"headline": "short",'
        '"thesis_paragraph": "this thesis paragraph is at least twenty characters long",'
        '"suggested_action": null,'
        '"invalidation": ["close below"],'
        '"confidence_self_assessment": 0.5'
        '}}}'
    )
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is not None
    assert "bull_flag" not in out.theses


def test_pydantic_rejects_confidence_out_of_range():
    payload = (
        '{"summary": "ok", "theses": {"bull_flag": {'
        '"headline": "Bull Flag on NVDA",'
        '"thesis_paragraph": "this thesis paragraph is at least twenty characters long",'
        '"suggested_action": null,'
        '"invalidation": ["close below"],'
        '"confidence_self_assessment": 1.5'
        '}}}'
    )
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is not None
    assert "bull_flag" not in out.theses


def test_pydantic_accepts_well_formed_payload():
    payload = (
        '{"summary": "NVDA looks healthy.", "theses": {"bull_flag": {'
        '"headline": "Bull Flag on NVDA — 65% historical (N=200)",'
        '"thesis_paragraph": "Posterior is healthy at 65 percent over 200 trades.",'
        '"suggested_action": {"action": "BUY_CALL", "strike": 101.0, '
        '"expiry": "30", "rationale": "OTM bias"},'
        '"invalidation": ["close below VWAP"],'
        '"confidence_self_assessment": 0.6'
        '}}}'
    )
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is not None
    assert "bull_flag" in out.theses
    t = out.theses["bull_flag"]
    assert t.headline.startswith("Bull Flag on NVDA")
    assert t.confidence_self_assessment == pytest.approx(0.6)
    assert t.suggested_action is not None
    assert t.suggested_action.action == "BUY_CALL"


def test_returns_none_when_knowledge_empty():
    """No patterns to compose — fast path doesn't call Claude at all."""
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient("{}"),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge={},
            observations=[], bars=_bars(),
        )
    assert out is None


def test_returns_none_when_no_api_key():
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=None,
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is None


def test_invalidation_length_constrained():
    """invalidation list must have 1-6 entries; reject if empty."""
    payload = (
        '{"summary": "ok", "theses": {"bull_flag": {'
        '"headline": "Bull Flag on NVDA — 65%",'
        '"thesis_paragraph": "this thesis paragraph is at least twenty characters long",'
        '"suggested_action": null,'
        '"invalidation": [],'
        '"confidence_self_assessment": 0.5'
        '}}}'
    )
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is not None
    assert "bull_flag" not in out.theses


def test_suggested_action_schema_can_be_null():
    """suggested_action being null is valid and preserved."""
    payload = (
        '{"summary": "ok", "theses": {"bull_flag": {'
        '"headline": "Bull Flag on NVDA — 65%",'
        '"thesis_paragraph": "this thesis paragraph is at least twenty characters long",'
        '"suggested_action": null,'
        '"invalidation": ["close below VWAP"],'
        '"confidence_self_assessment": 0.5'
        '}}}'
    )
    with patch(
        "backend.bot.analysis.deep_composer._claude_client",
        return_value=_StubClient(payload),
    ):
        out = deep_compose(
            ticker="NVDA", window="today", knowledge=_kn(),
            observations=[], bars=_bars(),
        )
    assert out is not None
    assert out.theses["bull_flag"].suggested_action is None


def test_pydantic_schema_direct_validation():
    """Sanity that DeepThesisSchema rejects bad ranges on direct construction."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DeepThesisSchema.model_validate({
            "headline": "h" * 5,  # too short
            "thesis_paragraph": "p" * 30,
            "invalidation": ["x"],
            "confidence_self_assessment": 0.5,
        })


def test_to_dict_roundtrips_summary_and_theses():
    out = DeepComposerOutput(
        summary="ok",
        theses={
            "bull_flag": DeepThesisSchema(
                headline="Bull Flag on NVDA",
                thesis_paragraph="this thesis paragraph is at least twenty characters long",
                suggested_action=None,
                invalidation=["close below VWAP"],
                confidence_self_assessment=0.5,
            )
        },
    )
    d = out.to_dict()
    assert d["summary"] == "ok"
    assert "bull_flag" in d["theses"]
    assert d["theses"]["bull_flag"]["confidence_self_assessment"] == 0.5
