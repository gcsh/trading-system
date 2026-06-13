"""Stage-20a — Master Agent Contract + Market Internals Score.

Pinned:
  • The 4 AgentVote contract invariants fire on structured votes only
  • Legacy votes (default reasoning_type) bypass the contract entirely
  • KeyDriver validates source_category, direction, weight
  • MarketInternalsScore is deterministic and returns expected verdicts
  • run_consensus threads MarketInternalsScore into context + Consensus
  • Quorum gate forces abstain when < agent_quorum_min agents non-silent
  • Silent agents (reasoning_type=insufficient_signal) don't count for
    quorum, supporters, or dissenters — they surface in silent_agents
  • Production agents emit structured key_drivers with valid categories
"""
import pytest

from backend.bot.agents import (
    AGENT_FUNCS,
    AgentVote,
    Consensus,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_HOLD,
    STANCE_SELL,
    aggregate,
    run_consensus,
)
from backend.bot.agents.contract import (
    ContractViolation,
    DIRECTION_ABSTAIN,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KEY_DRIVER_DIRECTIONS,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
    REASONING_LEGACY,
    SOURCE_CATEGORIES,
    enforce_vote_contract,
)
from backend.bot.agents.market_internals import (
    MARKET_INTERNAL_CATEGORIES,
    MarketInternalsScore,
    VERDICT_MIXED,
    VERDICT_RISK_OFF,
    VERDICT_RISK_ON,
    VERDICT_UNKNOWN,
    compute_market_internals,
)
from backend.config import TUNABLES


# ── KeyDriver invariants ────────────────────────────────────────────────


class TestKeyDriver:
    def test_valid_construction(self):
        d = KeyDriver(
            description="HY spread 4.2% widening",
            source_category="credit",
            direction=DIRECTION_SHORT,
            weight=0.7,
            time_sensitive=True,
        )
        assert d.weight == 0.7
        assert d.source_category == "credit"

    def test_rejects_invalid_category(self):
        with pytest.raises(ContractViolation):
            KeyDriver(
                description="x",
                source_category="not_a_category",
                direction=DIRECTION_LONG,
            )

    def test_rejects_invalid_direction(self):
        with pytest.raises(ContractViolation):
            KeyDriver(description="x", source_category="credit",
                          direction="up")

    def test_rejects_weight_outside_0_to_1(self):
        with pytest.raises(ContractViolation):
            KeyDriver(description="x", source_category="credit",
                          direction=DIRECTION_LONG, weight=1.5)
        with pytest.raises(ContractViolation):
            KeyDriver(description="x", source_category="credit",
                          direction=DIRECTION_LONG, weight=-0.1)

    def test_every_market_internal_category_is_in_source_categories(self):
        for c in MARKET_INTERNAL_CATEGORIES:
            assert c in SOURCE_CATEGORIES

    def test_portfolio_state_is_excluded_from_market_internals(self):
        # portfolio_state is a SOURCE_CATEGORY (used by portfolio_risk
        # agent drivers) but is NOT a market-internal category.
        assert "portfolio_state" in SOURCE_CATEGORIES
        assert "portfolio_state" not in MARKET_INTERNAL_CATEGORIES


# ── AgentVote contract invariants ───────────────────────────────────────


