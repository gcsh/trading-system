"""Meta-AI Reasoning — fake client; no network."""
import json

from backend.bot.meta_ai import MetaReasoner


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, text):
        self._text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _Resp(self._text)


class FakeClient:
    def __init__(self, text):
        self.messages = _Messages(text)


_ANALYTICS = {
    "regime": {"trend": "bullish", "volatility": "normal", "gamma": "long_gamma",
                "label": "bullish · normal-vol"},
    "probability": {"probability": 0.78, "direction": "LONG"},
    "rank": {"grade": "A", "score": 0.78},
    "features": {"composite_bias": 0.4, "flow_bullishness": 0.5},
}
_PORTFOLIO = {"top_sector": "Semis", "top_sector_pct": 0.55, "net_beta": 1.3,
              "macro_risk": "MODERATE", "concentration_flags": ["Semis concentration 55%"]}


def test_pass_through_when_no_key():
    m = MetaReasoner(api_key="")
    out = m.audit("AAPL", {"action": "BUY_STOCK"}, _ANALYTICS, _PORTFOLIO)
    assert out.approve is True
    assert out.risk_modifier == 1.0
    assert out.source == "pass_through"


def test_audit_parses_approval_and_clamps_modifier():
    payload = json.dumps({
        "approve": True, "confidence": 0.82, "risk_modifier": 0.7,
        "reasoning": ["regime aligned", "portfolio fine"],
    })
    m = MetaReasoner(client=FakeClient(payload))
    out = m.audit("AAPL", {"action": "BUY_STOCK"}, _ANALYTICS, _PORTFOLIO)
    assert out.approve is True
    assert out.risk_modifier == 0.7
    assert out.confidence == 0.82
    assert out.source == "claude"
    assert "regime aligned" in out.reasoning


def test_audit_clamps_out_of_range_modifier():
    payload = json.dumps({"approve": True, "confidence": 0.9, "risk_modifier": 1.7,
                          "reasoning": []})
    m = MetaReasoner(client=FakeClient(payload))
    out = m.audit("AAPL", {"action": "BUY_STOCK"}, _ANALYTICS, _PORTFOLIO)
    assert out.risk_modifier == 1.0       # capped at 1.0 (no leverage)


def test_audit_veto_path():
    payload = json.dumps({
        "approve": False, "confidence": 0.65, "risk_modifier": 0.5,
        "reasoning": ["portfolio already 70% semis", "fighting daily downtrend"],
    })
    m = MetaReasoner(client=FakeClient(payload))
    out = m.audit("AAPL", {"action": "BUY_STOCK"}, _ANALYTICS, _PORTFOLIO)
    assert out.approve is False
    assert "fighting daily downtrend" in out.reasoning


def test_audit_handles_malformed_response():
    m = MetaReasoner(client=FakeClient("not json"))
    out = m.audit("AAPL", {"action": "BUY_STOCK"}, _ANALYTICS, _PORTFOLIO)
    assert out.approve is True            # safe pass-through on parse failure
    assert out.source == "error"
