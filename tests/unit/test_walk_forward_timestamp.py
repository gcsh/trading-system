"""MITS Phase 2 (P2.5) — walk-forward TIMESTAMP-based splitting.

Locks the refinement contract:
  * `first_live_observation_at` is computed from MIN(timestamp)
    where source='live_engine'.
  * Observations BEFORE that cutoff classify as in_sample regardless
    of source.
  * Observations on/after classify as out_of_sample.
  * Tickers with no live observations fall back to source-based
    splitting (Phase 1 behaviour preserved).
"""
import json
from datetime import datetime, timedelta

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "walk_forward_test.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import backend.db as _dbmod
    _dbmod._engine = None
    _dbmod._SessionLocal = None
    from backend.db import init_db
    init_db(str(db_path))
    yield
    _dbmod._engine = None
    _dbmod._SessionLocal = None


def _seed_observations(ticker: str, pattern: str, *,
                            historical_count: int, live_count: int,
                            historical_window_start: datetime,
                            live_window_start: datetime,
                            wins_historical: int = None,
                            wins_live: int = None):
    """Seed historical_replay + live_engine observations + outcomes.

    Critically, EVERY observation uses source='live_engine' for the
    `live_count` window to ensure the TIMESTAMP cutoff (not the source
    label) is what splits them. The historical_replay-sourced rows in
    the BEFORE-cutoff window confirm the timestamp wins over source.
    """
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.market_outcome import MarketOutcome

    wins_h = wins_historical if wins_historical is not None else historical_count
    wins_l = wins_live if wins_live is not None else live_count

    with session_scope() as s:
        # Historical window — all `historical_replay` source.
        for i in range(historical_count):
            ts = historical_window_start + timedelta(days=i)
            obs = MarketObservation(
                ticker=ticker, pattern=pattern, timestamp=ts,
                timeframe="1d", regime="trending_up",
                vol_state="normal", time_bucket="rth",
                spot=100.0, features=json.dumps({}),
                source="historical_replay",
            )
            s.add(obs)
            s.flush()
            s.add(MarketOutcome(
                observation_id=obs.id, horizon="1d",
                entry_price=100.0,
                exit_price=(103.0 if i < wins_h else 97.0),
                return_pct=(0.03 if i < wins_h else -0.03),
                was_winner=(i < wins_h),
            ))
        # Live window — all `live_engine` source.
        for i in range(live_count):
            ts = live_window_start + timedelta(days=i)
            obs = MarketObservation(
                ticker=ticker, pattern=pattern, timestamp=ts,
                timeframe="1d", regime="trending_up",
                vol_state="normal", time_bucket="rth",
                spot=100.0, features=json.dumps({}),
                source="live_engine",
            )
            s.add(obs)
            s.flush()
            s.add(MarketOutcome(
                observation_id=obs.id, horizon="1d",
                entry_price=100.0,
                exit_price=(103.0 if i < wins_l else 97.0),
                return_pct=(0.03 if i < wins_l else -0.03),
                was_winner=(i < wins_l),
            ))


class TestFirstLiveCutoff:
    def test_compute_first_live_per_ticker(self):
        from backend.bot.corpus.knowledge_aggregator import (
            _compute_first_live_per_ticker,
        )
        hist_start = datetime(2024, 1, 1)
        live_start = datetime(2025, 1, 1)
        _seed_observations(
            "AAA", "bull_flag",
            historical_count=10, live_count=5,
            historical_window_start=hist_start,
            live_window_start=live_start,
        )
        cutoffs = _compute_first_live_per_ticker("AAA")
        assert "AAA" in cutoffs
        assert cutoffs["AAA"] == live_start

    def test_persists_cutoff_onto_corpus_status(self):
        from backend.bot.corpus.knowledge_aggregator import (
            _compute_first_live_per_ticker,
        )
        from backend.db import session_scope
        from backend.models.corpus_status import CorpusStatus
        from sqlalchemy import select

        hist_start = datetime(2024, 5, 1)
        live_start = datetime(2025, 6, 1)
        _seed_observations(
            "BBB", "breakout",
            historical_count=8, live_count=4,
            historical_window_start=hist_start,
            live_window_start=live_start,
        )
        _compute_first_live_per_ticker("BBB")
        with session_scope() as s:
            row = s.execute(
                select(CorpusStatus).where(CorpusStatus.ticker == "BBB")
            ).scalar_one_or_none()
            assert row is not None
            assert row.first_live_observation_at == live_start


