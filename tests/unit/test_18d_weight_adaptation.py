"""MITS Phase 18.D — Online Agent Weight Adaptation (Advisory) unit tests.

Covers:

  * Insufficient-data path: n_closed < min_n forces multiplier=1.0 +
    confidence_level='insufficient_data'.
  * Boost path: low brier + high hit_rate + large n → multiplier > 1.0
    AND clamped at 1.5x base.
  * Penalty path: high brier + low hit_rate + large n → multiplier < 1.0
    AND clamped at 0.5x base.
  * Bayesian shrinkage: extreme brier at n=10 still yields a multiplier
    near 1.0 (the prior dominates).
  * get_current_weights() returns AGENT_BASE_WEIGHTS verbatim when
    ``adaptive_weights_apply_enabled`` is False.
  * get_current_weights() returns adaptive values when apply_enabled
    is True AND history rows exist.
  * Each of the 8 known agents appears in compute_weight_proposals().
  * to_dict round-trips losslessly via json.dumps/loads.
  * Append-only history: persist twice + the row count doubles.
  * persist_weight_proposals returns the correct count.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

import pytest
from sqlalchemy import func, select

from backend.bot.learning.attribution import KNOWN_AGENTS
from backend.bot.learning.weight_adaptation import (
    AGENT_BASE_WEIGHTS,
    AgentWeightProposal,
    DEFAULT_MAX_BOOST,
    DEFAULT_MAX_PENALTY,
    DEFAULT_MIN_N,
    WeightAdaptationReport,
    _adaptive_multiplier,
    _clamp_weight,
    _confidence_level,
    compute_weight_proposals,
    get_current_weights,
    history_for_agent,
    latest_weight_rows,
    persist_weight_proposals,
)
from backend.config import TUNABLES


pytestmark = [pytest.mark.unit]


# ── Synthetic AgentCalibration shim (skip the AgentCalibration import to
#     keep the test independent of attribution's internal dataclass shape) ─


@dataclass
class _FakeCal:
    """Mirrors the fields ``compute_weight_proposals`` reads off an
    AgentCalibration. Using a small in-test dataclass keeps the unit
    suite free of attribution's heavyweight ``_iter_closed_decisions``
    DB scan."""
    agent: str
    n_closed: int
    hit_rate: Optional[float] = None
    brier_score: Optional[float] = None
    ece: Optional[float] = None


def _cals_for(*pairs) -> List[_FakeCal]:
    """``pairs`` is a list of (agent, n, hit_rate, brier) tuples. Builds
    a list of ``_FakeCal`` covering ONLY the agents named — other
    KNOWN_AGENTS receive no calibration row and the advisor returns
    insufficient_data for them."""
    return [
        _FakeCal(agent=a, n_closed=n, hit_rate=hr, brier_score=br)
        for (a, n, hr, br) in pairs
    ]


# ── Math: confidence_level + adaptive_multiplier + clamp ────────────


def test_confidence_level_below_min_n_is_insufficient():
    assert _confidence_level(n_closed=5, brier=0.10, min_n=30) == \
        "insufficient_data"
    assert _confidence_level(n_closed=29, brier=0.10, min_n=30) == \
        "insufficient_data"


def test_confidence_level_high_with_large_n():
    assert _confidence_level(n_closed=250, brier=0.10, min_n=30) == "high"


def test_confidence_level_medium_with_mid_n():
    assert _confidence_level(n_closed=100, brier=0.20, min_n=30) == "medium"


def test_confidence_level_low_when_brier_none():
    """brier=None ⇒ agent never made a directional bet — label 'low'
    even when n_closed clears the floor."""
    assert _confidence_level(n_closed=100, brier=None, min_n=30) == "low"


def test_adaptive_multiplier_returns_1_when_below_min_n():
    """Hard floor — no adaptation below min_n."""
    assert _adaptive_multiplier(n_closed=10, brier=0.0, min_n=30) == 1.0
    assert _adaptive_multiplier(n_closed=29, brier=0.50, min_n=30) == 1.0


def test_adaptive_multiplier_returns_1_when_brier_none():
    """No directional bets ⇒ no signal ⇒ no adaptation."""
    assert _adaptive_multiplier(n_closed=200, brier=None, min_n=30) == 1.0


def test_adaptive_multiplier_at_random_baseline_is_neutral():
    """brier=0.25 (random) ⇒ delta=0 ⇒ multiplier=1.0 regardless of n."""
    assert abs(_adaptive_multiplier(
        n_closed=200, brier=0.25, min_n=30,
    ) - 1.0) < 1e-9


def test_adaptive_multiplier_boost_path():
    """brier=0.0 + n=200 ⇒ delta = +0.5, shrink = 200/250 = 0.8
    ⇒ multiplier ≈ 1.4. Must be > 1.0 and well above the floor."""
    m = _adaptive_multiplier(n_closed=200, brier=0.0, min_n=30)
    assert m > 1.0
    assert abs(m - 1.4) < 1e-6


def test_adaptive_multiplier_penalty_path():
    """brier=0.50 + n=200 ⇒ delta = -0.5, shrink = 0.8 ⇒ multiplier ≈ 0.6."""
    m = _adaptive_multiplier(n_closed=200, brier=0.50, min_n=30)
    assert m < 1.0
    assert abs(m - 0.6) < 1e-6


def test_adaptive_multiplier_shrinkage_keeps_thin_samples_near_one():
    """Even an EXTREME brier (0.0 = perfect) at n=10 — well below the
    prior_n=50 — must yield a multiplier close to 1.0. The Bayesian
    prior dominates."""
    # The math: floor below min_n=30 keeps it at 1.0 anyway. Test the
    # boundary case: n=30 (the floor) with brier=0.0 yields
    # delta=0.5 * 30/(30+50) = 0.1875 ⇒ multiplier=1.1875 — bigger
    # than 1.0 but FAR from the unshrunk +0.5 delta.
    m = _adaptive_multiplier(n_closed=30, brier=0.0, min_n=30)
    assert 1.0 < m < 1.25
    # And n=50 with brier=0.0 yields 1 + 0.5 * 50/100 = 1.25 — still
    # well below the unshrunk +0.5.
    m = _adaptive_multiplier(n_closed=50, brier=0.0, min_n=30)
    assert abs(m - 1.25) < 1e-6


def test_clamp_weight_enforces_hard_bounds():
    """No matter how extreme the multiplier, the proposed weight
    cannot escape [0.5*base, 1.5*base]."""
    assert _clamp_weight(1.0, 10.0) == DEFAULT_MAX_BOOST
    assert _clamp_weight(1.0, -5.0) == DEFAULT_MAX_PENALTY
    # In-range values pass through unchanged.
    assert abs(_clamp_weight(1.0, 1.25) - 1.25) < 1e-9


# ── compute_weight_proposals — listing + clamp + insufficient path ──


def test_every_known_agent_appears_in_proposals():
    """Even when the calibration list is empty, every KNOWN_AGENT must
    surface as a proposal (so the operator sees the FULL council)."""
    report = compute_weight_proposals(
        agent_calibrations=[], axis_calibrations=[],
    )
    agents = {p.agent for p in report.proposals}
    assert agents == set(KNOWN_AGENTS)
    # And all of them must be insufficient_data (no rows = no signal).
    for p in report.proposals:
        assert p.confidence_level == "insufficient_data"
        assert p.adaptive_multiplier == 1.0
        assert p.weight_proposed == AGENT_BASE_WEIGHTS[p.agent]


def test_boost_for_well_calibrated_agent_with_full_sample():
    """brier=0.0 + hit_rate=0.7 + n=200 ⇒ multiplier > 1.0 and
    weight_proposed ≤ 1.5 × base (clamp enforces the ceiling)."""
    cals = _cals_for(("market", 200, 0.70, 0.0))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    market = next(p for p in report.proposals if p.agent == "market")
    assert market.adaptive_multiplier > 1.0
    assert market.weight_proposed <= 1.5 * AGENT_BASE_WEIGHTS["market"]
    assert market.confidence_level == "high"   # n=200 hits CONFIDENCE_HIGH_N
    # The cohort note: GOOD calibration ⇒ up-weight recommended
    assert "up-weight" in market.rationale.lower()


def test_penalty_for_poorly_calibrated_agent_with_full_sample():
    """brier=0.40 + hit_rate=0.30 + n=200 ⇒ multiplier < 1.0 and
    weight_proposed ≥ 0.5 × base (clamp enforces the floor)."""
    cals = _cals_for(("macro", 200, 0.30, 0.40))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    macro = next(p for p in report.proposals if p.agent == "macro")
    assert macro.adaptive_multiplier < 1.0
    assert macro.weight_proposed >= 0.5 * AGENT_BASE_WEIGHTS["macro"]
    assert "down-weight" in macro.rationale.lower()


def test_clamp_floor_holds_under_extreme_penalty():
    """brier=1.0 + n=10000 ⇒ delta=-1.5, fully shrunk → multiplier=-0.5.
    The clamp MUST keep weight_proposed at 0.5 × base — not negative,
    not zero."""
    cals = _cals_for(("simulator", 10000, 0.0, 1.0))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    sim = next(p for p in report.proposals if p.agent == "simulator")
    base = AGENT_BASE_WEIGHTS["simulator"]
    assert sim.weight_proposed == 0.5 * base


def test_clamp_ceiling_holds_under_extreme_boost():
    """brier=0.0 at very large n approaches but does NOT exceed the
    1.5 × base ceiling. The clamp guarantees the cap holds even when
    the shrinkage barely pulls the multiplier in.

    Math: delta=+0.5, shrink=n/(n+50). At n=100000 the multiplier ≈
    1.4998 (just under the ceiling); the clamp would still apply if
    a different brier value pushed it past 1.5."""
    cals = _cals_for(("market", 100000, 1.0, 0.0))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    market = next(p for p in report.proposals if p.agent == "market")
    base = AGENT_BASE_WEIGHTS["market"]
    # Strict ceiling: never exceeds 1.5 × base.
    assert market.weight_proposed <= 1.5 * base
    # And within rounding of 1.5 — the shrinkage barely costs anything
    # at n=100000.
    assert market.weight_proposed > 1.49 * base
    # Construct a multiplier that would EXCEED the ceiling without the
    # clamp, and verify clamp engages. Negative brier is impossible
    # in practice but the clamp must hold defensively.
    cals2 = _cals_for(("market", 100000, 1.0, -1.0))
    report2 = compute_weight_proposals(
        agent_calibrations=cals2, axis_calibrations=[],
    )
    market2 = next(p for p in report2.proposals if p.agent == "market")
    assert market2.weight_proposed == 1.5 * base


# ── Serialization round-trip ────────────────────────────────────────


def test_proposal_to_dict_round_trips_via_json():
    """Every WeightAdaptationReport.to_dict() must serialize cleanly
    so the cockpit can render it without a custom encoder."""
    cals = _cals_for(("market", 100, 0.60, 0.18))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    raw = report.to_dict()
    s = json.dumps(raw, default=str)
    parsed = json.loads(s)
    assert "proposals" in parsed
    assert len(parsed["proposals"]) == len(KNOWN_AGENTS)
    for p in parsed["proposals"]:
        # Every contract field surfaces.
        for key in (
            "agent", "base_weight", "current_weight", "weight_proposed",
            "adaptive_multiplier", "n_closed", "confidence_level",
            "rationale", "computed_at",
        ):
            assert key in p, f"missing {key}"


# ── Persistence + append-only history ──────────────────────────────


def test_get_current_weights_returns_base_when_apply_disabled(
    monkeypatch,
):
    """``adaptive_weights_apply_enabled=False`` (default) ⇒
    get_current_weights returns AGENT_BASE_WEIGHTS verbatim."""
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", False, raising=False,
    )
    out = get_current_weights()
    assert out == AGENT_BASE_WEIGHTS


def test_get_current_weights_reads_history_when_apply_enabled(
    temp_db, monkeypatch,
):
    """When apply is on AND history rows exist, the engine reads the
    persisted ``weight_active`` per agent. Untracked agents fall back
    to base."""
    # Seed one row for "market" via the persistence helper so we
    # exercise the real append path.
    cals = _cals_for(("market", 200, 0.70, 0.0))
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_advisory_enabled", True, raising=False,
    )
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    written = persist_weight_proposals(report)
    assert written == len(KNOWN_AGENTS)
    # Now flip apply on and confirm the engine reads adaptive for
    # market (proposed > base) and base for all others.
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", True, raising=False,
    )
    current = get_current_weights()
    # market should reflect its boost.
    market_proposed = next(
        p.weight_proposed for p in report.proposals if p.agent == "market"
    )
    assert abs(current["market"] - market_proposed) < 1e-9
    # All other agents must equal their base (calibration was insufficient
    # so the proposal == base, persisted as weight_active).
    for agent in KNOWN_AGENTS:
        if agent == "market":
            continue
        assert current[agent] == AGENT_BASE_WEIGHTS[agent]


def test_persist_weight_proposals_is_append_only(temp_db):
    """Two recomputes ⇒ 2 × len(KNOWN_AGENTS) rows. The advisor never
    UPDATEs an existing row — every batch is a fresh INSERT so the
    operator can roll back."""
    from backend.models.agent_weight_history import AgentWeightHistory

    cals = _cals_for(("market", 100, 0.60, 0.18))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    persist_weight_proposals(report)
    persist_weight_proposals(report)
    with temp_db_session() as s:
        n_rows = s.execute(
            select(func.count()).select_from(AgentWeightHistory)
        ).scalar() or 0
    assert n_rows == 2 * len(KNOWN_AGENTS)


def test_latest_weight_rows_returns_most_recent_batch_only(
    temp_db, monkeypatch,
):
    """latest_weight_rows must scope to the MAX(computed_at) batch —
    the previous batches stay in the table but don't bleed into the
    'current' read."""
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_advisory_enabled", True, raising=False,
    )
    cals = _cals_for(("market", 100, 0.60, 0.18))
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    persist_weight_proposals(report)
    rows_first = latest_weight_rows(limit=64)
    # Hammer a second batch — must displace the first as "latest".
    import time
    time.sleep(0.01)
    cals2 = _cals_for(("market", 300, 0.80, 0.05))
    report2 = compute_weight_proposals(
        agent_calibrations=cals2, axis_calibrations=[],
    )
    persist_weight_proposals(report2)
    rows_latest = latest_weight_rows(limit=64)
    assert len(rows_latest) == len(KNOWN_AGENTS)
    assert rows_latest[0]["computed_at"] >= rows_first[0]["computed_at"]
    # And history_for_agent returns BOTH batches when we query for one
    # agent.
    hist = history_for_agent("market", limit=10)
    assert len(hist) >= 2


# ── Session helper (small DB-context wrapper used above) ────────────


def temp_db_session():
    from backend.db import session_scope
    return session_scope()
