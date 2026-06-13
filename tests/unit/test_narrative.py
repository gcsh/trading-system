"""Narrative + Macro Intelligence — heuristic + Claude paths, all mocked."""
import json

from backend.bot.narrative import NarrativeAnalyzer, heuristic_narrative


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

    def create(self, **kwargs):
        return _Resp(self._text)


class FakeClient:
    def __init__(self, text):
        self.messages = _Messages(text)


def test_heuristic_picks_ai_infrastructure_when_chip_headlines_dominate():
    headlines = [
        "Nvidia unveils next-gen GPU for AI datacenter buildout",
        "AMD chip supplier confirms AI semiconductor demand soaring",
        "OpenAI says GPU shortage persists into next quarter",
        "TSMC expands semiconductor fab capacity for AI customers",
    ]
    state = heuristic_narrative(headlines, universe=["NVDA", "AMD", "AAPL", "AVGO"])
    assert state.dominant_theme == "AI infrastructure"
    assert "NVDA" in state.beneficiaries
    assert state.macro_risk == "LOW"
    assert state.source == "heuristic"
    assert state.themes and state.themes[0]["name"] == "AI infrastructure"


def test_heuristic_flags_high_macro_risk_on_recession_headlines():
    headlines = [
        "JPMorgan warns of looming recession",
        "Tech layoffs accelerate amid Fed rate hike",
        "Sovereign default risk rises as inflation crashes growth",
        "Oil prices collapse on demand fears",
        "Bankruptcy filings hit decade high",
    ]
    state = heuristic_narrative(headlines, universe=["SPY", "QQQ"])
    assert state.macro_risk == "HIGH"
    assert "recession" in state.summary.lower() or state.dominant_theme in (
        "Fed / rates", "Recession / risk-off", "Energy")


def test_heuristic_safe_with_empty_input():
    state = heuristic_narrative([])
    assert state.dominant_theme == "—"
    assert state.macro_risk == "LOW"
    assert state.beneficiaries == []


def test_analyzer_falls_back_to_heuristic_when_no_key():
    a = NarrativeAnalyzer(api_key="")
    state = a.analyze(["Nvidia AI chip demand soars"], universe=["NVDA"])
    assert state.source == "heuristic"
    assert state.dominant_theme == "AI infrastructure"


def test_analyzer_uses_claude_when_available():
    payload = json.dumps({
        "dominant_theme": "Fed easing cycle",
        "beneficiaries": ["spy", "qqq", "iwm"],   # uppercased by analyzer
        "macro_risk": "MODERATE",
        "summary": "Powell signaled rate cuts ahead.",
    })
    a = NarrativeAnalyzer(client=FakeClient(payload))
    state = a.analyze(["Powell signals rate cuts ahead"], universe=["SPY", "QQQ", "IWM"])
    assert state.source == "claude"
    assert state.dominant_theme == "Fed easing cycle"
    assert state.beneficiaries == ["SPY", "QQQ", "IWM"]
    assert state.macro_risk == "MODERATE"


def test_analyzer_falls_back_when_claude_returns_garbage():
    a = NarrativeAnalyzer(client=FakeClient("not json at all"))
    state = a.analyze(["Nvidia AI chip demand soars"], universe=["NVDA"])
    assert state.source == "heuristic"        # fell back gracefully
    assert state.dominant_theme == "AI infrastructure"
