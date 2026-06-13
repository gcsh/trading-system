"""MITS Phase 17.D — chain selection provenance unit tests.

Each test isolates one observable on ``backend.bot.data.chain_selection``:

  1. ``ChainSelectionProvenance.to_dict`` round-trips JSON-safely.
  2. ``build_provenance_from_candidates`` with rich chain data picks the
     delta-band winner and stamps rejection reasons on the 4 losers.
  3. Paper-stub fallback when no candidates available.
  4. ``chosen_reason`` is non-empty on every chosen contract.
  5. ``freshness_seconds`` is computed correctly from quote timestamps.
  6. All candidates have ``rejection_reason`` populated EXCEPT the chosen one.
  7. Rejection reasons are stable tokens (one of the published set).
  8. ``ChainCandidate.to_dict`` round-trips JSON-safely.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from backend.bot.data.chain_selection import (
    ChainCandidate,
    ChainSelectionProvenance,
    REJECTION_REASONS,
    _paper_stub_provenance,
    _quote_freshness_seconds,
    build_provenance_from_candidates,
)


def _rich_call_candidates():
    """Five AAPL call candidates near $200 spot, weekly expiry.

    Idx 0: delta=0.15 (below band)        -> wrong_delta_band
    Idx 1: delta=0.32 (in band) but OI=500 (low)
    Idx 2: delta=0.38 (in band), OI=4221, tight spread -> CHOSEN
    Idx 3: delta=0.42 (in band), OI=3000, but spread 8% (wide)
    Idx 4: delta=0.60 (above band)        -> wrong_delta_band
    """
    return [
        ChainCandidate(
            expiry="2026-06-19", strike=195.0, option_type="C",
            delta=0.15, open_interest=2000, volume=300,
            bid=5.10, ask=5.20, iv=0.28,
        ),
        ChainCandidate(
            expiry="2026-06-19", strike=200.0, option_type="C",
            delta=0.32, open_interest=500, volume=200,
            bid=2.80, ask=2.85, iv=0.27,
        ),
        ChainCandidate(
            expiry="2026-06-19", strike=205.0, option_type="C",
            delta=0.38, open_interest=4221, volume=1200,
            bid=1.94, ask=1.99, iv=0.26,
        ),
        ChainCandidate(
            expiry="2026-06-19", strike=210.0, option_type="C",
            delta=0.42, open_interest=3000, volume=800,
            bid=1.10, ask=1.20, iv=0.25,
        ),
        ChainCandidate(
            expiry="2026-06-19", strike=215.0, option_type="C",
            delta=0.60, open_interest=2500, volume=500,
            bid=0.60, ask=0.65, iv=0.24,
        ),
    ]


def test_chain_candidate_to_dict_roundtrip():
    cand = ChainCandidate(
        expiry="2026-06-19", strike=205.0, option_type="C",
        delta=0.38, open_interest=4221, volume=1200,
        bid=1.94, ask=1.99, iv=0.26, rejection_reason=None,
    )
    d = cand.to_dict()
    assert d["expiry"] == "2026-06-19"
    assert d["strike"] == 205.0
    assert d["delta"] == 0.38
    assert d["rejection_reason"] is None
    # Must be JSON-serializable.
    s = json.dumps(d)
    again = json.loads(s)
    assert again == d


def test_provenance_to_dict_roundtrip():
    prov = ChainSelectionProvenance(
        ticker="AAPL", direction="long_call",
        requested_dte=30, requested_delta_band=(0.30, 0.45),
        underlying_spot=200.0,
        candidates=[ChainCandidate(
            expiry="2026-06-19", strike=205.0, option_type="C",
            delta=0.38, rejection_reason=None,
        )],
        chosen_expiry="2026-06-19", chosen_strike=205.0,
        chosen_option_type="C",
        chosen_reason="delta=0.38 in [0.30,0.45]",
        freshness_seconds=2.5, chain_source="thetadata",
        captured_at=datetime.utcnow().isoformat(),
    )
    d = prov.to_dict()
    # JSON round-trip cleanly.
    s = json.dumps(d)
    again = json.loads(s)
    assert again["ticker"] == "AAPL"
    assert again["chosen_strike"] == 205.0
    assert again["chain_source"] == "thetadata"
    assert again["requested_delta_band"] == [0.30, 0.45]


def test_delta_band_winner_picked_and_losers_tagged():
    cands = _rich_call_candidates()
    prov = build_provenance_from_candidates(
        ticker="AAPL", direction="long_call",
        requested_dte=30, requested_delta_band=(0.30, 0.45),
        underlying_spot=200.0,
        candidates=cands,
        min_open_interest=1000, min_volume=100,
        max_spread_pct=0.05, max_staleness_seconds=60.0,
    )
    # Winner: idx 2 (delta=0.38 nearest to band midpoint 0.375, OI ok,
    # tight spread).
    assert prov.chosen_strike == 205.0
    assert prov.chosen_option_type == "C"
    chosen = next(c for c in prov.candidates if c.rejection_reason is None)
    assert chosen.strike == 205.0

    # Exactly one chosen.
    chosen_count = sum(1 for c in prov.candidates if c.rejection_reason is None)
    assert chosen_count == 1

    # All others have rejection_reason set.
    rejected = [c for c in prov.candidates if c.rejection_reason is not None]
    assert len(rejected) == 4
    for c in rejected:
        assert c.rejection_reason in REJECTION_REASONS, (
            f"unknown rejection token {c.rejection_reason!r}"
        )

    # Idx 0 + 4 rejected for wrong_delta_band.
    by_strike = {c.strike: c for c in prov.candidates}
    assert by_strike[195.0].rejection_reason == "wrong_delta_band"
    assert by_strike[215.0].rejection_reason == "wrong_delta_band"
    # Idx 1: in-band delta but low OI.
    assert by_strike[200.0].rejection_reason == "low_open_interest"
    # Idx 3: in-band, OI ok, but wide spread (≈8.7%).
    assert by_strike[210.0].rejection_reason == "wide_spread_pct"


def test_paper_stub_fallback_when_no_candidates():
    prov = build_provenance_from_candidates(
        ticker="MSFT", direction="long_call",
        requested_dte=14, requested_delta_band=(0.30, 0.45),
        underlying_spot=420.0,
        candidates=[],
    )
    assert prov.chain_source == "paper_stub"
    assert "paper_stub" in prov.chosen_reason
    # Single candidate (the chosen stub), no rejection on it.
    assert len(prov.candidates) == 1
    assert prov.candidates[0].rejection_reason is None


def test_chosen_reason_is_nonempty():
    cands = _rich_call_candidates()
    prov = build_provenance_from_candidates(
        ticker="AAPL", direction="long_call",
        requested_dte=30, requested_delta_band=(0.30, 0.45),
        underlying_spot=200.0,
        candidates=cands,
    )
    assert prov.chosen_reason
    assert "delta" in prov.chosen_reason.lower() or "source" in prov.chosen_reason.lower()


def test_freshness_seconds_computed():
    now = datetime.utcnow()
    five_seconds_ago = now - timedelta(seconds=5)
    fr = _quote_freshness_seconds(five_seconds_ago)
    assert fr is not None
    # Allow a half-second jitter for clock-tick during the test.
    assert 4.5 < fr < 6.0
    # None input -> None output (vendor didn't supply timestamp).
    assert _quote_freshness_seconds(None) is None


def test_only_chosen_has_no_rejection_reason():
    cands = _rich_call_candidates()
    prov = build_provenance_from_candidates(
        ticker="AAPL", direction="long_call",
        requested_dte=30, requested_delta_band=(0.30, 0.45),
        underlying_spot=200.0,
        candidates=cands,
    )
    none_count = sum(1 for c in prov.candidates if c.rejection_reason is None)
    assert none_count == 1, (
        f"expected exactly one chosen (rejection_reason=None) but got "
        f"{none_count} on candidates: "
        f"{[(c.strike, c.rejection_reason) for c in prov.candidates]}"
    )


def test_freshness_seconds_propagates_into_provenance():
    """When a freshness vector is supplied alongside the candidates, the
    chosen candidate's freshness lands on the provenance."""
    cands = _rich_call_candidates()
    fr = [10.0, 8.0, 2.5, 9.0, 12.0]  # idx 2 = chosen -> 2.5
    prov = build_provenance_from_candidates(
        ticker="AAPL", direction="long_call",
        requested_dte=30, requested_delta_band=(0.30, 0.45),
        underlying_spot=200.0,
        candidates=cands,
        freshnesses=fr,
    )
    assert prov.freshness_seconds == 2.5


def test_rejection_reasons_are_stable_tokens():
    """All published tokens are stable strings — gives the audit + the
    cockpit something deterministic to filter on."""
    for tok in REJECTION_REASONS:
        assert isinstance(tok, str)
        assert tok
        assert " " not in tok


def test_paper_stub_helper_shape():
    prov = _paper_stub_provenance(
        ticker="NVDA", direction="long_call",
        requested_dte=21,
        requested_delta_band=(0.30, 0.45),
        underlying_spot=850.0,
        chosen_expiry="2026-07-19", chosen_strike=860.0,
        chosen_option_type="C",
    )
    d = prov.to_dict()
    assert d["chain_source"] == "paper_stub"
    assert d["chosen_strike"] == 860.0
    assert d["chosen_expiry"] == "2026-07-19"
    assert len(d["candidates"]) == 1
    assert d["candidates"][0]["rejection_reason"] is None