class TestTimestampBasedSplitting:
    def test_recompute_produces_three_splits(self):
        from backend.bot.corpus.knowledge_aggregator import recompute_cells

        hist_start = datetime(2024, 1, 1)
        live_start = datetime(2025, 1, 1)
        _seed_observations(
            "CCC", "consolidation",
            historical_count=10, live_count=5,
            historical_window_start=hist_start,
            live_window_start=live_start,
            wins_historical=7, wins_live=3,
        )
        stats = recompute_cells("CCC")
        assert stats["in_sample_cells"] >= 1
        assert stats["out_of_sample_cells"] >= 1
        assert stats["combined_cells"] >= 1

    def test_in_sample_uses_only_pre_cutoff_observations(self):
        # Seed: historical window pre-cutoff (10 obs, 7 wins),
        #       live window post-cutoff (5 obs, 3 wins).
        # in_sample should reflect 10/7 = 70% WR (pre-cutoff only),
        # out_of_sample 5/3 = 60% WR (post-cutoff only).
        from backend.bot.corpus.knowledge_aggregator import recompute_cells
        from backend.db import session_scope
        from backend.models.knowledge_graph_cell import KnowledgeGraphCell
        from sqlalchemy import select

        hist_start = datetime(2024, 1, 1)
        live_start = datetime(2025, 1, 1)
        _seed_observations(
            "DDD", "bear_flag",
            historical_count=10, live_count=5,
            historical_window_start=hist_start,
            live_window_start=live_start,
            wins_historical=7, wins_live=3,
        )
        recompute_cells("DDD")

        with session_scope() as s:
            rows = s.execute(
                select(KnowledgeGraphCell)
                .where(KnowledgeGraphCell.ticker == "DDD")
                .where(KnowledgeGraphCell.pattern == "bear_flag")
            ).scalars().all()
            by_split = {r.sample_split: r.to_dict() for r in rows}
        assert "in_sample" in by_split
        assert "out_of_sample" in by_split
        assert by_split["in_sample"]["sample_size"] == 10
        assert by_split["in_sample"]["win_rate"] == pytest.approx(0.70, abs=1e-3)
        assert by_split["out_of_sample"]["sample_size"] == 5
        assert by_split["out_of_sample"]["win_rate"] == pytest.approx(0.60, abs=1e-3)

    def test_no_live_observations_falls_back_to_source_split(self):
        # No live observations → first_live_observation_at is None →
        # source-based split (Phase 1). All historical_replay obs land
        # in in_sample. No out_of_sample cell.
        from backend.bot.corpus.knowledge_aggregator import recompute_cells
        from backend.db import session_scope
        from backend.models.knowledge_graph_cell import KnowledgeGraphCell
        from sqlalchemy import select

        hist_start = datetime(2024, 1, 1)
        _seed_observations(
            "EEE", "pullback",
            historical_count=20, live_count=0,
            historical_window_start=hist_start,
            live_window_start=hist_start,  # unused
            wins_historical=15,
        )
        recompute_cells("EEE")
        with session_scope() as s:
            rows = s.execute(
                select(KnowledgeGraphCell.sample_split)
                .where(KnowledgeGraphCell.ticker == "EEE")
            ).all()
            splits = {r[0] for r in rows}
        assert "in_sample" in splits
        assert "out_of_sample" not in splits
        assert "combined" in splits
