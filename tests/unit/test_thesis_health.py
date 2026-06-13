"""MITS-5 — thesis-health agent + winner-profile builder.

Locks:
  * Synthetic profile + position matching all traits → high health,
    HOLD vote (not EXIT).
  * Synthetic position with degraded VWAP / flag low → low health,
    EXIT vote.
  * Thin-corpus profile → agent abstains (insufficient_signal).
  * AGENT_FUNCS contains 8 agents including `thesis_health` (Phase 14.C
    added `simulator` as the 8th).
  * The agent abstains on new-trade evaluations (no open position).
"""
from datetime import datetime, timedelta

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "thesis_health_test.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import backend.db as _dbmod
    _dbmod._engine = None
    _dbmod._SessionLocal = None
    from backend.db import init_db
    init_db(str(db_path))
    # Drop profile cache so each test starts fresh.
    from backend.bot.thesis import profile_builder as _pb
    _pb.clear_profile_cache()
    yield
    _dbmod._engine = None
    _dbmod._SessionLocal = None


# ── helpers ────────────────────────────────────────────────────────────


def _make_trustworthy_profile():
    from backend.bot.thesis.winner_profile import (
        TRAIT_HELD_FLAG_LOW,
        TRAIT_HELD_VWAP,
        WinnerProfile,
    )
    return WinnerProfile(
        pattern="bull_flag",
        regime="trending_up",
        sample_size=50,
        avg_minutes_to_peak=120.0,
        avg_max_drawdown_during_hold=-0.05,
        common_traits=[TRAIT_HELD_FLAG_LOW, TRAIT_HELD_VWAP],
        trait_frequencies={TRAIT_HELD_VWAP: 0.80, TRAIT_HELD_FLAG_LOW: 0.70},
        confidence=0.70,
    )


def _make_thin_profile():
    from backend.bot.thesis.winner_profile import WinnerProfile
    return WinnerProfile(
        pattern="bull_flag",
        regime="trending_up",
        sample_size=3,
        avg_minutes_to_peak=0.0,
        avg_max_drawdown_during_hold=0.0,
        common_traits=[],
        trait_frequencies={},
        confidence=0.10,
    )


# ── health_calculator unit tests ───────────────────────────────────────


class TestCalculateHealth:
    def test_all_traits_intact_high_score(self):
        from backend.bot.thesis import calculate_health
        profile = _make_trustworthy_profile()
        pos = {
            "current_price": 110.0,
            "vwap": 105.0,
            "flag_low": 100.0,
        }
        health = calculate_health(pos, None, profile)
        assert not health.abstain
        assert health.score >= 70.0
        assert "held_vwap" in health.intact_traits
        assert "held_flag_low" in health.intact_traits
        assert not health.degraded_traits

    def test_broken_vwap_lowers_score(self):
        from backend.bot.thesis import calculate_health
        profile = _make_trustworthy_profile()
        pos = {
            "current_price": 100.0,
            "vwap": 105.0,        # below VWAP → degraded
            "flag_low": 100.5,    # below flag_low → degraded
        }
        health = calculate_health(pos, None, profile)
        assert not health.abstain
        assert "held_vwap" in health.degraded_traits
        assert "held_flag_low" in health.degraded_traits
        # All defining traits degraded → blended score below midline.
        assert health.score < 50.0

    def test_thin_profile_returns_abstain(self):
        from backend.bot.thesis import calculate_health
        profile = _make_thin_profile()
        pos = {"current_price": 100.0, "vwap": 95.0}
        health = calculate_health(pos, None, profile)
        assert health.abstain
        assert "thin" in health.reason.lower() or "unavailable" in health.reason.lower()

    def test_no_data_to_evaluate_abstains(self):
        from backend.bot.thesis import calculate_health
        profile = _make_trustworthy_profile()
        # Position with no vwap / no flag_low → no traits applicable.
        pos = {"current_price": 100.0}
        health = calculate_health(pos, None, profile)
        assert health.abstain


# ── agent_thesis_health unit tests ─────────────────────────────────────


