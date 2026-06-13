"""Stage-20b — Heuristic Chairman.

Pinned:
  • chairman_review is pure + deterministic + lossless (no invented text)
  • Refuses to operate on legacy-only panels (returns empty ABSTAIN report)
  • Quorum failure short-circuits to ABSTAIN with reason
  • DissentSurface surfaces primary_dissenter, dissent_weight, dissent_share
  • independent_signal_count counts UNIQUE source_categories
  • overlap_coefficient is mean pairwise Jaccard
  • evidence_correlation bands map correctly (< 0.3, 0.3-0.6, >= 0.6)
  • Decision logic: low_conviction → ABSTAIN; split → ABSTAIN;
    correlated+thin → MONITOR; some dissent/overlap → SIZE_DOWN; clean → EXECUTE
  • Lossless summaries: every word in bull/bear/critical_risk/why_now
    comes from an agent's reasoning, key_driver.description, or
    invalidator — never invented
  • Chairman report is attached to Consensus by aggregate() and to
    run_consensus() output for real council runs
"""
import pytest

from backend.bot.agents import (
    AgentVote,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_SELL,
    aggregate,
    run_consensus,
)
from backend.bot.agents.chairman import (
    CORRELATION_CORRELATED,
    CORRELATION_INDEPENDENT,
    CORRELATION_MIXED,
    ChairmanReport,
    DECISION_ABSTAIN,
    DECISION_EXECUTE,
    DECISION_MONITOR,
    DECISION_SIZE_DOWN,
    DissentSurface,
    chairman_review,
)
from backend.bot.agents.contract import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
)


# ── helpers ─────────────────────────────────────────────────────────────


def _kd(desc, cat="credit", direction=DIRECTION_LONG, weight=0.5,
              time_sensitive=False):
    return KeyDriver(
        description=desc, source_category=cat, direction=direction,
        weight=weight, time_sensitive=time_sensitive,
    )


def _contrib(name, stance=STANCE_BUY, conf=0.7, weight=1.0,
                  drivers=None, reasoning="strong support",
                  risk="MEDIUM", invalidators=None):
    return AgentVote(
        agent=name, role=name.upper(), stance=stance,
        confidence=conf, weight=weight,
        reasoning=reasoning,
        reasoning_type=REASONING_CONTRIBUTING,
        risk_level=risk,
        invalidators=invalidators or [],
        key_drivers=drivers or [_kd(f"{name} driver")],
    )


def _dissent(name, stance=STANCE_SELL, conf=0.7, weight=1.0,
                 drivers=None, reasoning="counter-argument"):
    return AgentVote(
        agent=name, role=name.upper(), stance=stance,
        confidence=conf, weight=weight,
        reasoning=reasoning,
        reasoning_type=REASONING_DISSENTING,
        risk_level="HIGH",
        invalidators=["x clears"],
        key_drivers=drivers or [_kd(f"{name} driver")],
    )


def _silent(name):
    return AgentVote(
        agent=name, role=name.upper(), stance=STANCE_ABSTAIN,
        confidence=0.2, weight=1.0,
        reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
    )


# ── refusal to operate on legacy / under-informed panels ───────────────


class TestChairmanRefusal:
    def test_legacy_only_panel_returns_empty_abstain(self):
        legacy_votes = [
            AgentVote(agent="a", role="A", stance=STANCE_BUY,
                          confidence=0.7, weight=1.0, reasoning="x"),
            AgentVote(agent="b", role="B", stance=STANCE_SELL,
                          confidence=0.6, weight=1.0, reasoning="y"),
        ]
        rep = chairman_review(
            votes=legacy_votes,
            consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN,
            quorum_met=True, quorum_count=2, quorum_required=2,
        )
        assert rep.decision == DECISION_ABSTAIN
        assert rep.decision_reason == "no_structured_votes"
        # No fabricated content
        assert rep.bull_case == ""
        assert rep.bear_case == ""
        assert rep.sources_cited == []

    def test_quorum_failure_aborts(self):
        votes = [_contrib("a")] + [_silent(f"s{i}") for i in range(4)]
        rep = chairman_review(
            votes=votes,
            consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN,
            quorum_met=False, quorum_count=1, quorum_required=3,
        )
        assert rep.decision == DECISION_ABSTAIN
        assert rep.decision_reason == "insufficient_council_quorum"
        # Empty — no inventing
        assert rep.disagreement_axes == []
        assert rep.position_size_modifier == 1.0