class TestAgentVoteContract:
    def _driver(self):
        return KeyDriver(description="x", source_category="credit",
                              direction=DIRECTION_LONG, weight=0.5)

    def test_legacy_default_bypasses_invariants(self):
        # Legacy construction with no key_drivers + high confidence is allowed.
        v = AgentVote(agent="x", role="X", stance=STANCE_BUY,
                          confidence=0.9, weight=1.0, reasoning="ok")
        assert v.reasoning_type == REASONING_LEGACY
        assert v.key_drivers == []

    def test_contributing_requires_at_least_one_key_driver(self):
        with pytest.raises(ContractViolation):
            AgentVote(agent="x", role="X", stance=STANCE_BUY,
                          confidence=0.7, weight=1.0,
                          reasoning="empty",
                          reasoning_type=REASONING_CONTRIBUTING)

    def test_dissenting_requires_at_least_one_key_driver(self):
        with pytest.raises(ContractViolation):
            AgentVote(agent="x", role="X", stance=STANCE_SELL,
                          confidence=0.6, weight=1.0,
                          reasoning="empty",
                          reasoning_type=REASONING_DISSENTING)

    def test_insufficient_signal_must_be_abstain(self):
        with pytest.raises(ContractViolation):
            AgentVote(agent="x", role="X", stance=STANCE_BUY,
                          confidence=0.3, weight=1.0,
                          reasoning_type=REASONING_INSUFFICIENT_SIGNAL)

    def test_insufficient_signal_must_have_empty_key_drivers(self):
        with pytest.raises(ContractViolation):
            AgentVote(agent="x", role="X", stance=STANCE_ABSTAIN,
                          confidence=0.3, weight=1.0,
                          reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
                          key_drivers=[self._driver()])

    def test_insufficient_signal_valid(self):
        v = AgentVote(agent="x", role="X", stance=STANCE_ABSTAIN,
                          confidence=0.2, weight=1.0,
                          reasoning="no data",
                          reasoning_type=REASONING_INSUFFICIENT_SIGNAL)
        assert v.is_silent() is True
        assert v.is_structured() is True

    def test_high_confidence_without_drivers_forbidden(self):
        # Even at exactly the threshold + 0.01, missing drivers is a violation.
        thresh = TUNABLES.min_confidence_for_contribution
        with pytest.raises(ContractViolation):
            enforce_vote_contract(
                reasoning_type=REASONING_CONTRIBUTING,
                stance=STANCE_BUY, confidence=thresh + 0.01,
                key_drivers=[],
                abstain_stance=STANCE_ABSTAIN,
                min_confidence_for_contribution=thresh,
            )

    def test_to_dict_serializes_key_drivers_as_dicts(self):
        v = AgentVote(agent="m", role="M", stance=STANCE_BUY,
                          confidence=0.7, weight=1.0,
                          reasoning="ok",
                          reasoning_type=REASONING_CONTRIBUTING,
                          key_drivers=[self._driver()])
        d = v.to_dict()
        assert isinstance(d["key_drivers"][0], dict)
        assert d["key_drivers"][0]["source_category"] == "credit"
        assert d["reasoning_type"] == REASONING_CONTRIBUTING


# ── MarketInternalsScore ────────────────────────────────────────────────


class TestMarketInternals:
    def test_empty_inputs_yield_unknown(self):
        s = compute_market_internals()
        assert s.verdict == VERDICT_UNKNOWN
        assert s.sources_available == 0
        for c in MARKET_INTERNAL_CATEGORIES:
            assert s.category_score(c) is None

    def test_risk_on_panel(self):
        s = compute_market_internals(
            macro={"BAMLH0A0HYM2": {"value": 2.8, "change_30d_pct": -0.05},
                       "NFCI": {"value": -0.45}},
            breadth={"verdict": "healthy_advance", "pct_above_50dma": 0.72},
            snapshot={"vix": 13.0, "spy_trend": "bullish"},
            features={"trend_bias": 0.5, "flow_bullishness": 0.4,
                          "volume_ratio": 1.6},
        )
        assert s.verdict == VERDICT_RISK_ON
        assert s.composite > 0.30
        assert s.macro_liquidity is not None and s.macro_liquidity > 0
        assert s.credit is not None and s.credit > 0
        assert s.breadth is not None and s.breadth > 0
        assert s.volatility is not None and s.volatility > 0
        assert s.sources_available >= 4

    def test_risk_off_panel(self):
        s = compute_market_internals(
            macro={"BAMLH0A0HYM2": {"value": 6.5, "change_30d_pct": 0.20},
                       "NFCI": {"value": 0.45},
                       "yield_curve_inverted": True},
            breadth={"verdict": "broken", "pct_above_50dma": 0.25},
            snapshot={"vix": 28.0, "spy_trend": "bearish"},
        )
        assert s.verdict == VERDICT_RISK_OFF
        assert s.composite < -0.30
        assert s.credit < 0
        assert s.macro_liquidity < 0
        assert s.volatility < 0

    def test_mixed_panel(self):
        # Loose conditions but bad breadth → mixed.
        s = compute_market_internals(
            macro={"BAMLH0A0HYM2": {"value": 3.0}, "NFCI": {"value": -0.40}},
            breadth={"verdict": "broken", "pct_above_50dma": 0.25},
            snapshot={"vix": 22.0},
        )
        assert s.verdict in (VERDICT_MIXED, VERDICT_RISK_ON, VERDICT_RISK_OFF)
        # At least 3 categories must have fired
        assert s.sources_available >= 3

    def test_determinism(self):
        kwargs = dict(
            macro={"BAMLH0A0HYM2": {"value": 4.2}},
            breadth={"verdict": "mixed", "pct_above_50dma": 0.5},
            snapshot={"vix": 18.0},
        )
        a = compute_market_internals(**kwargs)
        b = compute_market_internals(**kwargs)
        assert a.to_dict() == b.to_dict()

    def test_to_dict_is_json_safe(self):
        import json
        s = compute_market_internals(
            macro={"BAMLH0A0HYM2": {"value": 4.0}},
            snapshot={"vix": 16},
        )
        json.dumps(s.to_dict())          # must not raise