class TestAgentThesisHealth:
    def test_new_trade_eval_abstains_silently(self):
        from backend.bot.agents.thesis_health import agent_thesis_health
        from backend.bot.agents.contract import REASONING_INSUFFICIENT_SIGNAL

        v = agent_thesis_health({"action": "BUY_CALL"})
        assert v.reasoning_type == REASONING_INSUFFICIENT_SIGNAL
        assert v.stance == "abstain"
        assert v.key_drivers == []

    def test_strong_profile_with_matching_position_holds(self):
        from backend.bot.agents.thesis_health import agent_thesis_health
        from backend.bot.agents import STANCE_HOLD

        ctx = {
            "action": "MANAGE_POSITION",
            "open_position": {
                "option_type": "call",
                "current_price": 5.0,
                "vwap": 95.0,
                "flag_low": 90.0,
            },
            "winner_profile": _make_trustworthy_profile().to_dict(),
        }
        # Hydrate the position with the actual underlying price the
        # trait checks compare against vwap/flag_low. (current_price
        # for the OPTION mid is fine; for the trait check we need
        # the underlying — that's why the engine surfaces vwap via
        # market_data.) For the unit test we put underlying values
        # under both keys.
        ctx["open_position"]["current_price"] = 110.0

        v = agent_thesis_health(ctx)
        assert v.stance == STANCE_HOLD

    def test_strong_profile_with_degraded_position_votes_exit(self):
        from backend.bot.agents.thesis_health import agent_thesis_health
        from backend.bot.agents import STANCE_SELL

        ctx = {
            "action": "MANAGE_POSITION",
            "open_position": {
                "option_type": "call",
                "current_price": 95.0,    # underlying < vwap
                "vwap": 105.0,
                "flag_low": 100.0,
            },
            "winner_profile": _make_trustworthy_profile().to_dict(),
        }
        v = agent_thesis_health(ctx)
        # SELL on a long call = exit.
        assert v.stance == STANCE_SELL
        assert v.confidence >= 0.55
        assert "THESIS-HEALTH EXIT" in v.reasoning

    def test_thin_profile_abstains(self):
        from backend.bot.agents.thesis_health import agent_thesis_health
        from backend.bot.agents.contract import REASONING_INSUFFICIENT_SIGNAL

        ctx = {
            "action": "MANAGE_POSITION",
            "open_position": {"option_type": "call", "current_price": 100.0,
                                  "vwap": 95.0},
            "winner_profile": _make_thin_profile().to_dict(),
        }
        v = agent_thesis_health(ctx)
        assert v.reasoning_type == REASONING_INSUFFICIENT_SIGNAL
        assert v.stance == "abstain"

    def test_put_position_exit_uses_buy_stance(self):
        from backend.bot.agents.thesis_health import agent_thesis_health
        from backend.bot.agents import STANCE_BUY

        ctx = {
            "action": "MANAGE_POSITION",
            "open_position": {
                "option_type": "put",
                "current_price": 95.0,
                "vwap": 105.0,
                "flag_low": 100.0,
            },
            "winner_profile": _make_trustworthy_profile().to_dict(),
        }
        v = agent_thesis_health(ctx)
        # On a put, "exit" = BUY (covering the short or closing the put).
        assert v.stance == STANCE_BUY


# ── registry ──────────────────────────────────────────────────────────


class TestRegistry:
    def test_eight_agents_registered(self):
        # Phase 14.C added ``simulator`` (the 8th agent). The roster:
        # market, microstructure, macro, portfolio_risk,
        # mechanical_trend, thesis_health, simulator, devils_advocate.
        from backend.bot.agents import AGENT_FUNCS
        assert len(AGENT_FUNCS) == 8
        names = {n for n, _, _ in AGENT_FUNCS}
        assert "thesis_health" in names
        assert "simulator" in names

    def test_list_agents_endpoint_includes_thesis_health(self):
        from backend.bot.agents import list_agents
        agents = list_agents()
        # Phase 14.C added ``simulator`` (the 8th agent).
        assert len(agents) == 8
        assert any(a["agent"] == "thesis_health" for a in agents)


# ── winner-profile builder integration ─────────────────────────────────


class TestWinnerProfileBuilder:
    def test_builds_profile_from_corpus(self):
        from backend.bot.thesis import build_winner_profile

        # Seed 40 winners with held_vwap features.
        import json
        from backend.db import session_scope
        from backend.models.market_observation import MarketObservation
        from backend.models.market_outcome import MarketOutcome

        with session_scope() as s:
            base_ts = datetime(2024, 1, 1, 10, 0)
            for i in range(40):
                obs = MarketObservation(
                    ticker="NVDA", pattern="bull_flag",
                    timestamp=base_ts + timedelta(days=i),
                    timeframe="1d",
                    regime="trending_up", vol_state="normal",
                    time_bucket="rth",
                    spot=100.0,
                    features=json.dumps({"price_vs_vwap": 1.0,
                                                  "price_vs_flag_low": 1.0}),
                    source="historical_replay",
                )
                s.add(obs)
                s.flush()
                s.add(MarketOutcome(
                    observation_id=obs.id,
                    horizon="1d",
                    entry_price=100.0, exit_price=103.0,
                    return_pct=0.03, was_winner=True,
                ))
            # 10 losers
            for i in range(10):
                obs = MarketObservation(
                    ticker="NVDA", pattern="bull_flag",
                    timestamp=base_ts + timedelta(days=100 + i),
                    timeframe="1d",
                    regime="trending_up", vol_state="normal",
                    time_bucket="rth", spot=100.0,
                    features=json.dumps({}),
                    source="historical_replay",
                )
                s.add(obs)
                s.flush()
                s.add(MarketOutcome(
                    observation_id=obs.id, horizon="1d",
                    entry_price=100.0, exit_price=97.0,
                    return_pct=-0.03, was_winner=False,
                ))

        profile = build_winner_profile(
            pattern="bull_flag", regime="trending_up", horizon="1d",
            ticker="NVDA",
        )
        assert profile.sample_size == 40
        assert profile.is_trustworthy
        assert "held_vwap" in profile.common_traits
        assert "held_flag_low" in profile.common_traits
        assert profile.trait_frequencies["held_vwap"] == pytest.approx(1.0,
                                                                                          abs=1e-6)

    def test_no_winners_returns_zero_sample_profile(self):
        from backend.bot.thesis import build_winner_profile
        profile = build_winner_profile(
            pattern="zzz_does_not_exist", regime="any", horizon="1d",
        )
        assert profile.sample_size == 0
        assert not profile.is_trustworthy
