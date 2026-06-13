"""Stage-16 — forward-outcome backfill + research-digest push tests."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.bot.regime_similarity.backfill import (
    backfill_forward_outcomes,
)
from backend.bot.scheduler import BotScheduler
from backend.bot.state import build_market_state, reset_latest, set_latest


@pytest.fixture(autouse=True)
def _isolate():
    reset_latest()
    yield
    reset_latest()


def _seed_snapshot(*, ts_offset_min=0, trend="bullish"):
    from backend.db import session_scope
    from backend.models.regime_episode import RegimeEpisodeSnapshot
    with session_scope() as s:
        r = RegimeEpisodeSnapshot(
            trend=trend, trend_phase="neutral", volatility="normal",
            vol_phase="neutral", gamma="long_gamma", risk="neutral",
            equities="risk_on", yields="rising", dollar="neutral",
            label=f"{trend} test", vix=15, iv_rank=40,
            breadth_score=0.5, sentiment_score=0.3, sector_strength=0.4,
        )
        r.timestamp = datetime.utcnow() - timedelta(minutes=ts_offset_min)
        s.add(r); s.flush()
        return r.id


def _seed_trade_after_snapshot(*, snapshot_minutes_ago=30, pnl=100.0):
    """Insert a closed trade that fired right after a snapshot was captured."""
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        t = Trade(
            ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
            strategy="trend_pullback", signal_source="t",
            confidence=0.7, paper=1, status="closed", instrument="option",
            pnl=pnl,
        )
        # 30 minutes ago (after snapshot's 60-min-ago timestamp)
        t.timestamp = datetime.utcnow() - timedelta(minutes=snapshot_minutes_ago)
        s.add(t); s.flush()
        return t.id


def _seed_equity_curve():
    from backend.db import session_scope
    from backend.models.snapshot import PortfolioSnapshot
    with session_scope() as s:
        for d in range(-2, 6):
            row = PortfolioSnapshot(portfolio_value=10000.0 + d * 100)
            row.timestamp = datetime.utcnow() + timedelta(days=d)
            s.add(row)


# ── backfill ────────────────────────────────────────────────────────────


class TestBackfill:
    def test_no_data_no_crash(self, temp_db):
        out = backfill_forward_outcomes()
        assert out["snapshots_scanned"] == 0
        assert out["snapshots_updated"] == 0

    def test_credits_trade_inside_window(self, temp_db):
        # Snapshot taken 60 min ago, trade fired 30 min ago → trade is
        # inside the [snap_ts, snap_ts + 60min] credit window.
        _seed_snapshot(ts_offset_min=60)
        _seed_trade_after_snapshot(snapshot_minutes_ago=30, pnl=120.0)
        out = backfill_forward_outcomes(credit_window_min=60)
        assert out["snapshots_updated"] == 1
        # Confirm the row got the trade attributed
        from backend.db import session_scope
        from backend.models.regime_episode import RegimeEpisodeSnapshot
        with session_scope() as s:
            rows = s.query(RegimeEpisodeSnapshot).all()
            assert rows[0].fwd_trades_count == 1
            assert rows[0].fwd_trades_wins == 1
            assert rows[0].fwd_trades_pnl == 120.0

    def test_idempotent(self, temp_db):
        _seed_snapshot(ts_offset_min=60)
        _seed_trade_after_snapshot(snapshot_minutes_ago=30, pnl=120.0)
        first = backfill_forward_outcomes()
        assert first["snapshots_updated"] == 1
        # Second run: same data → 0 dirty updates
        second = backfill_forward_outcomes()
        assert second["snapshots_updated"] == 0

    def test_equity_curve_fills_fwd_returns(self, temp_db):
        _seed_snapshot(ts_offset_min=60)
        _seed_equity_curve()
        out = backfill_forward_outcomes()
        from backend.db import session_scope
        from backend.models.regime_episode import RegimeEpisodeSnapshot
        with session_scope() as s:
            rows = s.query(RegimeEpisodeSnapshot).all()
            assert rows[0].fwd_1d_return is not None


# ── scheduler job wiring ────────────────────────────────────────────────


def _make_scheduler():
    return BotScheduler(MagicMock())


class TestJobRegistration:
    def test_configure_registers_backfill(self):
        sch = _make_scheduler()
        sch.configure()
        names = [str(j.func) for j in sch.scheduler.get_jobs()]
        assert any("_regime_backfill" in n for n in names)

    def test_configure_registers_research_digest(self):
        sch = _make_scheduler()
        sch.configure()
        names = [str(j.func) for j in sch.scheduler.get_jobs()]
        assert any("_research_digest" in n for n in names)


class TestResearchDigestJob:
    def test_noop_on_non_trading_day(self, temp_db):
        sch = _make_scheduler()
        with patch("backend.bot.scheduler.is_trading_day", return_value=False):
            # Should silently skip without ever calling generate_digest
            sch._research_digest()

    def test_does_not_crash_on_empty_findings(self, temp_db):
        sch = _make_scheduler()
        with patch("backend.bot.scheduler.is_trading_day", return_value=True):
            # No data → empty findings → silently exit
            sch._research_digest()

    def test_fires_alert_when_findings_present(self, temp_db):
        from backend.bot.alerts import ALERT_CENTER, Alert
        from backend.bot import research as research_mod

        sch = _make_scheduler()
        # Patch generate_digest to return a digest with one finding
        fake_finding = research_mod.Finding(
            area="agents", title="market hit-rate degrading",
            detail="recent 30% vs baseline 60%", severity="alert",
        )
        fake_digest = research_mod.ResearchDigest(
            generated_at="2026-05-30T18:00:00",
            findings=[fake_finding],
            counts={"info": 0, "warn": 0, "alert": 1},
        )

        before = len(ALERT_CENTER.history)
        with patch("backend.bot.scheduler.is_trading_day", return_value=True), \
                patch.object(research_mod, "generate_digest", return_value=fake_digest):
            sch._research_digest()
        # New alert appeared
        assert len(ALERT_CENTER.history) == before + 1
        latest = ALERT_CENTER.history[-1]
        assert latest.severity == "danger"  # alert → danger mapping
        assert "Research digest" in latest.title

    def test_backfill_job_safe_on_non_trading_day(self, temp_db):
        sch = _make_scheduler()
        with patch("backend.bot.scheduler.is_trading_day", return_value=False):
            sch._regime_backfill()      # should silently no-op
