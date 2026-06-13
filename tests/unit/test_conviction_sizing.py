"""MITS Phase 5 (P5.3) — conviction-weighted sizing tests."""
from __future__ import annotations

import pytest

from backend.bot.eod_sizing import (
    apply_conviction_sizing,
    conviction_multiplier,
)


def test_rank_1_gets_largest_multiplier():
    assert conviction_multiplier(1) == pytest.approx(1.5)


def test_rank_2_and_3_get_neutral_multiplier():
    assert conviction_multiplier(2) == pytest.approx(1.0)
    assert conviction_multiplier(3) == pytest.approx(1.0)


def test_rank_4_plus_gets_half_size():
    assert conviction_multiplier(4) == pytest.approx(0.5)
    assert conviction_multiplier(10) == pytest.approx(0.5)


def test_concurrent_cap_collapses_to_rank_4_plus():
    # Once high-conviction concurrent positions hit the cap, a rank=1
    # proposal collapses to rank_4_plus multiplier.
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=3,
        daily_notional_used=0.0, equity=100_000.0,
        proposed_notional=1000.0,
    )
    assert res.rank_tier == "rank_4_plus"
    assert res.multiplier == pytest.approx(0.5)


def test_clean_rank_1_full_multiplier():
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=0,
        daily_notional_used=0.0, equity=100_000.0,
        proposed_notional=1000.0,
    )
    assert res.rank_tier == "rank_1"
    assert res.multiplier == pytest.approx(1.5)
    assert res.cap_reason is None


def test_daily_notional_cap_truncates_size():
    # 30% of 10k = 3000 budget. We've used 2500. Proposed 1000 × 1.5 =
    # 1500 would push past — should be truncated to use the remaining
    # 500 (multiplier ≈ 0.5).
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=0,
        daily_notional_used=2500.0, equity=10_000.0,
        proposed_notional=1000.0,
    )
    assert res.cap_reason == "daily_notional_cap_truncated"
    # remaining = 3000 - 2500 = 500, multiplier = 500 / 1000 = 0.5
    assert res.multiplier == pytest.approx(0.5)


def test_daily_notional_cap_exhausted_returns_zero():
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=0,
        daily_notional_used=3000.0, equity=10_000.0,
        proposed_notional=1000.0,
    )
    assert res.multiplier == 0.0
    assert res.cap_reason == "daily_notional_cap_exhausted"


def test_catalyst_zero_collapses_multiplier_to_zero():
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=0,
        daily_notional_used=0.0, equity=10_000.0,
        proposed_notional=1000.0, catalyst_multiplier=0.0,
    )
    assert res.multiplier == 0.0
    assert res.cap_reason == "catalyst_abstain"


def test_catalyst_half_compounds_with_rank():
    # rank_1 (1.5) × catalyst (0.5) = 0.75
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=0,
        daily_notional_used=0.0, equity=100_000.0,
        proposed_notional=1000.0, catalyst_multiplier=0.5,
    )
    assert res.multiplier == pytest.approx(0.75)
    assert res.cap_reason is None


def test_to_dict_serializes():
    res = apply_conviction_sizing(
        rank=1, high_conviction_open=0,
        daily_notional_used=0.0, equity=100_000.0,
        proposed_notional=1000.0,
    )
    d = res.to_dict()
    assert {"multiplier", "notional_cap_remaining", "cap_reason", "rank_tier"} <= set(d.keys())