# ── Production agents emit structured contracts ─────────────────────────


def _full_bullish_ctx():
    return {
        "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
        "analytics": {
            "regime": {"trend": "bullish", "volatility": "normal",
                         "gamma": "long_gamma", "momentum": "expanding",
                         "label": "bullish · normal-vol · long gamma"},
            "features": {
                "trend_bias": 0.5, "flow_bullishness": 0.4,
                "premarket_bullish_sweeps": 0.6, "dealer_regime": "long_gamma",
                "hedging_pressure": "normal", "iv_rank": 30,
                "pinning_probability": 0.1, "earnings_days": 30,
                "vix": 14, "news_sentiment": 0.3, "volume_ratio": 1.4,
            },
        },
        "snapshot": {"spy_trend": "bullish", "vix": 14, "volume": 1_400_000,
                       "avg_volume": 1_000_000},
        "cross_asset": {"equities": "risk_on", "volatility": "compressed"},
        "portfolio_risk": {"net_beta": 0.5, "drawdown_pct": 0.01,
                              "top_theme": "AI infra", "top_theme_pct": 0.18,
                              "concentration_flags": []},
        "cohort": {"win_rate": 0.62, "closed_count": 35},
        "macro": {"BAMLH0A0HYM2": {"value": 3.2, "change_30d_pct": -0.04},
                     "NFCI": {"value": -0.35}},
        "breadth": {"verdict": "healthy_advance",
                       "pct_above_50dma": 0.7},
    }


class TestProductionAgentsEmitStructured:
    def test_every_agent_emits_structured_payload(self):
        ctx = _full_bullish_ctx()
        # Compute internals once to mimic run_consensus.
        ctx["market_internals_obj"] = compute_market_internals(
            macro=ctx["macro"], breadth=ctx["breadth"],
            snapshot=ctx["snapshot"], features=ctx["analytics"]["features"],
        )
        for name, role, fn in AGENT_FUNCS:
            v = fn(ctx)
            assert v.is_structured(), f"{name} returned a legacy vote"
            if v.is_silent():
                assert v.key_drivers == [], (
                    f"{name} silent vote has key_drivers")
                assert v.stance == STANCE_ABSTAIN
            else:
                assert len(v.key_drivers) >= 1, (
                    f"{name} contributing/dissenting vote has 0 drivers")
                for kd in v.key_drivers:
                    assert kd.source_category in SOURCE_CATEGORIES
                    assert kd.direction in KEY_DRIVER_DIRECTIONS

    def test_empty_context_makes_agents_silent(self):
        """An empty context should make all agents go silent
        (reasoning_type=insufficient_signal), not abstain with conviction."""
        ctx = {"ticker": "X", "action": "BUY_STOCK"}
        ctx["market_internals_obj"] = compute_market_internals()
        silent_count = 0
        for _, _, fn in AGENT_FUNCS:
            v = fn(ctx)
            if v.is_silent():
                silent_count += 1
        # Most agents should be silent on an empty context.
        assert silent_count >= 3


