"""MITS Phase 7.3 — opportunistic trade gate tests."""
from __future__ import annotations

import pytest

from backend.bot.ai.opportunity_brain import OpportunityHypothesis
from backend.bot.gates.opportunistic_gate import (
    OpportunisticGateResult,
    vet,
)


def _hyp(direction="long_put", conviction=0.7, dte_bucket="1d",
            regime_state="panic"):
    return OpportunityHypothesis(
        ticker="SPY", direction=direction, dte_bucket=dte_bucket,
        conviction=conviction, thesis="t", notes="n",
        regime_state=regime_state,
    )


def test_passes_when_conviction_above_floor():
    res = vet(_hyp(conviction=0.50), {})
    # 0.50 ≥ default opportunistic floor 0.45 → passes.
    assert res.passes is True
    assert res.posterior_floor == pytest.approx(0.45)


def test_blocks_when_conviction_below_opportunistic_floor():
    res = vet(_hyp(conviction=0.30), {})
    assert res.passes is False
    assert "below" in (res.reason or "")


def test_skip_direction_returns_passes_false():
    res = vet(_hyp(direction="skip", conviction=0.95), {})
    assert res.passes is False
    assert "skip" in (res.reason or "")


def test_statistical_layer_would_abstain_at_same_posterior():
    """0.50 conviction: passes the opportunistic gate (floor 0.45) but
    sits well below the statistical layer's 0.60 floor — that's the
    whole point of Phase 7."""
    statistical_floor = 0.60
    opportunistic_floor = 0.45
    posterior = 0.50
    assert posterior < statistical_floor
    assert posterior >= opportunistic_floor
    res = vet(_hyp(conviction=posterior), {})
    assert res.passes is True


def test_panic_regime_caps_dte_at_one_day():
    res = vet(_hyp(regime_state="panic", dte_bucket="0d"), {})
    assert res.passes is True
    assert res.dte == 0
    assert res.dte_bucket == "0d"


def test_capitulation_regime_prefers_short_dte():
    res = vet(_hyp(regime_state="capitulation", dte_bucket="1d"), {})
    assert res.passes is True
    assert res.dte <= 1
    assert res.side == "long_put"


def test_squeeze_regime_prefers_long_call():
    res = vet(_hyp(regime_state="squeeze", direction="long_call",
                       dte_bucket="0d"), {})
    assert res.passes is True
    assert res.side == "long_call"
    assert res.dte == 0


def test_trending_regime_picks_3_to_5_dte():
    res = vet(_hyp(regime_state="trending_up", direction="long_call",
                       dte_bucket="3-5d"), {})
    assert res.passes is True
    assert 3 <= res.dte <= 5
    assert res.dte_bucket == "3-5d"


def test_must_exit_by_eod_set_on_every_pass():
    res = vet(_hyp(), {})
    assert res.passes is True
    assert res.must_exit_by_eod is True


def test_dynamic_stop_loss_from_atr_30m():
    ctx = {"atr_30m": 2.0, "price": 400.0}
    res = vet(_hyp(), ctx)
    assert res.passes is True
    # 1.5 × 2.0 / 400 * 100 = 0.75%
    assert res.stop_loss_pct == pytest.approx(0.75, rel=1e-2)


def test_stop_loss_none_when_no_atr_available():
    res = vet(_hyp(), {"price": 400.0})
    assert res.passes is True
    assert res.stop_loss_pct is None


def test_to_dict_serializes_all_fields():
    res = vet(_hyp(), {"atr_30m": 2.0, "price": 400.0})
    d = res.to_dict()
    assert {"passes", "dte", "dte_bucket", "instrument",
              "side", "must_exit_by_eod", "stop_loss_pct"} <= set(d.keys())


def test_accepts_plain_dict_hypothesis():
    """Tests + JSON round-trips can pass a plain dict instead of an
    :class:`OpportunityHypothesis` instance."""
    plain = {
        "direction": "long_call", "conviction": 0.75,
        "dte_bucket": "1d", "regime_state": "squeeze",
    }
    res = vet(plain, {})
    assert res.passes is True
    assert res.side == "long_call"


def test_iron_condor_routes_to_spread_instrument():
    res = vet(_hyp(direction="iron_condor", regime_state="trending_up",
                       dte_bucket="3-5d"), {})
    assert res.passes is True
    assert res.instrument == "spread"
    assert res.side == "iron_condor"
