"""MITS Phase 3 — EOD analysis pipeline tests (mocked Claude + watchlist)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from unittest.mock import patch

import pandas as pd
import pytest

from backend.bot import eod_analysis as eod_mod
from backend.db import init_db, session_scope
from backend.models.eod_analysis import EodAnalysis
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.watchlist import WatchlistItem


pytestmark = [pytest.mark.unit]


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
        yield
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _fake_intraday_df():
    idx = pd.date_range("2026-06-04 09:30", periods=40, freq="5min")
    return pd.DataFrame({
        "open": [100.0] * 40,
        "high": [101.0] * 40,
        "low": [99.0] * 40,
        "close": [100.5] * 40,
        "volume": [1000.0] * 40,
    }, index=idx)


def _seed_cohort(ticker, pattern, sample_size, posterior):
    """Seed at horizon=5d to match the EOD cohort lookup (since Phase
    12.2 the EOD pass queries the 5d horizon)."""
    with session_scope() as s:
        s.add(KnowledgeGraphCell(
            ticker=ticker, pattern=pattern, regime="trending_up",
            vol_state="normal", time_bucket="rth", horizon="5d",
            sample_size=sample_size, win_rate=posterior,
            posterior_win_rate=posterior, avg_return_pct=0.02,
            avg_hold_minutes=60.0, confidence_lower=posterior - 0.05,
            confidence_upper=posterior + 0.05, sample_split="combined",
        ))


class _FakeObs:
    def __init__(self, pattern):
        self.pattern = pattern


def test_rank_score_rewards_samples_and_posterior():
    high = eod_mod._rank_score(0.70, 500)
    low = eod_mod._rank_score(0.95, 5)
    assert high > low


def test_pick_top_patterns_orders_by_rank():
    cohorts = {
        "bull_flag": {"posterior_win_rate": 0.70, "sample_size": 500},
        "bear_flag": {"posterior_win_rate": 0.55, "sample_size": 50},
        "breakout":  {"posterior_win_rate": 0.65, "sample_size": 300},
    }
    top = eod_mod._pick_top_patterns(cohorts, top_n=3)
    assert [t[0] for t in top] == ["bull_flag", "breakout", "bear_flag"]


def test_suggested_action_gating():
    high = eod_mod._suggested_action(
        "bull_flag", {"posterior_win_rate": 0.70, "sample_size": 100},
        "NVDA", spot=470,
    )
    low_post = eod_mod._suggested_action(
        "bull_flag", {"posterior_win_rate": 0.45, "sample_size": 100},
        "NVDA", spot=470,
    )
    low_n = eod_mod._suggested_action(
        "bull_flag", {"posterior_win_rate": 0.80, "sample_size": 5},
        "NVDA", spot=470,
    )
    assert isinstance(high, dict)
    assert high["action"] == "BUY_CALL"
    assert high["strike"] is not None
    assert low_post is None
    assert low_n is None


def test_run_eod_pass_persists_rows(fresh_db):
    """End-to-end: a small watchlist + cohort seed produces EodAnalysis rows."""
    with session_scope() as s:
        s.add(WatchlistItem(ticker="NVDA"))
    _seed_cohort("NVDA", "bull_flag", sample_size=400, posterior=0.71)
    _seed_cohort("SPY", "breakout", sample_size=300, posterior=0.65)

    with patch.object(eod_mod, "_fetch_intraday_df",
                            return_value=_fake_intraday_df()), \
            patch.object(eod_mod, "_fetch_daily_df",
                              return_value=_fake_intraday_df()), \
            patch.object(eod_mod, "detect_all",
                              return_value=[_FakeObs("bull_flag"),
                                                _FakeObs("pullback")]), \
            patch.object(eod_mod, "_compose_thesis", return_value={
                "headline": "Bull Flag on NVDA — 71%",
                "thesis_paragraph": "Cohort posterior is healthy at 71%.",
                "suggested_action": {"action": "BUY_CALL", "strike": 470,
                                              "dte": 14,
                                              "target_premium_pct": 50,
                                              "stop_premium_pct": 30,
                                              "rationale": "x"},
                "invalidation": ["loses VWAP"],
            }):
        stats = eod_mod.run_eod_pass(date=date(2026, 6, 5),
                                              tickers=["NVDA", "SPY"])

    assert stats["tickers_analyzed"] == 2
    assert stats["rows_inserted"] >= 1
    with session_scope() as s:
        rows = s.query(EodAnalysis).all()
        assert len(rows) == 2
        nv = next(r for r in rows if r.ticker == "NVDA")
        assert nv.top_pattern == "bull_flag"
        assert nv.top_posterior == 0.71
        sa = json.loads(nv.suggested_action_json)
        assert sa["action"] == "BUY_CALL"


def test_run_eod_pass_idempotent(fresh_db):
    """Re-running the same day overwrites the row, doesn't dup."""
    with session_scope() as s:
        s.add(WatchlistItem(ticker="NVDA"))
    _seed_cohort("NVDA", "bull_flag", sample_size=400, posterior=0.71)
    with patch.object(eod_mod, "_fetch_intraday_df",
                            return_value=_fake_intraday_df()), \
            patch.object(eod_mod, "_fetch_daily_df",
                              return_value=_fake_intraday_df()), \
            patch.object(eod_mod, "detect_all",
                              return_value=[_FakeObs("bull_flag")]), \
            patch.object(eod_mod, "_compose_thesis", return_value={
                "headline": "h", "thesis_paragraph": "p",
                "suggested_action": None, "invalidation": [],
            }):
        eod_mod.run_eod_pass(date=date(2026, 6, 5), tickers=["NVDA"])
        eod_mod.run_eod_pass(date=date(2026, 6, 5), tickers=["NVDA"])
    with session_scope() as s:
        rows = s.query(EodAnalysis).filter_by(
            ticker="NVDA", analysis_date=date(2026, 6, 5)
        ).all()
        assert len(rows) == 1


def test_format_tomorrow_digest_text(fresh_db):
    with session_scope() as s:
        s.add(EodAnalysis(
            ticker="NVDA", analysis_date=date(2026, 6, 5),
            patterns_fired=json.dumps(["bull_flag"]),
            top_pattern="bull_flag", top_posterior=0.71, top_sample_size=400,
            headline="Bull Flag on NVDA",
            thesis_paragraph="thesis text",
            suggested_action_json=json.dumps({
                "action": "BUY_CALL", "strike": 470, "dte": 14,
                "target_premium_pct": 50, "stop_premium_pct": 30,
            }),
            invalidation_json=json.dumps(["VWAP break"]),
            rank_score=4.32,
        ))
    text = eod_mod.format_tomorrow_digest_text(
        analysis_date=date(2026, 6, 5), limit=3,
    )
    assert text is not None
    assert "NVDA" in text
    assert "bull_flag" in text
    assert "71%" in text
    assert "BUY_CALL" in text


def test_format_tomorrow_digest_empty_returns_none(fresh_db):
    assert eod_mod.format_tomorrow_digest_text(
        analysis_date=date(2026, 1, 1)
    ) is None