# ── Quorum + silent surfacing ───────────────────────────────────────────


class TestQuorumAndSilence:
    def _silent(self, name):
        return AgentVote(
            agent=name, role="X", stance=STANCE_ABSTAIN,
            confidence=0.2, weight=1.0,
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
        )

    def _contributing(self, name, stance=STANCE_BUY, conf=0.7):
        return AgentVote(
            agent=name, role="X", stance=stance,
            confidence=conf, weight=1.0,
            reasoning="ok",
            reasoning_type=REASONING_CONTRIBUTING,
            key_drivers=[KeyDriver(description="x",
                                          source_category="credit",
                                          direction=DIRECTION_LONG)],
        )

    def test_quorum_failure_forces_abstain(self):
        # 4 silent + 1 contributing → quorum_min=3 fails (1 non-silent).
        votes = [self._silent(f"a{i}") for i in range(4)]
        votes.append(self._contributing("b"))
        c = aggregate(votes, quorum_min=3)
        assert c.recommendation == "abstain"
        assert c.recommendation_reason == "insufficient_council_quorum"
        assert c.quorum_met is False
        assert c.quorum_count == 1
        assert c.quorum_required == 3
        assert len(c.silent_agents) == 4

    def test_quorum_met_allows_execute(self):
        votes = [self._contributing(f"a{i}") for i in range(3)]
        c = aggregate(votes, quorum_min=3)
        assert c.quorum_met is True
        assert c.quorum_count == 3
        assert c.recommendation in ("execute", "size_down")

    def test_silent_agents_surface_in_consensus(self):
        votes = [
            self._silent("macro"),
            self._silent("portfolio_risk"),
            self._contributing("market"),
            self._contributing("microstructure"),
            self._contributing("devils_advocate"),
        ]
        c = aggregate(votes, quorum_min=3)
        assert set(c.silent_agents) == {"macro", "portfolio_risk"}
        assert c.quorum_met is True


# ── Engine integration via run_consensus ────────────────────────────────


class TestRunConsensusIntegration:
    def test_attaches_market_internals_to_consensus(self):
        ctx = _full_bullish_ctx()
        c = run_consensus(ctx)
        assert isinstance(c, Consensus)
        assert "verdict" in c.market_internals
        assert c.market_internals["sources_available"] >= 3
        assert c.market_internals["verdict"] in (
            VERDICT_RISK_ON, VERDICT_MIXED, VERDICT_RISK_OFF, VERDICT_UNKNOWN
        )

    def test_quorum_diagnostic_present_on_consensus(self):
        c = run_consensus(_full_bullish_ctx())
        assert c.quorum_required >= 1
        assert c.quorum_count >= 0
        assert isinstance(c.quorum_met, bool)

    def test_empty_context_silent_majority_abstains_for_quorum(self):
        ctx = {"ticker": "X", "action": "BUY_STOCK"}
        c = run_consensus(ctx)
        # With an empty context, most agents go silent; quorum likely fails
        # OR majority abstain trips first — both end at abstain.
        assert c.recommendation == "abstain"
        # If quorum failed, the reason is explicit.
        if not c.quorum_met:
            assert c.recommendation_reason == "insufficient_council_quorum"

    def test_bullish_context_executes_and_carries_internals(self):
        c = run_consensus(_full_bullish_ctx())
        assert c.stance == STANCE_BUY
        assert c.recommendation in ("execute", "size_down")
        assert c.quorum_met is True
        assert c.market_internals.get("verdict") in (
            VERDICT_RISK_ON, VERDICT_MIXED
        )
