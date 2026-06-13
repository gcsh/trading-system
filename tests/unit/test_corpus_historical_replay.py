"""MITS Phase 0 — historical replay test (yfinance mocked)."""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

from backend.bot.corpus.historical_replay import bootstrap_ticker
from backend.db import init_db, session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.market_observation import MarketObservation


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


def _synthetic_daily(n: int = 250) -> pd.DataFrame:
    """A semi-realistic daily OHLCV frame: trending up with periodic
    pullbacks. Guaranteed to trigger several detectors so the replay
    test sees a non-trivial inserted count."""
    import math
    closes = []
    for i in range(n):
        trend = 100 + i * 0.5
        noise = 5 * math.sin(i / 7.0)
        closes.append(round(trend + noise, 2))
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000 + (i % 7) * 100_000 for i in range(n)],
    }, index=idx)


def test_bootstrap_persists_observations(fresh_db):
    bars = _synthetic_daily(250)
    stats = bootstrap_ticker("TEST", bars_daily=bars, bars_intraday=None)
    assert stats["status"] in {"ready", "insufficient"}
    assert stats["daily"]["bars"] == 250
    with session_scope() as s:
        n_obs = s.query(MarketObservation).filter_by(ticker="TEST").count()
        # With 250 trending bars + TA-Lib + 8 price-action + structure +
        # liquidity + vol-profile detectors, expect at least dozens of
        # observations. Lower bound is conservative.
        assert n_obs >= 10
        status_row = s.query(CorpusStatus).filter_by(ticker="TEST").first()
        assert status_row is not None
        assert status_row.status in {"ready", "insufficient"}
        assert status_row.observation_count == n_obs


def test_bootstrap_idempotent(fresh_db):
    bars = _synthetic_daily(200)
    bootstrap_ticker("IDEM", bars_daily=bars, bars_intraday=None)
    with session_scope() as s:
        first_count = s.query(MarketObservation).filter_by(ticker="IDEM").count()
    # Re-run with the same bars — total should not double.
    bootstrap_ticker("IDEM", bars_daily=bars, bars_intraday=None)
    with session_scope() as s:
        second_count = s.query(MarketObservation).filter_by(ticker="IDEM").count()
    assert second_count == first_count, "replay must skip duplicates"


def test_bootstrap_handles_empty_bars(fresh_db):
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    stats = bootstrap_ticker("EMPTY", bars_daily=empty, bars_intraday=empty)
    assert stats["status"] in {"error", "insufficient"}
    with session_scope() as s:
        assert s.query(MarketObservation).filter_by(ticker="EMPTY").count() == 0


def test_corpus_status_row_created(fresh_db):
    bars = _synthetic_daily(100)
    bootstrap_ticker("STATUS", bars_daily=bars, bars_intraday=None)
    with session_scope() as s:
        row = s.query(CorpusStatus).filter_by(ticker="STATUS").first()
        assert row is not None
        assert row.last_built_at is not None or row.status == "error"


# ── MITS Phase 1 — IV / GEX intraday wiring (Task C) ──


def test_iv_expansion_fires_via_replay_with_iv_series(fresh_db):
    """Feed a synthetic IV series with an obvious expansion through the
    replay path. IVExpansion observations should persist.

    This validates Task C wiring: bootstrap_ticker forwards iv_series into
    detect_all, which forwards into the options-intel detectors.
    """
    bars = _synthetic_daily(60)
    # 30 bars at IV 0.20 baseline, then a sharp jump to 0.45 for the
    # remaining 30. The 20-bar trailing window will detect the cross.
    iv_series = [0.20] * 30 + [0.45] * 30
    stats = bootstrap_ticker(
        "IVEXP", bars_daily=bars, bars_intraday=None,
        iv_series_daily=iv_series,
    )
    assert stats["daily"].get("iv_aligned") is True
    with session_scope() as s:
        n_iv_exp = s.query(MarketObservation).filter_by(
            ticker="IVEXP", pattern="iv_expansion").count()
        assert n_iv_exp >= 1, (
            "IVExpansion must fire when IV jumps from 0.20 baseline to 0.45")


def test_intraday_inherits_iv_when_supplied(fresh_db):
    """The intraday replay path also accepts iv_series_intraday.
    Carry-forward behaviour is the documented degraded mode."""
    intraday = _synthetic_daily(50)
    # Force the intraday spacing.
    intraday.index = pd.date_range("2024-06-01 09:30",
                                                periods=len(intraday), freq="1h")
    iv_intra = [0.18] * 25 + [0.40] * 25
    stats = bootstrap_ticker(
        "IVINTRA", bars_daily=None, bars_intraday=intraday,
        iv_series_intraday=iv_intra,
    )
    assert stats["intraday"].get("iv_aligned") is True
    with session_scope() as s:
        n_iv = s.query(MarketObservation).filter_by(
            ticker="IVINTRA", pattern="iv_expansion").count()
        assert n_iv >= 1


def test_replay_no_iv_series_no_iv_observations(fresh_db):
    """Without an IV series, options-intel detectors gracefully no-op."""
    bars = _synthetic_daily(60)
    bootstrap_ticker("NOIV", bars_daily=bars, bars_intraday=None)
    with session_scope() as s:
        n_iv = s.query(MarketObservation).filter_by(
            ticker="NOIV", pattern="iv_expansion").count()
        assert n_iv == 0
