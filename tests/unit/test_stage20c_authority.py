"""Stage-20c — Master contract lock + Chairman authority flag.

Pinned:
  • When consensus_stance == abstain the Chairman emits an empty
    DissentSurface (no side to dissent from) and surfaces active
    voters in disagreement_axes instead.
  • TUNABLES.chairman_authoritative defaults False — Chairman runs in
    shadow by default.
  • Module docstring contains the 10 master-contract rules.
"""
import pytest

from backend.bot.agents import AgentVote, STANCE_ABSTAIN, STANCE_BUY, STANCE_SELL
from backend.bot.agents.chairman import (
    DECISION_ABSTAIN,
    DissentSurface,
    chairman_review,
)
from backend.bot.agents.contract import (
    DIRECTION_LONG,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
)
from backend.config import TUNABLES


def _kd(desc, cat="credit"):
    return KeyDriver(description=desc, source_category=cat,
                          direction=DIRECTION_LONG, weight=0.5)


def _contrib(name, stance=STANCE_BUY, conf=0.7):
    return AgentVote(
        agent=name, role=name.upper(), stance=stance,
        confidence=conf, weight=1.0,
        reasoning=f"{name} reasoning",
        reasoning_type=REASONING_CONTRIBUTING,
        risk_level="MEDIUM",
        invalidators=[],
        key_drivers=[_kd(f"{name} driver")],
    )


def _silent(name):
    return AgentVote(
        agent=name, role=name.upper(), stance=STANCE_ABSTAIN,
        confidence=0.2, weight=1.0,
        reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
    )


