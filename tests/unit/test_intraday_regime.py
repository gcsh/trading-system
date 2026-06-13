"""MITS Phase 7.1 — intraday regime classifier tests."""
from __future__ import annotations

import pytest

from backend.bot.regime.intraday_regime import (
    IntradayRegimeClassifier,
    IntradayRegimeInputs,
    STATES,
    _classify_from_inputs,
)


def _inputs(**kwargs) -> IntradayRegimeInputs:
    return IntradayRegimeInputs(**kwargs)


def test_states_constant_lists_all_seven():
    assert set(STATES) == {
        "normal", "trending_up", "trending_down",
        "panic", "capitulation", "squeeze", "chop",
    }


# ---- per-state classification ----------------------------------------


def test_panic_state_triggers_on_sharp_spy_drop_and_vix_spike():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=-2.0,
        vix_spot=30.0,
        vix_1d_pct_change=35.0,
    ))
    assert state.state == "panic"
    assert state.severity == "high"


def test_capitulation_requires_panic_plus_pcr_and_breadth():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=-2.0,
        vix_spot=30.0,
        vix_1d_pct_change=35.0,
        put_call_ratio=1.5,
        breadth_ratio=0.15,
    ))
    assert state.state == "capitulation"
    assert state.severity == "high"


def test_capitulation_promo_spec_panic_minus_two_pct_vix_30_low_breadth():
    """Spec test: SPY -2% + VIX 30 + low breadth + heavy PCR ⇒ capitulation."""
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=-2.0,
        vix_spot=30.0,
        vix_1d_pct_change=25.0,
        put_call_ratio=1.4,
        breadth_ratio=0.18,
    ))
    assert state.state == "capitulation"


def test_squeeze_state_triggers_on_post_panic_bounce():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=2.0,
        prior_state="panic",
    ))
    assert state.state == "squeeze"


def test_squeeze_state_triggers_on_strong_breadth_recovery():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=1.8,
        breadth_ratio=0.90,
    ))
    assert state.state == "squeeze"


def test_trending_up_on_persistent_directional_move():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=0.9,
    ))
    assert state.state == "trending_up"


def test_trending_down_on_persistent_directional_move():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=-0.9,
    ))
    assert state.state == "trending_down"


def test_chop_on_low_range_and_below_avg_realized_vol():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=0.05,
        spy_pct_change_60m=0.10,
        spy_intraday_realized_vol=0.40,
        spy_realized_vol_10d=0.80,
    ))
    assert state.state == "chop"


def test_normal_state_when_nothing_extreme():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=0.20,
    ))
    assert state.state == "normal"


def test_normal_state_when_all_inputs_missing():
    state = _classify_from_inputs(_inputs())
    assert state.state == "normal"


# ---- state transition persistence ------------------------------------


def test_state_dict_serializes_all_fields():
    state = _classify_from_inputs(_inputs(
        spy_pct_change_30m=-2.0,
        vix_spot=30.0,
        vix_1d_pct_change=35.0,
    ))
    d = state.to_dict()
    assert d["state"] == "panic"
    assert d["vix_spot"] == 30.0
    assert d["spy_pct_change_30m"] == -2.0
    assert isinstance(d["reasons"], list)


def test_transition_persists_event(temp_db):
    from backend.db import session_scope
    from backend.models.intraday_regime_event import IntradayRegimeEvent

    # Build a classifier with a mock market_data that yields a panic
    # state on each call to classify(). We bypass the wire and force
    # via the cache + persist hook.
    classifier = IntradayRegimeClassifier(market_data=None)
    # Force inputs by patching _collect_inputs.
    panic = _inputs(
        spy_pct_change_30m=-2.0, vix_spot=30.0,
        vix_1d_pct_change=35.0, put_call_ratio=1.5,
        breadth_ratio=0.18, prior_state="unknown",
    )
    classifier._collect_inputs = lambda: panic  # type: ignore
    state = classifier.classify()
    assert state.state == "capitulation"

    with session_scope() as s:
        rows = s.query(IntradayRegimeEvent).all()
        assert len(rows) == 1
        assert rows[0].new_state == "capitulation"
        assert rows[0].prior_state == "unknown"


def test_no_persist_when_state_unchanged(temp_db):
    from backend.db import session_scope
    from backend.models.intraday_regime_event import IntradayRegimeEvent

    classifier = IntradayRegimeClassifier(market_data=None)
    # Drive normal -> normal: zero persists.
    classifier._collect_inputs = lambda: _inputs(  # type: ignore
        spy_pct_change_30m=0.1, prior_state=classifier._last_state,
    )
    s1 = classifier.classify()
    # bust the in-process cache so classify recomputes
    classifier._cache = None
    s2 = classifier.classify()
    assert s1.state == s2.state == "normal"
    with session_scope() as s:
        # unknown -> normal counts as one transition (the first call);
        # the second call's state matches the cached _last_state so no
        # additional row is written.
        rows = s.query(IntradayRegimeEvent).all()
        assert len(rows) == 1


def test_classifier_cache_short_circuits_recompute():
    """Within the cache window the classifier returns the cached state
    without re-invoking the input pipeline."""
    classifier = IntradayRegimeClassifier(market_data=None)
    call_counter = {"n": 0}

    def _stub_inputs():
        call_counter["n"] += 1
        return _inputs(spy_pct_change_30m=0.1)

    classifier._collect_inputs = _stub_inputs  # type: ignore
    classifier.classify()
    classifier.classify()
    classifier.classify()
    assert call_counter["n"] == 1
