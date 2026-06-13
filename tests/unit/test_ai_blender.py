"""Signal blender + Claude / ML wrappers — unit tests with mocks."""
from unittest.mock import MagicMock

from backend.bot.ai import SignalBlender
from backend.bot.ai.claude_signal import (
    ClaudeSignalGenerator,
    _parse_response,
    _to_signal,
    build_messages,
)
from backend.bot.ai.ml_signal import MLSignalModel, extract_features
from backend.bot.strategies.base import Action, Signal


# -- claude --------------------------------------------------------------

def test_parse_response_extracts_json_object():
    text = 'Sure thing: {"action": "BUY_STOCK", "confidence": 0.8, "reasoning": "ok"} done'
    parsed = _parse_response(text)
    assert parsed["action"] == "BUY_STOCK"


def test_parse_response_rejects_bad_text():
    import pytest
    with pytest.raises(ValueError):
        _parse_response("no json here")


def test_to_signal_handles_unknown_action():
    sig = _to_signal("AAPL", {"action": "TELEPORT", "confidence": 0.9, "reasoning": "x"})
    assert sig.action == Action.HOLD


def test_to_signal_clamps_confidence():
    sig = _to_signal("AAPL", {"action": "BUY_STOCK", "confidence": 1.5, "reasoning": "x"})
    assert sig.confidence == 1.0
    sig = _to_signal("AAPL", {"action": "BUY_STOCK", "confidence": -1.0, "reasoning": "x"})
    assert sig.confidence == 0.0


def test_build_messages_includes_ticker_and_snapshot():
    msgs = build_messages("AAPL", {"price": 100, "rsi": 50}, [])
    assert msgs[0]["role"] == "user"
    assert "AAPL" in msgs[0]["content"]
    assert "rsi: 50" in msgs[0]["content"]


def test_claude_unavailable_returns_hold():
    gen = ClaudeSignalGenerator(api_key="")
    sig = gen.analyze("AAPL", {"price": 100})
    assert sig.action == Action.HOLD
    assert "missing" in sig.reason


def test_claude_with_mock_client_parses_response():
    client = MagicMock()
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = '{"action": "BUY_STOCK", "confidence": 0.7, "reasoning": "good setup"}'
    response.content = [block]
    client.messages.create.return_value = response
    gen = ClaudeSignalGenerator(api_key="fake", client=client)
    sig = gen.analyze("AAPL", {"price": 100, "rsi": 45}, news=[])
    assert sig.action == Action.BUY_STOCK
    assert sig.confidence == 0.7


# -- ml ------------------------------------------------------------------

def test_extract_features_returns_expected_length():
    feats = extract_features({"price": 100, "ma50": 95})
    from backend.bot.ai.ml_signal import FEATURE_NAMES

    assert len(feats) == len(FEATURE_NAMES)


def test_extract_features_uses_defaults_for_missing():
    feats = extract_features({})
    assert all(isinstance(f, float) for f in feats)


def test_ml_unavailable_returns_hold():
    model = MLSignalModel(model_path="/tmp/__nope__.txt")
    sig = model.analyze("AAPL", {"price": 100})
    assert sig.action == Action.HOLD


def test_ml_with_mocked_booster():
    model = MLSignalModel(model_path="/tmp/__nope__.txt")
    model._booster = MagicMock()
    model._booster.predict.return_value = [0.75]
    sig = model.analyze("AAPL", {"price": 100})
    assert sig.action == Action.BUY_STOCK
    assert sig.confidence > 0


def test_ml_neutral_when_close_to_50():
    model = MLSignalModel(model_path="/tmp/__nope__.txt")
    model._booster = MagicMock()
    model._booster.predict.return_value = [0.5]
    sig = model.analyze("AAPL", {"price": 100})
    assert sig.action == Action.HOLD


# -- blender -------------------------------------------------------------

def test_blender_returns_rule_signal_when_ai_disabled():
    blender = SignalBlender(
        claude=ClaudeSignalGenerator(api_key=""),
        ml=MLSignalModel(model_path="/tmp/__nope__.txt"),
    )
    rule = Signal(ticker="AAPL", action=Action.BUY_STOCK, confidence=0.7, strategy="momentum")
    out = blender.blend("AAPL", {"price": 100}, rule, ai_config={"claude_enabled": False, "ml_enabled": False})
    assert out.action == Action.BUY_STOCK
    assert out.confidence == 0.7


def test_blender_combines_with_ml_when_enabled():
    ml = MLSignalModel(model_path="/tmp/__nope__.txt")
    ml._booster = MagicMock()
    ml._booster.predict.return_value = [0.8]
    blender = SignalBlender(
        claude=ClaudeSignalGenerator(api_key=""),
        ml=ml,
    )
    rule = Signal(ticker="AAPL", action=Action.BUY_STOCK, confidence=0.6, strategy="momentum")
    out = blender.blend(
        "AAPL", {"price": 100}, rule,
        ai_config={"claude_enabled": False, "ml_enabled": True, "ml_weight": 0.5},
    )
    assert out.action == Action.BUY_STOCK
    assert "ai_components" in out.metadata
    assert "ml" in out.metadata["ai_components"]


def test_blender_holds_when_all_sources_hold():
    ml = MLSignalModel(model_path="/tmp/__nope__.txt")
    ml._booster = MagicMock()
    ml._booster.predict.return_value = [0.5]
    blender = SignalBlender(claude=ClaudeSignalGenerator(api_key=""), ml=ml)
    rule = Signal.hold("AAPL", "test", "no signal")
    out = blender.blend(
        "AAPL", {"price": 100}, rule,
        ai_config={"claude_enabled": False, "ml_enabled": True, "ml_weight": 0.5},
    )
    assert out.action == Action.HOLD
