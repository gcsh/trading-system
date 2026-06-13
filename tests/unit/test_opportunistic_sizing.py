"""MITS Phase 7.5 — inverted sizing on crisis-opportunity tests."""
from __future__ import annotations

import pytest

from backend.bot.eod_sizing import (
    OpportunisticSizingResult,
    opportunistic_multiplier,
    opportunistic_sizing,
)


# ---- multiplier --------------------------------------------------------


def test_panic_high_conviction_doubles_size():
    assert opportunistic_multiplier(
        conviction=0.85, regime="panic") == pytest.approx(2.0)


def test_capitulation_high_conviction_doubles_size():
    assert opportunistic_multiplier(
        conviction=0.85, regime="capitulation") == pytest.approx(2.0)


def test_squeeze_high_conviction_doubles_size():
    assert opportunistic_multiplier(
        conviction=0.85, regime="squeeze") == pytest.approx(2.0)


def test_trending_up_high_conviction_one_and_half():
    assert opportunistic_multiplier(
        conviction=0.85, regime="trending_up") == pytest.approx(1.5)


def test_trending_down_high_conviction_one_and_half():
    assert opportunistic_multiplier(
        conviction=0.85, regime="trending_down") == pytest.approx(1.5)


def test_normal_regime_returns_neutral_multiplier():
    assert opportunistic_multiplier(
        conviction=0.95, regime="normal") == pytest.approx(1.0)


def test_low_conviction_collapses_to_neutral_regardless_of_regime():
    assert opportunistic_multiplier(
        conviction=0.40, regime="panic") == pytest.approx(1.0)


# ---- sizing pass: caps + concurrency -----------------------------------


def test_clean_panic_high_conviction_full_multiplier():
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=0.0,
        concurrent_open=0,
    )
    assert res.multiplier == pytest.approx(2.0)
    assert res.cap_reason is None


def test_concurrency_cap_blocks_new_trade():
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=0.0,
        concurrent_open=3,
    )
    assert res.multiplier == 0.0
    assert res.concurrency_limited is True
    assert res.cap_reason == "opportunistic_max_concurrent_reached"


def test_daily_notional_cap_truncates():
    # daily cap 100% of equity = 10,000. Already used 9,500.
    # Single trade can also be at most 50% (5,000), but the daily cap
    # truncation kicks first (remaining 500).
    # proposed 1000 × 2.0 = 2000 → truncated to remaining 500.
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=9_500.0,
        concurrent_open=0,
    )
    assert res.cap_reason == "opportunistic_single_notional_truncated"
    # truncated multiplier = 500 / 1000 = 0.5
    assert res.multiplier == pytest.approx(0.5)


def test_daily_notional_cap_exhausted():
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=10_000.0,
        concurrent_open=0,
    )
    assert res.multiplier == 0.0
    assert res.cap_reason == "opportunistic_daily_cap_exhausted"


def test_per_trade_cap_at_fifty_percent_equity():
    """Even when daily budget allows it, a single opportunistic trade
    can't exceed 50% of equity at default settings."""
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=4000.0, daily_notional_used=0.0,
        concurrent_open=0,
    )
    # 4000 × 2.0 = 8000 > 5000 (50%); truncated to 5000 → mult ≈ 1.25
    assert res.cap_reason == "opportunistic_single_notional_truncated"
    assert res.multiplier == pytest.approx(1.25)


def test_catalyst_abstain_collapses_multiplier_to_zero():
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=0.0,
        concurrent_open=0, catalyst_multiplier=0.0,
    )
    assert res.multiplier == 0.0
    assert res.cap_reason == "catalyst_abstain"


def test_catalyst_does_not_shrink_when_regime_non_normal_and_high_conviction():
    """Operator spec: catalyst gate should NOT shrink size on
    opportunistic high-conviction crisis trades — those ARE the
    opportunity. We model that by treating catalyst_multiplier=1.0
    as the engine's planned override path, which compounds cleanly
    with the inverted multiplier."""
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=0.0,
        concurrent_open=0, catalyst_multiplier=1.0,
    )
    assert res.multiplier == pytest.approx(2.0)


def test_catalyst_half_does_compound_when_engine_passes_it():
    """When the engine passes a non-1.0 catalyst multiplier (e.g.
    overridden because the trade went through the catalyst layer for
    short-DTE-into-earnings reasons), compounding still works
    mechanically. The override decision is made upstream."""
    res = opportunistic_sizing(
        conviction=0.85, regime="trending_up", equity=10_000.0,
        proposed_notional=500.0, daily_notional_used=0.0,
        concurrent_open=0, catalyst_multiplier=0.5,
    )
    # 1.5 × 0.5 = 0.75
    assert res.multiplier == pytest.approx(0.75)


def test_to_dict_serializes_sizing_result():
    res = opportunistic_sizing(
        conviction=0.85, regime="panic", equity=10_000.0,
        proposed_notional=1000.0, daily_notional_used=0.0,
        concurrent_open=0,
    )
    d = res.to_dict()
    assert {"multiplier", "notional_cap_remaining",
              "cap_reason", "concurrency_limited"} <= set(d.keys())