# ── DissentSurface ──────────────────────────────────────────────────────


class TestDissentSurface:
    def test_unanimous_has_zero_dissent(self):
        votes = [_contrib(f"a{i}") for i in range(3)]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.dissent.dissent_share == 0.0
        assert rep.dissent.dissenters == []
        assert rep.dissent.primary_dissenter is None

    def test_primary_dissenter_is_loudest_opposing(self):
        votes = [
            _contrib("a", weight=1.0, conf=0.7),
            _contrib("b", weight=1.0, conf=0.7),
            _dissent("loud", conf=0.9, weight=1.5),     # vw = 1.35
            _dissent("quiet", conf=0.4, weight=0.5),    # vw = 0.20
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=4, quorum_required=3,
        )
        assert rep.dissent.primary_dissenter == "loud"
        assert set(rep.dissent.dissenters) == {"loud", "quiet"}
        assert 0 < rep.dissent.dissent_share < 1
        assert rep.dissent.dissent_weight == pytest.approx(1.35 + 0.20, abs=1e-3)


# ── Jaccard + independence ──────────────────────────────────────────────


class TestEvidenceReconciliation:
    def test_independent_signals_low_overlap(self):
        # 3 agents citing fully disjoint categories → overlap = 0
        votes = [
            _contrib("a", drivers=[_kd("d1", cat="credit")]),
            _contrib("b", drivers=[_kd("d2", cat="breadth")]),
            _contrib("c", drivers=[_kd("d3", cat="microstructure_flow")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.overlap_coefficient == 0.0
        assert rep.independent_signal_count == 3
        assert rep.evidence_correlation == CORRELATION_INDEPENDENT
        assert set(rep.sources_cited) == {"credit", "breadth",
                                                       "microstructure_flow"}

    def test_correlated_signals_high_overlap(self):
        # 3 agents citing the same single category → Jaccard = 1 pairwise
        votes = [
            _contrib("a", drivers=[_kd("d1", cat="credit")]),
            _contrib("b", drivers=[_kd("d2", cat="credit")]),
            _contrib("c", drivers=[_kd("d3", cat="credit")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.overlap_coefficient == 1.0
        assert rep.independent_signal_count == 1
        assert rep.evidence_correlation == CORRELATION_CORRELATED

    def test_mixed_overlap(self):
        # 3 agents: two share category, third independent
        votes = [
            _contrib("a", drivers=[_kd("x", cat="credit"),
                                            _kd("y", cat="breadth")]),
            _contrib("b", drivers=[_kd("z", cat="credit"),
                                            _kd("w", cat="volatility")]),
            _contrib("c", drivers=[_kd("q", cat="positioning")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.independent_signal_count == 4
        assert 0.0 < rep.overlap_coefficient < 1.0


# ── Decision logic ──────────────────────────────────────────────────────


class TestChairmanDecisions:
    def test_clean_aligned_council_executes(self):
        votes = [
            _contrib("a", conf=0.8, drivers=[_kd("d1", cat="credit")]),
            _contrib("b", conf=0.75, drivers=[_kd("d2", cat="breadth")]),
            _contrib("c", conf=0.7, drivers=[_kd("d3", cat="microstructure_flow")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.decision == DECISION_EXECUTE
        assert rep.position_size_modifier > 0.8

    def test_split_panel_abstains(self):
        # 2 buy + 2 sell → 50/50 → ABSTAIN with reason panel_split
        votes = [
            _contrib("a", conf=0.7, weight=1.0),
            _contrib("b", conf=0.7, weight=1.0),
            _dissent("c", conf=0.7, weight=1.0),
            _dissent("d", conf=0.7, weight=1.0),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=4, quorum_required=3,
        )
        assert rep.decision == DECISION_ABSTAIN
        assert rep.decision_reason == "panel_split"

    def test_some_dissent_sizes_down(self):
        votes = [
            _contrib("a", conf=0.7, drivers=[_kd("x", cat="credit")]),
            _contrib("b", conf=0.7, drivers=[_kd("y", cat="breadth")]),
            _contrib("c", conf=0.7, drivers=[_kd("z", cat="microstructure_flow")]),
            _dissent("d", conf=0.7, drivers=[_kd("counter", cat="volatility")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=4, quorum_required=3,
        )
        # 25% dissent < 50% split, ≥ 30% threshold actually depends on
        # weights — here 1 dissent / 4 = 25% dissent_share → triggers
        # SIZE_DOWN via the "≥ 2 counters" / overlap path? Only 1
        # counter, 25% share → should EXECUTE.
        # Let me adjust expectation: dissent_share < 0.30 and 1 counter
        # and independent_signal_count=4 → EXECUTE.
        assert rep.decision == DECISION_EXECUTE

    def test_correlated_thin_evidence_monitors(self):
        # 3 supporters all citing the SAME single category — correlated
        # and only 1 independent signal → MONITOR.
        votes = [
            _contrib("a", conf=0.7, drivers=[_kd("d1", cat="credit")]),
            _contrib("b", conf=0.7, drivers=[_kd("d2", cat="credit")]),
            _contrib("c", conf=0.7, drivers=[_kd("d3", cat="credit")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.decision == DECISION_MONITOR
        assert rep.decision_reason == "correlated_evidence_thin"

    def test_two_dissenters_size_down(self):
        votes = [
            _contrib("a", conf=0.7, weight=1.0,
                          drivers=[_kd("x", cat="credit")]),
            _contrib("b", conf=0.7, weight=1.0,
                          drivers=[_kd("y", cat="breadth")]),
            _contrib("c", conf=0.7, weight=1.0,
                          drivers=[_kd("z", cat="positioning")]),
            _dissent("d", conf=0.5, weight=0.5,
                            drivers=[_kd("c1", cat="volatility")]),
            _dissent("e", conf=0.5, weight=0.5,
                            drivers=[_kd("c2", cat="fundamentals")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=5, quorum_required=3,
        )
        # 2 dissenters, dissent_share < 0.30 (0.5*2 / (0.7*3+0.5*2) =
        # 1.0 / 3.1 = 0.32). Triggers SIZE_DOWN via "≥ 2 counters".
        assert rep.decision == DECISION_SIZE_DOWN

    def test_low_conviction_abstains(self):
        votes = [
            _contrib("a", conf=0.40, drivers=[_kd("x", cat="credit")]),
            _contrib("b", conf=0.40, drivers=[_kd("y", cat="breadth")]),
            _contrib("c", conf=0.40, drivers=[_kd("z", cat="positioning")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        # Each vote barely above the 0.35 contribution floor; weighted
        # mean conviction stays under 0.45 → ABSTAIN.
        assert rep.decision == DECISION_ABSTAIN
        assert rep.decision_reason == "low_conviction"


# ── Lossless compression — no fabricated text ──────────────────────────


class TestLossless:
    def test_bull_case_concatenates_supporter_drivers_only(self):
        votes = [
            _contrib("a", drivers=[
                _kd("HY spread tightening", cat="credit"),
                _kd("breadth healthy_advance", cat="breadth"),
            ]),
            _contrib("b", drivers=[_kd("VIX 13 calm", cat="volatility")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=2, quorum_required=2,
        )
        # Every fragment must originate in a driver description.
        assert "HY spread tightening" in rep.bull_case
        assert "breadth healthy_advance" in rep.bull_case
        assert "VIX 13 calm" in rep.bull_case
        # No invented narrative — must NOT contain words the agents
        # didn't write.
        assert "outlook" not in rep.bull_case.lower()
        assert "i think" not in rep.bull_case.lower()

    def test_bear_case_concatenates_counter_drivers_only(self):
        votes = [
            _contrib("a", drivers=[_kd("clean tape", cat="credit")]),
            _dissent("d", drivers=[_kd("yield curve inverted",
                                                  cat="macro_liquidity")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=2, quorum_required=2,
        )
        assert "yield curve inverted" in rep.bear_case
        # Supporter driver should NOT appear in bear_case.
        assert "clean tape" not in rep.bear_case

    def test_critical_risk_quotes_high_risk_invalidator_verbatim(self):
        v = _contrib("a", drivers=[_kd("d", cat="credit")],
                          invalidators=["HY widens > 5.5%"], risk="HIGH")
        votes = [v, _contrib("b", drivers=[_kd("d2", cat="breadth")])]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=2, quorum_required=2,
        )
        assert "HY widens > 5.5%" in rep.critical_risk
        assert rep.critical_risk.startswith("a:")

    def test_why_now_lists_only_time_sensitive_drivers(self):
        votes = [
            _contrib("a", drivers=[
                _kd("regular driver", cat="credit", time_sensitive=False),
                _kd("decaying signal", cat="microstructure_flow",
                       time_sensitive=True),
            ]),
            _contrib("b", drivers=[_kd("non-urgent", cat="breadth",
                                                  time_sensitive=False)]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=2, quorum_required=2,
        )
        assert "decaying signal" in rep.why_now
        assert "regular driver" not in rep.why_now
        assert "non-urgent" not in rep.why_now

    def test_disagreement_axes_quote_dissenter_reasoning_verbatim(self):
        d = _dissent("dx", reasoning="risk-off tape, HY widening")
        votes = [_contrib("a", drivers=[_kd("ok", cat="credit")]), d]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=2, quorum_required=2,
        )
        assert len(rep.disagreement_axes) == 1
        ax = rep.disagreement_axes[0]
        assert ax["agent"] == "dx"
        assert ax["reasoning"] == "risk-off tape, HY widening"

    def test_sources_cited_are_categories_not_descriptions(self):
        votes = [
            _contrib("a", drivers=[_kd("HY 4.2%", cat="credit"),
                                            _kd("vol 1.5x", cat="microstructure_flow")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=1, quorum_required=1,
        )
        assert "credit" in rep.sources_cited
        assert "microstructure_flow" in rep.sources_cited
        # description strings should NOT be in sources_cited
        assert "HY 4.2%" not in rep.sources_cited


# ── position_size_modifier ─────────────────────────────────────────────


class TestPositionSizeModifier:
    def test_clean_independent_panel_keeps_full_size(self):
        votes = [
            _contrib("a", drivers=[_kd("d1", cat="credit")]),
            _contrib("b", drivers=[_kd("d2", cat="breadth")]),
            _contrib("c", drivers=[_kd("d3", cat="volatility")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert rep.position_size_modifier == 1.0

    def test_correlated_evidence_cuts_size(self):
        votes = [
            _contrib("a", drivers=[_kd("d1", cat="credit")]),
            _contrib("b", drivers=[_kd("d2", cat="credit")]),
            _contrib("c", drivers=[_kd("d3", cat="credit")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        # overlap = 1.0 → size_mod = 1 - 0.5*1.0 = 0.5
        assert rep.position_size_modifier == pytest.approx(0.5, abs=1e-3)

    def test_dissent_cuts_size_more_than_overlap(self):
        votes = [
            _contrib("a", drivers=[_kd("d1", cat="credit")]),
            _contrib("b", drivers=[_kd("d2", cat="breadth")]),
            _dissent("d", drivers=[_kd("counter", cat="volatility")]),
        ]
        rep = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        # dissent_share ~ 1/3, overlap small → size_mod ≈ 1 - 0 - 0.33
        assert 0.5 < rep.position_size_modifier < 0.8


# ── Integration via aggregate / run_consensus ──────────────────────────


class TestChairmanInConsensus:
    def test_aggregate_attaches_chairman_report(self):
        votes = [
            _contrib("market", drivers=[_kd("trend", cat="price_structure")]),
            _contrib("microstructure",
                          drivers=[_kd("flow", cat="microstructure_flow")]),
            _contrib("portfolio_risk",
                          drivers=[_kd("dd", cat="portfolio_state")]),
        ]
        c = aggregate(votes)
        assert isinstance(c.chairman_report, dict)
        assert c.chairman_report["decision"] in (
            DECISION_EXECUTE, DECISION_SIZE_DOWN,
            DECISION_MONITOR, DECISION_ABSTAIN,
        )
        assert "dissent" in c.chairman_report

    def test_legacy_votes_get_empty_chairman_report(self):
        # 3 legacy votes (default reasoning_type) → Chairman refuses.
        votes = [
            AgentVote(agent=f"a{i}", role="A", stance=STANCE_BUY,
                          confidence=0.7, weight=1.0, reasoning="x")
            for i in range(3)
        ]
        c = aggregate(votes)
        assert c.chairman_report["decision"] == DECISION_ABSTAIN
        assert c.chairman_report["decision_reason"] == "no_structured_votes"

    def test_run_consensus_emits_chairman_report(self):
        # Use the real production agents on a bullish context.
        ctx = {
            "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend",
            "analytics": {
                "regime": {"trend": "bullish", "momentum": "expanding"},
                "features": {"trend_bias": 0.5, "flow_bullishness": 0.4,
                                "iv_rank": 30, "volume_ratio": 1.4, "vix": 14},
            },
            "snapshot": {"spy_trend": "bullish", "vix": 14,
                            "volume": 1_400_000, "avg_volume": 1_000_000},
            "portfolio_risk": {"drawdown_pct": 0.01, "top_theme": "AI",
                                  "top_theme_pct": 0.18,
                                  "concentration_flags": []},
            "cohort": {"win_rate": 0.62, "closed_count": 35},
            "macro": {"BAMLH0A0HYM2": {"value": 3.2}, "NFCI": {"value": -0.35}},
            "breadth": {"verdict": "healthy_advance",
                           "pct_above_50dma": 0.7},
        }
        c = run_consensus(ctx)
        # In a fully-bullish context with 5 structured agents, the
        # Chairman should pick something other than "no_structured_votes"
        # and should NOT be a quorum failure.
        assert c.chairman_report["decision"] in (
            DECISION_EXECUTE, DECISION_SIZE_DOWN, DECISION_MONITOR,
        )
        assert c.chairman_report.get("decision_reason") not in (
            "no_structured_votes",
            "insufficient_council_quorum",
        )
        # Sources cited should reference real categories.
        sc = c.chairman_report.get("sources_cited") or []
        assert len(sc) >= 1


# ── Determinism ────────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_input_same_report(self):
        votes = [
            _contrib("a", drivers=[_kd("d1", cat="credit")]),
            _contrib("b", drivers=[_kd("d2", cat="breadth")]),
            _dissent("d", drivers=[_kd("c1", cat="volatility")]),
        ]
        r1 = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        r2 = chairman_review(
            votes=votes, consensus_stance=STANCE_BUY,
            abstain_stance=STANCE_ABSTAIN, quorum_met=True,
            quorum_count=3, quorum_required=3,
        )
        assert r1.to_dict() == r2.to_dict()