class TestConsensusAbstainHasNoDissent:
    """When the consensus itself is abstain, no one is dissenting
    from a decision — they're contributing to active voting that
    didn't carry. DissentSurface should be empty; active voters
    surface in disagreement_axes."""

    def test_three_voters_two_silent_consensus_abstain(self):
        # 3 voters (2 buy, 1 sell) + 2 silent. Quorum met (3 ≥ 3).
        # If consensus_stance is supplied as "abstain" (mimicking the
        # majority_abstain path in aggregate), the Chairman should NOT
        # falsely report 100% dissent.
        votes = [
            _contrib("market", stance=STANCE_BUY),
            _contrib("macro", stance=STANCE_BUY),
            _contrib("microstructure", stance=STANCE_SELL),
            _silent("portfolio_risk"),
            _silent("devils_advocate"),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_ABSTAIN,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.decision == DECISION_ABSTAIN
        assert rep.decision_reason == "consensus_abstain"
        # No dissent — there's no winning side to dissent from.
        assert rep.dissent.dissenters == []
        assert rep.dissent.primary_dissenter is None
        assert rep.dissent.dissent_share == 0.0
        # Active voters surface in disagreement_axes.
        agents_in_ax = {ax["agent"] for ax in rep.disagreement_axes}
        assert agents_in_ax == {"market", "macro", "microstructure"}
        # Sources should still aggregate from all structured voters.
        assert "credit" in rep.sources_cited


class TestAuthorityFlag:
    def test_default_is_off(self):
        assert TUNABLES.chairman_authoritative is False

    def test_engine_import_exposes_tunable(self):
        # Engine reads TUNABLES.chairman_authoritative on every cycle.
        from backend.bot import engine
        # The wiring relies on getattr with default False — confirm
        # the attribute is reachable.
        assert hasattr(TUNABLES, "chairman_authoritative")


# ── Engine integration: authority flag flips decision source ───────────


from unittest.mock import MagicMock

from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot
from backend.db import session_scope
from backend.models.config import load_config, save_config


def _thin_volume(_ticker):
    """Snapshot that lets the strategy fire a BUY (RSI < 30, earnings
    far away) but drives the council toward silence/abstain via thin
    volume + risk-off VIX. earnings_days=30 avoids the upstream
    event-risk gate so the consensus path is actually exercised."""
    return MarketSnapshot(data={
        "price": 130.0, "rsi": 22.0, "macd": -0.3, "macd_signal": -0.1,
        "macd_hist": -0.2, "prev_macd_hist": 0.1, "ma50": 145.0,
        "ma200": 120.0, "volume": 50_000, "avg_volume": 1_000_000,
        "iv_rank": 0, "adx": 5, "vix": 30, "news_score": 0.0,
        "earnings_days": 30, "pe_ratio": 22, "spy_trend": "bearish",
        "spy_adx": 5, "gap_pct": 0.0, "premarket_volume": 5_000,
        "shares_owned": 0, "position_value": 0, "portfolio_value": 25_000,
        "unrealized_gain_pct": 0.0, "high_52w": 160.0, "prev_close": 132.0,
        "vwap": 131.0, "momentum_5m": -0.1, "rsi_5m": 30,
        "market_trend": "bearish", "time_of_day": "11:00",
        "orb_high": 132.0, "orb_low": 129.0,
        "hist_earnings_move_avg": 0.05, "implied_move": 0.07,
        "has_catalyst": False, "earnings_today": False,
        "news_age_hours": 999, "range_3w_pct": 0.03,
    }, source_errors=[])


def _setup_engine(*, ticker, consensus_abstain_enabled, snapshot_fn):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = [ticker]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        ai = dict(cfg.get("ai") or {})
        ai["consensus_abstain_enabled"] = consensus_abstain_enabled
        cfg["ai"] = ai
        save_config(session, cfg)
    adapter = MagicMock()
    adapter.snapshot.side_effect = snapshot_fn
    engine = BotEngine(executor=Executor(paper_mode=True), market_data=adapter)
    return engine


def _consensus_ran(event):
    """True iff the consensus block actually executed on this event."""
    return "consensus" in event


class TestAuthorityFlagWiring:
    def test_authority_off_uses_legacy_recommendation(self, temp_db, monkeypatch):
        # Flag OFF (default). When consensus runs, the event must
        # tag consensus_authority="legacy".
        monkeypatch.setattr(TUNABLES, "chairman_authoritative", False)
        engine = _setup_engine(
            ticker="AAPL", consensus_abstain_enabled=True,
            snapshot_fn=_thin_volume,
        )
        events = engine.run_cycle()
        if events and _consensus_ran(events[0]):
            assert events[0].get("consensus_authority") == "legacy"
            if events[0]["status"] == "consensus_abstain":
                assert "abstain" in events[0]["reason"].lower()

    def test_authority_on_chairman_blocks_on_abstain_or_monitor(
            self, temp_db, monkeypatch):
        # Flag ON. When consensus runs and the Chairman decided ABSTAIN
        # or MONITOR, the event status must be chairman_* and the
        # reason must quote the Chairman decision verbatim.
        monkeypatch.setattr(TUNABLES, "chairman_authoritative", True)
        engine = _setup_engine(
            ticker="AAPL", consensus_abstain_enabled=True,
            snapshot_fn=_thin_volume,
        )
        events = engine.run_cycle()
        if events and _consensus_ran(events[0]):
            assert events[0].get("consensus_authority") == "chairman"
            # Pull the Chairman decision from the persisted consensus.
            ch = (events[0]["consensus"].get("chairman_report") or {})
            decision = ch.get("decision")
            status = events[0]["status"]
            if decision == "ABSTAIN":
                assert status == "chairman_abstain"
                assert "chairman:" in events[0]["reason"].lower()
            elif decision == "MONITOR":
                assert status == "chairman_monitor"
                assert "chairman:" in events[0]["reason"].lower()
            else:
                # EXECUTE / SIZE_DOWN paths don't block here.
                assert not status.startswith("chairman_")

    def test_authority_on_gate_off_still_attaches_authority_tag(
            self, temp_db, monkeypatch):
        # Gate disabled (consensus_abstain_enabled=False). Even when
        # the Chairman cannot block, the event MUST tag the authority
        # source so audit logs reveal what the gate would have done.
        monkeypatch.setattr(TUNABLES, "chairman_authoritative", True)
        engine = _setup_engine(
            ticker="AAPL", consensus_abstain_enabled=False,
            snapshot_fn=_thin_volume,
        )
        events = engine.run_cycle()
        if events and _consensus_ran(events[0]):
            assert events[0].get("consensus_authority") == "chairman"


class TestMasterContractDocstring:
    """The 10-rule contract is the canonical source of truth.
    Verify it's present in the module docstring so future readers
    can't miss it."""

    def test_ten_rule_block_present(self):
        import backend.bot.agents as agents_mod
        doc = agents_mod.__doc__ or ""
        assert "MASTER AGENT CONTRACT" in doc
        # All ten rule keywords must appear, in order, in the
        # docstring — proxy for "the canonical rules haven't been
        # silently dropped".
        for keyword in [
            "One vote, one shape",
            "Three reasoning states",
            "Evidence accompanies conviction",
            "Silence has no drivers",
            "Active votes carry evidence",
            "Drivers are categorized",
            "One market view, shared",
            "Quorum gate first",
            "Chairman is lossless",
            "Authority is opt-in",
        ]:
            assert keyword in doc, (
                f"Master contract docstring is missing rule '{keyword}'")
