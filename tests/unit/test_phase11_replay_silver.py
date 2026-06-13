"""MITS Phase 11.H — unit tests for ``replay_from_silver``.

Covers:
  * Synthetic stock_bars → detector replay produces observations.
  * Idempotent re-run is a no-op.
  * Intraday-only detectors are skipped when the intraday watermark
    hasn't reached the audit window.
  * Empty silver layer returns a 0-row summary cleanly.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import delete, select, func

from backend.db import init_db, session_scope


@pytest.fixture(autouse=True)
def _init_db_once(tmp_path, monkeypatch):
    # Use a per-test temp DB so the test never touches the operator's
    # live SQLite.
    db = tmp_path / "test_phase11_replay.db"
    monkeypatch.setattr("backend.config.SETTINGS.db_path", str(db))
    init_db(str(db))
    yield


def _seed_synthetic_daily_bars(ticker: str, n: int = 150, seed: int = 11):
    """Insert N daily silver bars for ``ticker`` starting 2025-01-01."""
    from backend.models.stock_bar import StockBar
    random.seed(seed)
    px = 100.0
    base = datetime(2025, 1, 1)
    with session_scope() as s:
        s.execute(delete(StockBar).where(StockBar.ticker == ticker))
        for i in range(n):
            d = base + timedelta(days=i)
            px *= 1.0 + random.uniform(-0.025, 0.025)
            hi = px * (1.0 + abs(random.uniform(-0.012, 0.012)))
            lo = px * (1.0 - abs(random.uniform(-0.012, 0.012)))
            s.add(StockBar(
                ticker=ticker, interval="1d", bar_ts=d,
                open=px * 0.999, high=hi, low=lo, close=px,
                volume=1_000_000 + random.randint(0, 250_000),
                source="synthetic",
            ))


def test_replay_emits_observations_from_silver():
    from backend.bot.corpus.replay_from_silver import replay_ticker
    from backend.models.market_observation import MarketObservation

    _seed_synthetic_daily_bars("TST_A", n=150)
    summary = replay_ticker(
        "TST_A",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
    )
    assert summary.bars_read == 150
    assert summary.observations_emitted > 0
    assert summary.observations_inserted > 0
    with session_scope() as s:
        n = s.execute(
            select(func.count(MarketObservation.id))
            .where(MarketObservation.ticker == "TST_A")
        ).scalar_one()
        assert n == summary.observations_inserted


def test_replay_idempotent_on_rerun():
    from backend.bot.corpus.replay_from_silver import replay_ticker

    _seed_synthetic_daily_bars("TST_B", n=80, seed=7)
    first = replay_ticker(
        "TST_B",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
    )
    second = replay_ticker(
        "TST_B",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
    )
    assert first.observations_inserted > 0
    assert second.observations_inserted == 0
    assert second.observations_skipped == first.observations_emitted


def test_replay_skips_intraday_only_when_watermark_lags():
    """Without an intraday watermark, intraday-only families (VWAP,
    flow_intel) should be filtered out — the rest of the corpus is
    intact."""
    from backend.bot.corpus.replay_from_silver import replay_ticker

    _seed_synthetic_daily_bars("TST_C", n=120, seed=21)
    summary = replay_ticker(
        "TST_C",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
    )
    vwap_count = sum(v for k, v in summary.detector_counts.items()
                          if "vwap" in k.lower())
    assert vwap_count == 0


def test_replay_returns_zero_when_silver_empty():
    from backend.bot.corpus.replay_from_silver import replay_ticker

    summary = replay_ticker(
        "EMPTY_TKR",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
    )
    assert summary.bars_read == 0
    assert summary.observations_emitted == 0


def test_replay_universe_aggregates_per_detector():
    from backend.bot.corpus.replay_from_silver import replay_universe

    _seed_synthetic_daily_bars("TST_D", n=80, seed=11)
    _seed_synthetic_daily_bars("TST_E", n=80, seed=12)
    grand = replay_universe(
        ["TST_D", "TST_E"],
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 1),
    )
    assert grand["tickers"] == 2
    assert grand["bars_read"] == 160
    assert grand["observations_inserted"] > 0
    assert isinstance(grand["per_detector"], dict)
    # At least the talib_doji or consolidation should fire on synthetic noise.
    assert any(v > 0 for v in grand["per_detector"].values())
