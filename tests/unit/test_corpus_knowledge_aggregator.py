"""MITS Phase 0 + Phase 1 — knowledge aggregator tests."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta

import pytest

from backend.bot.corpus.knowledge_aggregator import (
    SAMPLE_SPLIT_COMBINED, SAMPLE_SPLIT_IN, SAMPLE_SPLIT_OUT,
    _wilson_interval, recompute_cells, snapshot_cells_to_history,
)
from backend.bot.corpus.priors_loader import load_default_priors
from backend.db import init_db, session_scope
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.knowledge_graph_history import KnowledgeGraphHistory
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture
def fresh_db():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    try:
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed(ticker: str, pattern: str, regime: str, n_winners: int,
              n_losers: int, horizon: str = "1d",
              source: str = "historical_replay",
              ts_start: datetime = datetime(2025, 1, 1)) -> None:
    """Seed (n_winners + n_losers) observations + outcomes for a cell."""
    with session_scope() as s:
        for i in range(n_winners + n_losers):
            obs = MarketObservation(
                ticker=ticker, pattern=pattern,
                timestamp=ts_start + timedelta(days=i),
                timeframe="1d", regime=regime, vol_state="normal",
                time_bucket="rth", spot=100.0,
                source=source,
            )
            s.add(obs)
            s.flush()
            is_winner = i < n_winners
            s.add(MarketOutcome(
                observation_id=obs.id, horizon=horizon,
                entry_price=100.0,
                exit_price=110.0 if is_winner else 90.0,
                return_pct=0.10 if is_winner else -0.10,
                was_winner=is_winner,
            ))


def test_wilson_interval_basic():
    lo, hi = _wilson_interval(5, 10)
    assert lo is not None and hi is not None
    assert 0.0 <= lo <= 0.5 <= hi <= 1.0


def test_wilson_zero_samples():
    lo, hi = _wilson_interval(0, 0)
    assert lo is None and hi is None


def test_aggregates_single_cell(fresh_db):
    load_default_priors()
    _seed("AAPL", "bull_flag", "trending_up", n_winners=14, n_losers=6)
    stats = recompute_cells("AAPL")
    assert stats["cells_inserted"] >= 1
    with session_scope() as s:
        # MITS Phase 1 — 3 split rows per cohort (in_sample, out_of_sample,
        # combined). When seeded data has no live observations, only the
        # in_sample + combined rows materialise. Inspect the combined row
        # since it is the union (matches pre-Phase-1 semantics).
        cells = s.query(KnowledgeGraphCell).filter_by(
            ticker="AAPL", pattern="bull_flag",
            sample_split="combined",
        ).all()
        assert len(cells) == 1
        cell = cells[0]
        assert cell.sample_size == 20
        assert cell.win_rate == pytest.approx(0.70, abs=0.001)
        # Posterior with prior 0.62 / weight 20: (14 + 20*0.62) / (20 + 20)
        # = (14 + 12.4) / 40 = 0.66
        assert cell.posterior_win_rate == pytest.approx(0.66, abs=0.005)
        assert cell.avg_return_pct == pytest.approx(
            (14 * 0.10 + 6 * -0.10) / 20, abs=0.001)
        assert cell.confidence_lower is not None
        assert cell.confidence_upper is not None
        assert cell.confidence_lower < cell.win_rate < cell.confidence_upper


def test_idempotent_recompute(fresh_db):
    load_default_priors()
    _seed("MSFT", "breakout", "trending_up", n_winners=10, n_losers=10)
    first = recompute_cells("MSFT")
    second = recompute_cells("MSFT")
    # MITS Phase 1 — 2 rows per cohort (in_sample + combined) when all
    # observations come from historical_replay source.
    assert first["cells_inserted"] == 2
    assert second["cells_inserted"] == 0
    assert second["cells_updated"] >= 2


def test_posterior_shrinks_toward_prior_when_sample_small(fresh_db):
    load_default_priors()
    # 1 win, 0 losses → frequentist WR 100%. With prior 0.58 / weight 20,
    # posterior = (1 + 20*0.58) / (1 + 20) = 12.6 / 21 ≈ 0.60.
    _seed("TSLA", "breakout", "any", n_winners=1, n_losers=0)
    recompute_cells("TSLA")
    with session_scope() as s:
        cell = s.query(KnowledgeGraphCell).filter_by(
            ticker="TSLA", pattern="breakout",
            sample_split="combined",
        ).first()
        assert cell.win_rate == 1.0
        assert cell.posterior_win_rate < 0.80, (
            "posterior must shrink toward prior with small N")


# ── MITS Phase 1 — walk-forward (in_sample / out_of_sample / combined) ──


def test_walk_forward_emits_three_splits(fresh_db):
    """MITS Phase 1 — when both historical_replay and live observations
    exist for a cohort, the aggregator MUST emit 3 rows (in_sample,
    out_of_sample, combined) for each unique 6-axis cohort key."""
    load_default_priors()
    # 12 in-sample winners + 8 in-sample losers (historical_replay).
    _seed("NVDA", "bull_flag", "trending_up",
          n_winners=12, n_losers=8,
          source="historical_replay",
          ts_start=datetime(2024, 1, 1))
    # 6 out-of-sample winners + 4 out-of-sample losers (live_engine).
    _seed("NVDA", "bull_flag", "trending_up",
          n_winners=6, n_losers=4,
          source="live_engine",
          ts_start=datetime(2025, 6, 1))
    stats = recompute_cells("NVDA")
    assert stats["in_sample_cells"] == 1
    assert stats["out_of_sample_cells"] == 1
    assert stats["combined_cells"] == 1
    with session_scope() as s:
        cells = {c.sample_split: c for c in s.query(KnowledgeGraphCell)
                          .filter_by(ticker="NVDA", pattern="bull_flag").all()}
        assert set(cells.keys()) == {SAMPLE_SPLIT_IN, SAMPLE_SPLIT_OUT,
                                                  SAMPLE_SPLIT_COMBINED}
        assert cells[SAMPLE_SPLIT_IN].sample_size == 20
        assert cells[SAMPLE_SPLIT_IN].win_rate == pytest.approx(0.60, abs=0.001)
        assert cells[SAMPLE_SPLIT_OUT].sample_size == 10
        assert cells[SAMPLE_SPLIT_OUT].win_rate == pytest.approx(0.60, abs=0.001)
        assert cells[SAMPLE_SPLIT_COMBINED].sample_size == 30


def test_walk_forward_only_in_sample_when_no_live(fresh_db):
    """When the corpus only has historical_replay rows, we still emit
    in_sample + combined cells but the out_of_sample slot stays empty."""
    load_default_priors()
    _seed("AMD", "breakout", "trending_up",
          n_winners=5, n_losers=5,
          source="historical_replay")
    stats = recompute_cells("AMD")
    assert stats["in_sample_cells"] == 1
    assert stats["out_of_sample_cells"] == 0
    assert stats["combined_cells"] == 1
    with session_scope() as s:
        rows = s.query(KnowledgeGraphCell).filter_by(
            ticker="AMD", pattern="breakout").all()
        splits = {r.sample_split for r in rows}
        assert splits == {SAMPLE_SPLIT_IN, SAMPLE_SPLIT_COMBINED}


def test_walk_forward_idempotent(fresh_db):
    """Re-running recompute on mixed in+out data updates instead of
    duplicating the 3 split rows."""
    load_default_priors()
    _seed("AMZN", "breakout", "trending_up",
          n_winners=4, n_losers=4, source="historical_replay")
    _seed("AMZN", "breakout", "trending_up",
          n_winners=3, n_losers=2, source="live_engine",
          ts_start=datetime(2025, 7, 1))
    first = recompute_cells("AMZN")
    second = recompute_cells("AMZN")
    assert first["cells_inserted"] == 3
    assert second["cells_inserted"] == 0
    assert second["cells_updated"] >= 3


# ── MITS Phase 1 — sparkline history snapshot ──


def test_snapshot_cells_to_history_inserts(fresh_db):
    """`snapshot_cells_to_history` must create one history row per cell
    for the given calendar date."""
    load_default_priors()
    _seed("META", "bull_flag", "trending_up",
          n_winners=6, n_losers=4, source="historical_replay")
    recompute_cells("META")
    stats = snapshot_cells_to_history()
    assert stats["inserted"] >= 2  # in_sample + combined
    with session_scope() as s:
        rows = s.query(KnowledgeGraphHistory).filter_by(
            ticker="META", pattern="bull_flag").all()
        assert len(rows) >= 2
        for r in rows:
            assert r.snapshot_date == date.today()
            assert r.sample_size == 10
            assert r.posterior_win_rate is not None


def test_snapshot_cells_to_history_idempotent(fresh_db):
    """Re-running on the same day updates instead of duplicating."""
    load_default_priors()
    _seed("GOOG", "breakout", "trending_up",
          n_winners=5, n_losers=5, source="historical_replay")
    recompute_cells("GOOG")
    first = snapshot_cells_to_history()
    second = snapshot_cells_to_history()
    assert first["inserted"] >= 2
    assert second["inserted"] == 0
    assert second["updated"] >= 2
    with session_scope() as s:
        rows = s.query(KnowledgeGraphHistory).filter_by(
            ticker="GOOG", pattern="breakout").all()
        # Same number after second run — no duplicates.
        assert len(rows) == first["inserted"]


def test_snapshot_cells_to_history_specific_date(fresh_db):
    """Passing snapshot_date overrides 'today'."""
    load_default_priors()
    _seed("NFLX", "pullback", "trending_up",
          n_winners=4, n_losers=6, source="historical_replay")
    recompute_cells("NFLX")
    custom_date = date(2024, 12, 31)
    snapshot_cells_to_history(snapshot_date=custom_date)
    with session_scope() as s:
        rows = s.query(KnowledgeGraphHistory).filter_by(
            ticker="NFLX", pattern="pullback",
            snapshot_date=custom_date,
        ).all()
        assert len(rows) >= 1
