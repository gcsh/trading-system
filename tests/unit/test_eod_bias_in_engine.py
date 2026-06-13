"""MITS Phase 5 (P5.1) — EOD bias loading + ticker promotion."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.bot.eod_bias import (
    EodBiasRow, load_eod_bias, priority_tickers_from_bias,
)
from backend.config import TUNABLES
from backend.db import init_db, session_scope
from backend.models.eod_analysis import EodAnalysis


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "eod_bias_test.db"
    monkeypatch.setattr(
        "backend.config.SETTINGS.db_path", str(db_file),
    )
    # Force a re-init pointed at the temp DB.
    import backend.db as db_mod
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(str(db_file))
    yield
    db_mod._engine = None
    db_mod._SessionLocal = None


def _seed(session, ticker, posterior, n, rank_score, suggested=None):
    row = EodAnalysis(
        ticker=ticker,
        analysis_date=datetime.utcnow().date(),
        patterns_fired=json.dumps([f"pat_{ticker.lower()}"]),
        top_pattern="bull_flag",
        top_posterior=posterior,
        top_sample_size=n,
        rank_score=rank_score,
        suggested_action_json=(
            json.dumps(suggested) if suggested else None
        ),
        headline=f"{ticker} headline",
    )
    session.add(row)
    return row


def test_high_conviction_promotes_into_priority(fresh_db):
    with session_scope() as s:
        _seed(s, "AAPL", 0.80, 120, 4.5,
              suggested={
                  "action": "BUY_CALL", "direction": "long_call",
                  "strike": 200.0, "dte": 30,
              })
        _seed(s, "TSLA", 0.55, 25, 2.0)
    bias = load_eod_bias()
    assert "AAPL" in bias
    assert bias["AAPL"].rank == 1
    assert bias["AAPL"].is_high_conviction()
    promo = priority_tickers_from_bias(bias)
    assert "AAPL" in promo
    assert "TSLA" not in promo


def test_lower_conviction_info_only(fresh_db):
    with session_scope() as s:
        # Posterior + N hits the info-only floor (>=0.55, >=30 by default)
        # but not the high-conviction floor (>=0.70, >=50).
        _seed(s, "QQQ", 0.60, 40, 3.0)
    bias = load_eod_bias()
    assert "QQQ" in bias
    row = bias["QQQ"]
    assert not row.is_high_conviction()
    assert row.is_info_only()
    assert priority_tickers_from_bias(bias) == []


def test_empty_corpus_returns_empty_dict(fresh_db):
    bias = load_eod_bias()
    assert bias == {}


def test_ranks_assigned_by_rank_score_desc(fresh_db):
    with session_scope() as s:
        _seed(s, "AAA", 0.75, 60, 4.5)
        _seed(s, "BBB", 0.78, 80, 5.0)  # highest rank_score
        _seed(s, "CCC", 0.72, 55, 3.2)
    bias = load_eod_bias()
    ordered = sorted(bias.items(), key=lambda kv: kv[1].rank)
    assert [t for t, _ in ordered] == ["BBB", "AAA", "CCC"]


def test_eod_bias_row_to_dict_contains_flags():
    row = EodBiasRow(
        ticker="NVDA", rank=1, top_pattern="bull_flag",
        posterior=0.85, sample_size=120, rank_score=5.5,
    )
    d = row.to_dict()
    assert d["high_conviction"] is True
    assert d["info_only"] is True
    assert d["ticker"] == "NVDA"


def test_high_conviction_floor_respects_tunables(monkeypatch):
    # Setting the floor stricter than the row's posterior moves it OUT
    # of high_conviction territory.
    row = EodBiasRow(
        ticker="X", rank=1, posterior=0.72, sample_size=60, rank_score=4.0,
    )
    assert row.is_high_conviction() is True
    monkeypatch.setattr(TUNABLES, "eod_high_conviction_posterior", 0.95)
    assert row.is_high_conviction() is False
