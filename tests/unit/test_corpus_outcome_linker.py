"""MITS Phase 0 — outcome linker test."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from backend.bot.corpus.outcome_linker import (
    DAILY_HORIZONS, INTRADAY_HORIZONS, link_outcomes_batch,
)
from backend.db import init_db, session_scope
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


def _seed_observation(ts: datetime, timeframe: str = "1d",
                            ticker: str = "TEST") -> int:
    with session_scope() as s:
        row = MarketObservation(
            ticker=ticker, pattern="bull_flag", timestamp=ts,
            timeframe=timeframe, spot=100.0,
        )
        s.add(row)
        s.flush()
        return row.id


def _synthetic_daily_bars(start: datetime, n: int = 50,
                                base: float = 100.0,
                                up_pct_per_bar: float = 0.01) -> pd.DataFrame:
    closes = [base * (1 + up_pct_per_bar) ** i for i in range(n)]
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def test_links_outcomes_for_daily_observation(fresh_db):
    obs_ts = datetime(2025, 1, 5)
    obs_id = _seed_observation(obs_ts)
    bars = _synthetic_daily_bars(datetime(2024, 12, 31), n=40, up_pct_per_bar=0.01)

    with patch("backend.bot.corpus.outcome_linker._fetch_bars_for_outcome",
                  return_value=bars):
        stats = link_outcomes_batch(ticker="TEST")
    assert stats["outcomes_inserted"] >= 1
    with session_scope() as s:
        outcomes = s.query(MarketOutcome).filter_by(observation_id=obs_id).all()
        assert len(outcomes) >= 1
        horizons = {o.horizon for o in outcomes}
        # At least 1d should be reachable from a 40-bar series.
        assert "1d" in horizons
        # 20d won't fit (we have ~35 forward bars from idx 5).
        for o in outcomes:
            assert o.entry_price is not None
            assert o.return_pct is not None
            # Returns should be positive given the uptrend.
            assert o.return_pct > 0


def test_linker_is_idempotent(fresh_db):
    obs_ts = datetime(2025, 1, 5)
    obs_id = _seed_observation(obs_ts)
    bars = _synthetic_daily_bars(datetime(2024, 12, 31), n=40)
    with patch("backend.bot.corpus.outcome_linker._fetch_bars_for_outcome",
                  return_value=bars):
        link_outcomes_batch(ticker="TEST")
        first_count = len(_outcomes_for(obs_id))
        link_outcomes_batch(ticker="TEST")
        second_count = len(_outcomes_for(obs_id))
    assert first_count == second_count, "linker must not duplicate"


def test_intraday_observation_uses_intraday_horizons(fresh_db):
    obs_ts = datetime(2025, 1, 5, 10, 0)
    obs_id = _seed_observation(obs_ts, timeframe="1h")
    # Build intraday-spaced bars.
    n = 30
    idx = pd.date_range(obs_ts - timedelta(hours=1), periods=n, freq="h")
    closes = [100 + i * 0.5 for i in range(n)]
    bars = pd.DataFrame({
        "open": closes, "high": [c * 1.002 for c in closes],
        "low": [c * 0.998 for c in closes], "close": closes,
        "volume": [100_000] * n,
    }, index=idx)
    with patch("backend.bot.corpus.outcome_linker._fetch_bars_for_outcome",
                  return_value=bars):
        link_outcomes_batch(ticker="TEST")
    with session_scope() as s:
        outcomes = s.query(MarketOutcome).filter_by(observation_id=obs_id).all()
        horizons = {o.horizon for o in outcomes}
        # Intraday horizon set: 5min/30min/60min.
        for h in horizons:
            assert h in INTRADAY_HORIZONS


def _outcomes_for(obs_id: int):
    with session_scope() as s:
        return s.query(MarketOutcome).filter_by(observation_id=obs_id).all()
