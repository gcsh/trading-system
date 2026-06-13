"""MITS Phase 6 (P6.1) — Live outcome → corpus ingest tests."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from backend.bot.corpus.live_outcome_ingest import (
    LIVE_ENGINE_SOURCE, WATERMARK_SOURCE,
    apply_live_weighted_posterior,
    ingest_closed_trade,
    ingest_live_outcomes,
    split_observations_by_provenance,
)
from backend.config import TUNABLES
from backend.db import init_db, session_scope
from backend.models.ingest_watermark import IngestWatermark
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.trade import Trade


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
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed_trade(*, ticker="NVDA", strategy="ai_brain",
                       signal_source="brain", pnl=125.0,
                       price=420.0, quantity=10.0,
                       instrument="stock",
                       top_pattern="bull_flag",
                       status="closed",
                       ts: datetime = None) -> int:
    """Insert one Trade row and return its id."""
    ts = ts or datetime.utcnow() - timedelta(days=2)
    detail = {}
    if top_pattern:
        detail["eod_bias"] = {"top_pattern": top_pattern,
                                       "rank": 1, "posterior": 0.72}
    with session_scope() as s:
        t = Trade(
            timestamp=ts,
            ticker=ticker, action="BUY", quantity=quantity,
            price=price, strategy=strategy,
            signal_source=signal_source,
            confidence=0.8, reason="test",
            paper=1, pnl=pnl, status=status,
            instrument=instrument, contracts=None,
            detail_json=json.dumps(detail) if detail else None,
        )
        s.add(t)
        s.flush()
        return int(t.id)


def test_ingest_closed_trade_creates_obs_and_outcome(fresh_db):
    tid = _seed_trade(pnl=200.0)
    r = ingest_closed_trade(tid)
    assert r["skipped"] is False
    assert r["observation_id"] is not None
    assert r["outcome_id"] is not None
    with session_scope() as s:
        obs = s.get(MarketObservation, r["observation_id"])
        assert obs is not None
        assert obs.source == LIVE_ENGINE_SOURCE
        assert obs.ticker == "NVDA"
        assert obs.pattern == "bull_flag"
        feats = json.loads(obs.features)
        assert feats["trade_id"] == tid
        assert feats["signal_source"] == "brain"
        out = s.get(MarketOutcome, r["outcome_id"])
        assert out is not None
        assert out.was_winner is True


def test_ingest_skips_open_trade(fresh_db):
    tid = _seed_trade(status="open")
    r = ingest_closed_trade(tid)
    assert r["skipped"] is True
    assert r["reason"] == "trade_open"


def test_ingest_skips_when_no_pnl(fresh_db):
    # Manually create a closed trade with None pnl.
    with session_scope() as s:
        t = Trade(
            timestamp=datetime.utcnow(), ticker="SPY", action="BUY",
            quantity=1, price=400.0, strategy="x",
            signal_source="brain", confidence=0.5, reason="",
            paper=1, pnl=None, status="closed",
            instrument="stock",
        )
        s.add(t)
        s.flush()
        tid = int(t.id)
    r = ingest_closed_trade(tid)
    assert r["skipped"] is True
    assert r["reason"] == "no_pnl"


def test_ingest_is_idempotent(fresh_db):
    tid = _seed_trade()
    r1 = ingest_closed_trade(tid)
    r2 = ingest_closed_trade(tid)
    assert r1["skipped"] is False
    assert r2["skipped"] is True
    assert r2["reason"] == "already_ingested"
    # Only one observation row exists.
    with session_scope() as s:
        n = s.query(MarketObservation).count()
        assert n == 1


def test_pattern_falls_back_to_strategy(fresh_db):
    """When no eod_bias.top_pattern, the strategy name becomes the
    pattern label so the cohort still aggregates."""
    tid = _seed_trade(top_pattern=None, strategy="ema50_momentum",
                            signal_source="strategy", pnl=50.0)
    r = ingest_closed_trade(tid)
    assert r["skipped"] is False
    with session_scope() as s:
        obs = s.get(MarketObservation, r["observation_id"])
        assert obs.pattern == "ema50_momentum"


def test_watermark_advances_after_batch(fresh_db):
    tids = [_seed_trade(pnl=10.0 + i) for i in range(3)]
    stats = ingest_live_outcomes(recompute=False)
    assert stats["trades_ingested"] == 3
    assert stats["last_ingested_trade_id"] == max(tids)
    # Re-run is a no-op (watermark already at max).
    stats2 = ingest_live_outcomes(recompute=False)
    assert stats2["trades_ingested"] == 0
    assert stats2["trades_considered"] == 0
    with session_scope() as s:
        wm = s.query(IngestWatermark).filter_by(
            source=WATERMARK_SOURCE).first()
        assert wm is not None
        assert int(wm.last_ingested_trade_id) == max(tids)
        assert int(wm.rows_ingested_total) == 3


def test_live_weighted_posterior_blends_when_below_floor():
    """Below the authoritative floor we blend; the multiplier amplifies
    live observations."""
    out = apply_live_weighted_posterior(
        historical_n=100, historical_wins=50,
        live_n=10, live_wins=10,
        live_weight_multiplier=5.0,
        live_authoritative_floor=30,
    )
    assert out["mode"] == "live_weighted"
    # Combined posterior should land between historical-only and
    # live-only because both contribute.
    assert out["historical_posterior"] < out["primary_posterior"]
    assert out["primary_posterior"] < out["live_posterior"]


def test_live_authoritative_at_or_above_floor():
    out = apply_live_weighted_posterior(
        historical_n=400, historical_wins=200,
        live_n=50, live_wins=40,
        live_weight_multiplier=5.0,
        live_authoritative_floor=30,
    )
    assert out["mode"] == "live_authoritative"
    assert out["primary_posterior"] == out["live_posterior"]
    # Historical is now secondary, not silenced.
    assert out["secondary_posterior"] == out["historical_posterior"]


def test_split_observations_by_provenance():
    rows = [
        {"was_winner": True, "source": "historical_replay"},
        {"was_winner": True, "source": "live_engine"},
        {"was_winner": False, "source": "live_trade"},
        {"was_winner": True, "source": None},
    ]
    hist, live = split_observations_by_provenance(rows)
    assert len(hist) == 2  # historical_replay + None
    assert len(live) == 2  # live_engine + live_trade


def test_tunables_carry_p6_defaults():
    assert TUNABLES.live_outcome_weight_multiplier == 5.0
    assert TUNABLES.live_n_authoritative_floor == 30
