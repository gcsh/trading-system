"""MITS Phase 18-FU Gap 4 — historical-replay backfill unit tests.

Covers:

  * Kill switches: dual-flag (TUNABLES + env var) required.
  * dry_run=True returns counts WITHOUT writing.
  * source_kind='synthetic_backfill' on every synthesized row.
  * signal_source='historical_replay_backfill' on every Trade.
  * Idempotency: second call writes 0 new rows.
  * Default attribution read EXCLUDES synthetic rows.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List

import pytest

from backend.bot.learning import backfill as backfill_mod
from backend.bot.learning.attribution import (
    UNATTRIBUTED_STRATEGY,
    _iter_closed_decisions,
    compute_attribution_report,
)
from backend.bot.learning.backfill import (
    SYNTHETIC_SIGNAL_SOURCE,
    SYNTHETIC_SOURCE_KIND,
    backfill_learning_from_historical_replay,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.trade import Trade


pytestmark = [pytest.mark.unit]


def _seed_corpus(n: int = 25, days_back_offset: int = 30) -> List[int]:
    """Plant n synthetic MarketObservation × MarketOutcome rows so the
    backfill has something to read. Returns the observation ids."""
    obs_ids: List[int] = []
    base_ts = datetime.utcnow() - timedelta(days=days_back_offset)
    with session_scope() as s:
        for i in range(n):
            obs = MarketObservation(
                ticker=f"T{i:02d}",
                pattern=f"pattern_{i % 4}",
                timestamp=base_ts + timedelta(hours=i),
                timeframe="1d",
                regime="trending_up" if i % 2 == 0 else "ranging",
                vol_state="normal",
                source="historical_replay",
                direction="long",
            )
            s.add(obs)
            s.flush()
            obs_ids.append(int(obs.id))
            outcome = MarketOutcome(
                observation_id=int(obs.id),
                horizon="1d",
                entry_price=100.0,
                exit_price=100.0 + (i % 5) - 2.0,
                return_pct=((i % 5) - 2.0) / 100.0,  # in [-0.02, 0.02]
                was_winner=(i % 5) >= 3,
            )
            s.add(outcome)
        s.flush()
    return obs_ids


def _enable_backfill(monkeypatch):
    monkeypatch.setattr(TUNABLES, "learning_backfill_enabled", True)
    monkeypatch.setenv("TB_LEARNING_BACKFILL_ENABLED", "1")


# ── Kill switches ────────────────────────────────────────────────────


def test_disabled_when_tunables_flag_off(temp_db, monkeypatch):
    monkeypatch.setattr(TUNABLES, "learning_backfill_enabled", False)
    monkeypatch.setenv("TB_LEARNING_BACKFILL_ENABLED", "1")
    result = backfill_learning_from_historical_replay(dry_run=True)
    assert result.flag_enabled is False
    assert result.error is not None
    assert result.n_written == 0


def test_disabled_when_env_flag_off(temp_db, monkeypatch):
    monkeypatch.setattr(TUNABLES, "learning_backfill_enabled", True)
    monkeypatch.delenv("TB_LEARNING_BACKFILL_ENABLED", raising=False)
    result = backfill_learning_from_historical_replay(dry_run=True)
    assert result.env_enabled is False
    assert result.error is not None
    assert result.n_written == 0


# ── dry_run ──────────────────────────────────────────────────────────


def test_dry_run_reports_counts_without_writing(temp_db, monkeypatch):
    _enable_backfill(monkeypatch)
    _seed_corpus(n=20)
    result = backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=10, dry_run=True,
    )
    assert result.dry_run is True
    assert result.error is None
    assert result.n_eligible_observations >= 10
    assert result.n_to_write > 0
    assert result.n_written == 0  # dry_run did not write
    with session_scope() as s:
        from sqlalchemy import func, select as _select
        n_synth = int(
            s.execute(
                _select(func.count(Trade.id)).where(
                    Trade.signal_source == SYNTHETIC_SIGNAL_SOURCE,
                )
            ).scalar() or 0
        )
        assert n_synth == 0


# ── Live write path ──────────────────────────────────────────────────


def test_live_write_tags_source_kind_correctly(temp_db, monkeypatch):
    _enable_backfill(monkeypatch)
    _seed_corpus(n=8)
    result = backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=5, dry_run=False,
    )
    assert result.error is None
    assert result.n_written > 0
    from sqlalchemy import select as _select
    with session_scope() as s:
        trades = s.execute(
            _select(Trade).where(
                Trade.signal_source == SYNTHETIC_SIGNAL_SOURCE,
            )
        ).scalars().all()
        assert len(trades) == result.n_written
        for t in trades:
            assert t.source_kind == SYNTHETIC_SOURCE_KIND
            assert t.signal_source == SYNTHETIC_SIGNAL_SOURCE
            assert t.status == "closed"
            assert t.pnl is not None
        provs = s.execute(
            _select(DecisionProvenance).where(
                DecisionProvenance.source_kind == SYNTHETIC_SOURCE_KIND,
            )
        ).scalars().all()
        assert len(provs) >= result.n_written


# ── Idempotency ──────────────────────────────────────────────────────


def test_idempotency_second_call_writes_nothing(temp_db, monkeypatch):
    """Re-running with the same source data + same cap produces NO
    new rows — every eligible observation that was processed last
    time is skipped via the dedup key."""
    _enable_backfill(monkeypatch)
    _seed_corpus(n=5)
    r1 = backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=10, dry_run=False,
    )
    assert r1.n_written > 0
    n_first = r1.n_written
    r2 = backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=10, dry_run=False,
    )
    assert r2.n_written == 0
    assert r2.n_already_synthesized >= n_first


# ── Attribution exclusion (default) ──────────────────────────────────


def test_default_attribution_excludes_synthetic(temp_db, monkeypatch):
    """The load-bearing safety contract: synthetic rows MUST NOT bleed
    into the default attribution scorecard."""
    _enable_backfill(monkeypatch)
    _seed_corpus(n=8)
    backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=5, dry_run=False,
    )
    # Default: include_synthetic=False — every Trade we just wrote
    # should be invisible to _iter_closed_decisions.
    default_rows = _iter_closed_decisions(
        window_days=90, include_synthetic=False,
    )
    # No synthetic rows visible to default read.
    for d in default_rows:
        # Trade.id is set on every closed decision; we look up the
        # underlying trade to confirm source_kind != synthetic_backfill.
        with session_scope() as s:
            t = s.get(Trade, d.trade_id)
            assert t.source_kind != SYNTHETIC_SOURCE_KIND


def test_include_synthetic_flag_surfaces_synthetic(temp_db, monkeypatch):
    """The opt-in flag flips synthetic rows back into the read."""
    _enable_backfill(monkeypatch)
    _seed_corpus(n=8)
    backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=5, dry_run=False,
    )
    incl = _iter_closed_decisions(
        window_days=90, include_synthetic=True,
    )
    # At least one synthetic row should now appear.
    with session_scope() as s:
        n_synth_visible = 0
        for d in incl:
            t = s.get(Trade, d.trade_id)
            if t and t.source_kind == SYNTHETIC_SOURCE_KIND:
                n_synth_visible += 1
        assert n_synth_visible > 0


def test_attribution_report_carries_include_synthetic_flag(
    temp_db, monkeypatch,
):
    """The composite report records WHICH read it ran so the operator
    can see at a glance whether synthetic rows participated."""
    _enable_backfill(monkeypatch)
    _seed_corpus(n=4)
    backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=4, dry_run=False,
    )
    default_rep = compute_attribution_report(window_days=90)
    assert default_rep["include_synthetic"] is False
    incl_rep = compute_attribution_report(
        window_days=90, include_synthetic=True,
    )
    assert incl_rep["include_synthetic"] is True
