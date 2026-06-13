"""MITS Phase 11.J — unit tests for the parity audit.

Covers:
  * No silver bars → audit is a no-op.
  * Mismatched yfinance vs ThetaData closes are flagged correctly.
  * Suspect-day MarketObservations get parity_warn=True.
  * Idempotent UPSERT on re-run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from backend.db import init_db, session_scope


@pytest.fixture(autouse=True)
def _init_db_once(tmp_path, monkeypatch):
    db = tmp_path / "test_phase11_parity.db"
    monkeypatch.setattr("backend.config.SETTINGS.db_path", str(db))
    init_db(str(db))
    yield


def _seed_silver(ticker: str, closes: dict):
    from backend.models.stock_bar import StockBar
    with session_scope() as s:
        s.execute(delete(StockBar).where(StockBar.ticker == ticker))
        for d, c in closes.items():
            s.add(StockBar(
                ticker=ticker, interval="1d",
                bar_ts=datetime.combine(d, datetime.min.time()),
                open=c, high=c, low=c, close=c, volume=100,
                source="thetadata",
            ))


def _seed_observation(ticker: str, day: date, pattern: str = "bull_flag"):
    from backend.models.market_observation import MarketObservation
    with session_scope() as s:
        s.add(MarketObservation(
            ticker=ticker, pattern=pattern,
            timestamp=datetime.combine(day, datetime.min.time()),
            timeframe="1d", regime="trending_up",
            vol_state="normal", time_bucket="rth",
            spot=100.0, source="historical_replay",
        ))


def test_audit_no_op_when_silver_empty():
    from backend.bot.corpus.parity_audit import audit_ticker
    with patch(
        "backend.bot.corpus.parity_audit._fetch_yfinance_closes",
        return_value={},
    ):
        stats = audit_ticker(
            "ZZZ", start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 5),
        )
    assert stats["rows_audited"] == 0
    assert stats["theta_dates"] == 0


def test_audit_flags_suspect_divergence():
    """Inject 1% yfinance divergence vs ThetaData → suspect severity,
    obs flagged with parity_warn=True."""
    from backend.bot.corpus.parity_audit import audit_ticker
    from backend.models.market_observation import MarketObservation
    from backend.models.parity_audit_history import ParityAuditHistory

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    theta = {d: 100.0 for d in days}
    yfin = {d: 103.0 for d in days}  # 3% divergence → suspect
    _seed_silver("FOO", theta)
    _seed_observation("FOO", days[0])
    _seed_observation("FOO", days[1])

    with patch(
        "backend.bot.corpus.parity_audit._fetch_yfinance_closes",
        return_value=yfin,
    ):
        stats = audit_ticker(
            "FOO", start_date=days[0], end_date=days[-1],
        )

    assert stats["suspect_dates"] == 3
    assert stats["obs_flagged"] == 2  # 2 observations on suspect days

    with session_scope() as s:
        rows = s.execute(
            select(ParityAuditHistory)
            .where(ParityAuditHistory.ticker == "FOO")
        ).scalars().all()
        assert len(rows) == 3
        for r in rows:
            assert r.severity == "suspect"
            assert r.divergence_pct is not None
            assert 0.02 < r.divergence_pct < 0.05

        # Both observations should be flagged.
        flagged = s.execute(
            select(MarketObservation.parity_warn)
            .where(MarketObservation.ticker == "FOO")
        ).scalars().all()
        assert all(bool(f) for f in flagged)


def test_audit_classifies_ok_divergence():
    """0.1% divergence is below warn threshold (0.5%) → severity 'ok'."""
    from backend.bot.corpus.parity_audit import audit_ticker
    from backend.models.parity_audit_history import ParityAuditHistory

    days = [date(2025, 2, 1), date(2025, 2, 2)]
    theta = {d: 200.0 for d in days}
    yfin = {d: 200.2 for d in days}  # 0.1% — OK
    _seed_silver("BAR", theta)
    with patch(
        "backend.bot.corpus.parity_audit._fetch_yfinance_closes",
        return_value=yfin,
    ):
        stats = audit_ticker(
            "BAR", start_date=days[0], end_date=days[-1],
        )
    assert stats["suspect_dates"] == 0
    assert stats["warn_dates"] == 0
    assert stats["ok_dates"] == 2
    with session_scope() as s:
        rows = s.execute(
            select(ParityAuditHistory)
            .where(ParityAuditHistory.ticker == "BAR")
        ).scalars().all()
        assert all(r.severity == "ok" for r in rows)


def test_audit_idempotent_on_rerun():
    """Second run produces 0 new rows; severity is preserved/refreshed."""
    from backend.bot.corpus.parity_audit import audit_ticker
    from backend.models.parity_audit_history import ParityAuditHistory

    days = [date(2025, 3, 3), date(2025, 3, 4)]
    theta = {d: 50.0 for d in days}
    yfin = {d: 51.0 for d in days}  # 2% → suspect
    _seed_silver("BAZ", theta)
    with patch(
        "backend.bot.corpus.parity_audit._fetch_yfinance_closes",
        return_value=yfin,
    ):
        stats_a = audit_ticker(
            "BAZ", start_date=days[0], end_date=days[-1],
        )
        stats_b = audit_ticker(
            "BAZ", start_date=days[0], end_date=days[-1],
        )
    assert stats_a["rows_inserted"] == 2
    assert stats_b["rows_inserted"] == 0
    assert stats_a["rows_audited"] == stats_b["rows_audited"] == 2
    with session_scope() as s:
        rows = s.execute(
            select(ParityAuditHistory)
            .where(ParityAuditHistory.ticker == "BAZ")
        ).scalars().all()
        assert len(rows) == 2  # idempotent — no duplicate rows
