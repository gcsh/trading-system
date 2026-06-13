"""Stage-14 Dynamic vote weights — scorecard-derived multipliers feed
back into the consensus engine.

Pinned:
  • aggregate() with no dynamic_weights → identical to legacy behavior
  • dynamic_weights of all 1.0 → no change
  • Boost on a single agent → that agent's contribution grows
  • Penalty on a single agent → that agent's contribution shrinks
  • Hostile weight can flip the consensus stance
  • run_consensus(use_dynamic_weights=False) preserves the legacy result
  • run_consensus(use_dynamic_weights=True) on a cold-start system also
    preserves the legacy result (vote_weights returns all 1.0)
"""
from backend.bot.agents import (
    AgentVote,
    Consensus,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_SELL,
    _apply_dynamic_weights,
    aggregate,
    run_consensus,
)


def _bullish_votes():
    return [
        AgentVote("market", "M", STANCE_BUY, 0.8, weight=1.0, reasoning=""),
        AgentVote("flow", "F", STANCE_BUY, 0.7, weight=1.0, reasoning=""),
        AgentVote("options", "O", STANCE_SELL, 0.6, weight=1.0, reasoning=""),
    ]


class TestApplyWeights:
    def test_no_weights_is_passthrough(self):
        v = _bullish_votes()
        out = _apply_dynamic_weights(v, None)
        # Should be the same list object back (efficient default).
        assert out is v

    def test_all_ones_is_noop(self):
        v = _bullish_votes()
        out = _apply_dynamic_weights(v, {"market": 1.0, "flow": 1.0, "options": 1.0})
        # Effective weights unchanged
        assert all(a.weight == 1.0 for a in out)

    def test_boost_grows_weight(self):
        v = _bullish_votes()
        out = _apply_dynamic_weights(v, {"market": 1.5})
        by = {a.agent: a for a in out}
        assert by["market"].weight == 1.5
        assert by["flow"].weight == 1.0      # untouched

    def test_penalty_shrinks_weight(self):
        v = _bullish_votes()
        out = _apply_dynamic_weights(v, {"options": 0.5})
        by = {a.agent: a for a in out}
        assert by["options"].weight == 0.5

    def test_does_not_mutate_caller_votes(self):
        v = _bullish_votes()
        original_weight = v[0].weight
        _apply_dynamic_weights(v, {"market": 1.5})
        assert v[0].weight == original_weight


class TestAggregateWithWeights:
    def test_baseline_buy_wins(self):
        c = aggregate(_bullish_votes())
        assert c.stance == STANCE_BUY

    def test_weights_flip_consensus(self):
        # Penalize both buyers, boost the seller → SELL stance dominates
        c = aggregate(_bullish_votes(),
                        dynamic_weights={"market": 0.1, "flow": 0.1,
                                            "options": 3.0})
        assert c.stance == STANCE_SELL

    def test_weights_boost_size_of_winning_side(self):
        base = aggregate(_bullish_votes())
        boosted = aggregate(_bullish_votes(),
                              dynamic_weights={"market": 1.5, "flow": 1.5})
        # Both should still be BUY but boosted has more probability mass on long
        assert boosted.probs["long"] >= base.probs["long"]


class TestRunConsensusFlag:
    def test_legacy_path_default_off(self):
        # Default use_dynamic_weights=False → identical to legacy behavior
        c = run_consensus({"ticker": "X", "action": "BUY_STOCK"})
        assert isinstance(c, Consensus)

    def test_cold_start_with_flag_on_is_safe(self, temp_db):
        # No DB data → vote_weights returns all 1.0 → result == legacy
        c_off = run_consensus({"ticker": "X", "action": "BUY_STOCK"},
                                use_dynamic_weights=False)
        c_on = run_consensus({"ticker": "X", "action": "BUY_STOCK"},
                               use_dynamic_weights=True)
        # Cold start: no scorecard data → same recommendation + same probs
        assert c_on.recommendation == c_off.recommendation
        assert c_on.probs == c_off.probs
