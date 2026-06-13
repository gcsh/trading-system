"""MITS Phase 2 (P2.2) — GEX scalar column on GexRegimeHistory.

Locks the contract:
  * `_fetch_gex_series` returns a real per-bar series when
    `net_gex_scalar` is populated.
  * GEXAccelerationDetector fires on a synthetic spike in the scalar
    series.
  * Carry-forward gap-fill matches the IV-series semantics.
"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "gex_replay_test.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import backend.db as _dbmod
    _dbmod._engine = None
    _dbmod._SessionLocal = None
    from backend.db import init_db
    init_db(str(db_path))
    yield
    _dbmod._engine = None
    _dbmod._SessionLocal = None


def _seed_gex_rows(ticker: str, day_start: date, n: int,
                       scalars: list, dealer_regimes: list = None):
    from backend.db import session_scope
    from backend.models.gex_history import GexRegimeHistory
    with session_scope() as s:
        for i in range(n):
            ts = datetime.combine(day_start + timedelta(days=i),
                                          datetime.min.time())
            row = GexRegimeHistory(
                ticker=ticker,
                timestamp=ts,
                spot_price=100.0,
                call_wall=105.0, put_wall=95.0,
                gamma_flip=100.0,
                dealer_regime=(dealer_regimes[i]
                                    if dealer_regimes else "long_gamma"),
                net_gex_scalar=scalars[i],
            )
            s.add(row)


class TestFetchGexSeries:
    def test_returns_per_bar_series_for_populated_scalar(self):
        from backend.bot.corpus.historical_replay import _fetch_gex_series

        start = date(2026, 1, 1)
        scalars = [float(1e9 * i) for i in range(1, 11)]
        _seed_gex_rows("AAA", start, 10, scalars)

        bars = pd.DataFrame(
            {"close": [100.0] * 10},
            index=[datetime.combine(start + timedelta(days=i),
                                            datetime.min.time())
                       for i in range(10)],
        )
        series = _fetch_gex_series("AAA", bars)
        assert series is not None
        assert len(series) == 10
        assert series[0] == pytest.approx(1e9, abs=1.0)
        assert series[-1] == pytest.approx(10e9, abs=1.0)

    def test_carry_forward_fill_for_gaps(self):
        from backend.bot.corpus.historical_replay import _fetch_gex_series

        start = date(2026, 2, 1)
        # Only seed days 0 and 5 — others should carry day-0 then day-5.
        from backend.db import session_scope
        from backend.models.gex_history import GexRegimeHistory
        with session_scope() as s:
            for i in (0, 5):
                row = GexRegimeHistory(
                    ticker="BBB",
                    timestamp=datetime.combine(start + timedelta(days=i),
                                                       datetime.min.time()),
                    spot_price=100.0, gamma_flip=100.0,
                    dealer_regime="long_gamma",
                    net_gex_scalar=1e9 * (i + 1),
                )
                s.add(row)

        bars = pd.DataFrame(
            {"close": [100.0] * 10},
            index=[datetime.combine(start + timedelta(days=i),
                                            datetime.min.time())
                       for i in range(10)],
        )
        series = _fetch_gex_series("BBB", bars)
        assert series is not None
        # Days 0..4 should be 1e9, days 5..9 should be 6e9.
        assert series[0] == pytest.approx(1e9, abs=1.0)
        assert series[4] == pytest.approx(1e9, abs=1.0)
        assert series[5] == pytest.approx(6e9, abs=1.0)
        assert series[9] == pytest.approx(6e9, abs=1.0)

    def test_empty_history_returns_none(self):
        from backend.bot.corpus.historical_replay import _fetch_gex_series
        bars = pd.DataFrame(
            {"close": [100.0] * 5},
            index=[datetime(2026, 1, 1) + timedelta(days=i) for i in range(5)],
        )
        assert _fetch_gex_series("ZZZ_NO_DATA", bars) is None


class TestGEXAccelerationFiresOnReplay:
    def test_detector_fires_on_synthetic_spike(self):
        # Build a 60-bar daily series with a step change in GEX.
        from backend.bot.detectors.options_intel import GEXAccelerationDetector

        start = date(2026, 3, 1)
        scalars = [1.0e9] * 30 + [5.0e9] * 30  # large step at i=30
        _seed_gex_rows("CCC", start, 60, scalars,
                            dealer_regimes=(["long_gamma"] * 60))

        # Construct bars and pull the GEX series via the replay helper
        # (this confirms the integration path the bootstrapper uses).
        from backend.bot.corpus.historical_replay import _fetch_gex_series
        idx = [datetime.combine(start + timedelta(days=i),
                                       datetime.min.time())
                  for i in range(60)]
        bars = pd.DataFrame({
            "open": [100.0] * 60, "high": [101.0] * 60,
            "low": [99.0] * 60, "close": [100.0] * 60,
            "volume": [1_000_000] * 60,
        }, index=idx)
        series = _fetch_gex_series("CCC", bars)
        assert series is not None

        detector = GEXAccelerationDetector()
        observations = detector.detect("CCC", bars, gex_series=series)
        assert any(o.pattern == "gex_acceleration" for o in observations)
