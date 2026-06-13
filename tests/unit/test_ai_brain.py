"""AI Brain (autonomous decisions) + chat copilot — fully mocked, no network."""
from backend.bot.ai.brain import AutonomousBrain
from backend.bot.ai import chat as chatmod


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


def test_brain_unavailable_without_key():
    assert AutonomousBrain(api_key="").available is False
    assert AutonomousBrain(api_key="sk-test").available is True
    # No key and no snapshots → empty, never raises.
    assert AutonomousBrain(api_key="").decide_portfolio({"AAPL": {"price": 1}}) == {}


def test_brain_maps_decisions_to_signals():
    payload = (
        '{"decisions": ['
        '{"ticker": "AAPL", "action": "BUY_STOCK", "confidence": 0.82, "stop_loss_pct": 4,'
        ' "take_profit_pct": 9, "approach": "gamma squeeze", "reasoning": "below call wall, bullish flow"},'
        '{"ticker": "TSLA", "action": "BUY_CALL", "confidence": 0.7, "stop_loss_pct": 30,'
        ' "take_profit_pct": 60, "approach": "breakout", "reasoning": "momentum"},'
        '{"ticker": "MSFT", "action": "HOLD", "confidence": 0.2, "approach": "wait", "reasoning": "mixed"}'
        ']}'
    )
    brain = AutonomousBrain(client=FakeClient(payload))
    snaps = {"AAPL": {"price": 150.0}, "TSLA": {"price": 250.0}, "MSFT": {"price": 400.0}}
    sigs = brain.decide_portfolio(snaps, {"web_research": False})

    assert set(sigs) == {"AAPL", "TSLA", "MSFT"}
    assert sigs["AAPL"].action.name == "BUY_STOCK"
    assert sigs["AAPL"].strategy == "ai_brain"
    assert sigs["AAPL"].stop_loss == 4 and sigs["AAPL"].take_profit == 9
    # The full reasoning is the auditable `reason`; the short label is metadata.
    assert sigs["AAPL"].reason == "below call wall, bullish flow"
    assert sigs["AAPL"].metadata["approach"] == "gamma squeeze"
    # Option action gets a strike + dte from the snapshot price.
    assert sigs["TSLA"].action.name == "BUY_CALL"
    assert sigs["TSLA"].strike == 250 and sigs["TSLA"].dte == 7
    assert sigs["MSFT"].action.name == "HOLD"


def test_brain_ignores_unknown_tickers_and_bad_actions():
    payload = (
        '{"decisions": ['
        '{"ticker": "ZZZZ", "action": "BUY_STOCK", "confidence": 0.9},'
        '{"ticker": "AAPL", "action": "NONSENSE", "confidence": 1.5}'
        ']}'
    )
    brain = AutonomousBrain(client=FakeClient(payload))
    sigs = brain.decide_portfolio({"AAPL": {"price": 10.0}}, {})
    assert "ZZZZ" not in sigs                       # invented ticker dropped
    assert sigs["AAPL"].action.name == "HOLD"        # bad action coerced to HOLD
    assert 0.0 <= sigs["AAPL"].confidence <= 1.0     # confidence clamped


def test_brain_passes_web_search_tool_when_requested():
    brain = AutonomousBrain(client=FakeClient('{"decisions": []}'))
    brain.decide_portfolio({"AAPL": {"price": 1}}, {"web_research": True})
    tools = brain._client.messages.kwargs.get("tools")
    assert tools and tools[0]["name"] == "web_search"


def test_anthropic_key_resolves_from_saved_config(temp_db, monkeypatch):
    from backend.config import anthropic_key
    from backend.db import session_scope
    from backend.models.config import load_config, save_config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert anthropic_key() == ""                 # nothing configured yet
    with session_scope() as s:
        cfg = load_config(s)
        cfg["anthropic_api_key"] = "sk-saved"
        save_config(s, cfg)
    # Picked up at runtime (no restart) — and a default brain goes available.
    assert anthropic_key() == "sk-saved"
    assert AutonomousBrain().available is True


def test_brain_only_runs_when_bot_is_on(temp_db, monkeypatch):
    """The AI Brain must reason only while the bot is ON (running). In watch-only
    / stopped state it stays armed — no (paid) brain calls on a stray cycle."""
    from unittest.mock import MagicMock

    from backend.bot.engine import BotEngine
    from backend.db import session_scope
    from backend.models.config import load_config, save_config

    with session_scope() as s:
        cfg = load_config(s)
        cfg["tickers"] = ["AAPL"]
        cfg["strategy"] = "rsi_mean_reversion"      # concrete rule strat for the off path
        cfg["auto_execute"] = False
        # Calendar gate would otherwise return market_closed during tests.
        cfg["force_run_when_closed"] = True
        # auto_market_mode would otherwise flip brain_enabled off when the
        # market is closed at test time (most CI runs).
        cfg["auto_market_mode"] = False
        cfg["ai"] = {**(cfg.get("ai") or {}), "brain_enabled": True}
        save_config(s, cfg)
    # Belt-and-suspenders: pin is_us_market_open=True so the gate and the
    # auto-mode logic both treat this as RTH.
    monkeypatch.setattr("backend.bot.calendar.is_us_market_open", lambda: True)

    ex = MagicMock()
    ex.get_account_state.return_value = {
        "buying_power": 1000.0, "portfolio_value": 5000.0, "open_positions": 0, "cash": 5000.0,
    }
    ex.positions.return_value = []
    md = MagicMock()
    snap = MagicMock()
    snap.data = {"price": 100.0}
    md.snapshot.return_value = snap
    brain = MagicMock()
    brain.available = True
    brain.decide_portfolio.return_value = {}

    eng = BotEngine(executor=ex, market_data=md, brain=brain)

    eng.status.running = False        # watch-only / stopped
    eng.run_cycle()
    assert brain.decide_portfolio.call_count == 0   # brain stays armed, doesn't reason

    eng.status.running = True         # bot on
    eng.run_cycle()
    assert brain.decide_portfolio.call_count == 1   # now it reasons


def test_chat_reply_uses_client():
    reply = chatmod.chat_reply("what do I own?", history=[], context="ctx", client=FakeClient("You hold AAPL."))
    assert reply == "You hold AAPL."


def test_chat_reply_no_key(monkeypatch):
    monkeypatch.setattr(chatmod, "anthropic_key", lambda: "")
    reply = chatmod.chat_reply("hello")
    assert "Anthropic API key" in reply
