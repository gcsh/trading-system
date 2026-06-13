"""MITS Phase 11.B.2 — Options EOD chain backfill unit tests.

Covers:
  - CSV parser for expirations + strikes endpoints
  - JSON envelope parser for per-contract EOD history (incl. dedup)
  - Strike-window selection around an ATM spot anchor
  - INSERT OR IGNORE silver writer dedupes on the PK
  - The orchestrator callback's (ticker|expiry) token convention
  - Watermark advance via SyncOrchestrator chunk progress
  - Options corpus replay populates observations
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import pytest


# ── fetch_expirations / fetch_strikes CSV parsing ─────────────────────


def test_fetch_expirations_parses_csv(temp_db, monkeypatch) -> None:
    from backend.bot.data import thetadata_options_history as toh

    csv_body = (
        "symbol,expiration\n"
        '"AAPL","2021-06-18"\n'
        '"AAPL","2021-07-16"\n'
        '"AAPL","2021-07-16"\n'  # dup — must dedupe
        '"AAPL","2026-06-20"\n'
    )

    def fake_get(path, params):
        assert path == "/v3/option/list/expirations"
        assert params["symbol"] == "AAPL"
        return (200, csv_body)

    monkeypatch.setattr(toh, "_http_get", fake_get)
    exps = toh.fetch_expirations("AAPL")
    assert exps == [date(2021, 6, 18), date(2021, 7, 16),
                    date(2026, 6, 20)]


def test_fetch_strikes_dollar_decimals(temp_db, monkeypatch) -> None:
    from backend.bot.data import thetadata_options_history as toh

    csv_body = (
        "symbol,strike\n"
        '"AAPL",130.000\n'
        '"AAPL",135.000\n'
        '"AAPL",140.000\n'
    )

    def fake_get(path, params):
        assert path == "/v3/option/list/strikes"
        assert params["expiration"] == "20210618"
        return (200, csv_body)

    monkeypatch.setattr(toh, "_http_get", fake_get)
    strikes = toh.fetch_strikes("AAPL", date(2021, 6, 18))
    # Strikes are DOLLARS, not ×1000 — keep the units sane.
    assert strikes == [130.0, 135.0, 140.0]


# ── per-contract EOD parsing + dedup ──────────────────────────────────


def test_fetch_contract_history_dedupes_double_snapshot(
        temp_db, monkeypatch) -> None:
    """ThetaData v3 sends 2 snapshots per trading day (intra-afternoon
    refresh). The fetcher must dedupe on ``last_trade`` date and keep
    the latest ``created`` snapshot."""
    from backend.bot.data import thetadata_options_history as toh

    payload = {
        "response": [{
            "contract": {"symbol": "AAPL", "expiration": "2021-06-18",
                         "strike": 130.0, "right": "CALL"},
            "data": [
                # Two snapshots for 2021-06-01 — keep the later ``created``.
                {"last_trade": "2021-06-01T15:59:50.453",
                 "created": "2021-06-01T18:34:20.980",
                 "open": 0.77, "high": 0.79, "low": 0.56, "close": 0.66,
                 "volume": 22811, "count": 3143,
                 "bid": 0.65, "ask": 0.66},
                {"last_trade": "2021-06-01T15:59:50.453",
                 "created": "2021-06-01T20:12:59.934",
                 "open": 0.77, "high": 0.79, "low": 0.56, "close": 0.66,
                 "volume": 22811, "count": 3143,
                 "bid": 0.65, "ask": 0.66},
                # Single snapshot for 2021-06-02.
                {"last_trade": "2021-06-02T15:59:41.862",
                 "created": "2021-06-02T17:15:51.312",
                 "open": 0.66, "high": 0.81, "low": 0.52, "close": 0.68,
                 "volume": 34529, "count": 3375,
                 "bid": 0.67, "ask": 0.68},
            ],
        }],
    }

    def fake_get(path, params):
        assert path == "/v3/option/history/eod"
        assert params["right"] == "C"  # normalized from "CALL"
        return (200, json.dumps(payload))

    monkeypatch.setattr(toh, "_http_get", fake_get)
    rows = toh.fetch_contract_history(
        "AAPL", date(2021, 6, 18), 130.0, "CALL",
        date(2021, 6, 1), date(2021, 6, 18),
    )
    # 2 unique trading days post-dedup.
    assert len(rows) == 2
    assert rows[0]["bar_date"] == date(2021, 6, 1)
    assert rows[1]["bar_date"] == date(2021, 6, 2)
    assert rows[0]["right"] == "C"
    # Mid is computed from bid/ask.
    assert rows[0]["mid"] == pytest.approx(0.655, rel=1e-6)
    # Snapshot kept = LATER one (20:12 > 18:34). Both snapshots had
    # identical OHLC so this is a coverage-only check.
    assert rows[0]["close"] == 0.66


# ── ATM strike window selection ───────────────────────────────────────


def test_select_strike_window_picks_atm_neighbors() -> None:
    from backend.bot.data import thetadata_options_history as toh

    strikes = [100, 105, 110, 115, 120, 125, 130, 135, 140, 145]
    # Spot = 123 → ATM = 125. ±2 each side = [115, 120, 125, 130, 135].
    selected = toh._select_strike_window(strikes, 123.0, 2)
    assert selected == [115, 120, 125, 130, 135]


def test_select_strike_window_handles_none_spot() -> None:
    from backend.bot.data import thetadata_options_history as toh

    strikes = [100, 105, 110, 115, 120]
    # No spot anchor → return all.
    assert toh._select_strike_window(strikes, None, 2) == strikes


# ── INSERT OR IGNORE silver writer ────────────────────────────────────


def test_write_silver_option_bars_dedupes(temp_db) -> None:
    from sqlalchemy import select

    from backend.bot.data.thetadata_options_history import (
        write_silver_option_bars,
    )
    from backend.db import session_scope
    from backend.models.option_contract_bar import OptionContractBar

    rows = [
        {"ticker": "AAPL", "expiration": date(2021, 6, 18),
         "strike": 130.0, "right": "C", "bar_date": date(2021, 6, 1),
         "open": 0.77, "high": 0.79, "low": 0.56, "close": 0.66,
         "bid": 0.65, "ask": 0.66, "mid": 0.655,
         "volume": 22811, "trade_count": 3143},
        {"ticker": "AAPL", "expiration": date(2021, 6, 18),
         "strike": 130.0, "right": "C", "bar_date": date(2021, 6, 2),
         "close": 0.68, "bid": 0.67, "ask": 0.68, "mid": 0.675},
    ]
    n1 = write_silver_option_bars(rows)
    assert n1 == 2

    # Re-running the same rows is a no-op (INSERT OR IGNORE).
    n2 = write_silver_option_bars(rows)
    assert n2 == 0

    with session_scope() as s:
        count = s.query(OptionContractBar).count()
        assert count == 2


# ── token convention ──────────────────────────────────────────────────


def test_callback_token_round_trip(temp_db, monkeypatch) -> None:
    """The launcher encodes ``"AAPL|20210618"`` so the orchestrator's
    (ticker, chunk_start, chunk_end) signature carries the expiry. The
    callback must decode it back."""
    from backend.bot.data import thetadata_options_history as toh
    from backend.bot.data.sync_orchestrator import CallbackResult

    # Stub the network paths so we can run the callback offline.
    monkeypatch.setattr(toh, "fetch_strikes",
                        lambda t, e: [128.0, 130.0, 132.0])
    monkeypatch.setattr(toh, "fetch_contract_history",
                        lambda *a, **kw: [])
    # Skip stock_bars lookup by stubbing _spot_at.
    monkeypatch.setattr(toh, "_spot_at", lambda *a, **kw: 130.0)

    result = toh.options_eod_backfill_callback(
        "AAPL|20210618",
        date(2021, 6, 1), date(2021, 6, 18),
    )
    assert isinstance(result, CallbackResult)
    # Spot=130 ± 15 strikes covers all 3 supplied strikes.
    assert result.metadata.get("strikes_total") == 3
    assert result.metadata.get("strikes_selected") == 3


def test_list_active_expiration_tokens_filters_window(
        temp_db, monkeypatch) -> None:
    from backend.bot.data import thetadata_options_history as toh

    # Three expirations: one well before, one overlapping, one future.
    monkeypatch.setattr(toh, "fetch_expirations", lambda t: [
        date(2018, 6, 15),   # lifetime 2017-04-22 → 2018-06-15 — out
        date(2022, 6, 17),   # overlaps 2021-06-09..2026-06-09 — in
        date(2028, 1, 21),   # lifetime starts 2026-11... — out (after end)
    ])
    tokens = toh.list_active_expiration_tokens(
        "AAPL", date(2021, 6, 9), date(2026, 6, 9))
    assert tokens == ["AAPL|20220617"]


# ── orchestrator chunk progress ───────────────────────────────────────


def test_orchestrator_advances_chunk_for_options_callback(
        temp_db, monkeypatch) -> None:
    """End-to-end: register the options callback, run bulk_backfill on
    a stub, verify BackfillProgress row marked done and rows counted."""
    from sqlalchemy import select

    from backend.bot.data.sync_orchestrator import (
        CallbackResult, SyncOrchestrator,
    )
    from backend.db import session_scope
    from backend.models.backfill_progress import BackfillProgress

    orch = SyncOrchestrator()

    def fake_cb(token, chunk_start, chunk_end):
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=42,
            metadata={"stub": True},
        )

    orch.register("thetadata_options_eod", fake_cb)
    summary = orch.bulk_backfill(
        "thetadata_options_eod", "AAPL|20210618",
        date(2021, 6, 1), date(2021, 6, 18),
        chunk_days=20,
    )
    assert summary.completed_chunks == 1
    assert summary.error_chunks == 0
    assert summary.rows_written == 42

    with session_scope() as s:
        rows = s.execute(
            select(BackfillProgress)
            .where(BackfillProgress.source == "thetadata_options_eod")
            .where(BackfillProgress.ticker == "AAPL|20210618")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "done"
        assert rows[0].rows_written == 42


# ── options_replay observation generation ─────────────────────────────


def test_options_replay_generates_iv_rank_observation(temp_db) -> None:
    from datetime import datetime as dt

    from backend.bot.corpus import options_replay
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.option_contract_bar import OptionContractBar
    from backend.models.stock_bar import StockBar

    # Seed: 5 trading days of stock bars + a single front-month
    # straddle on each. Spot anchor lets the replay pick ATM.
    with session_scope() as s:
        for i, d in enumerate([
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
            date(2024, 1, 5), date(2024, 1, 8),
        ]):
            s.add(StockBar(ticker="AAPL", interval="1d",
                           bar_ts=dt.combine(d, dt.min.time()),
                           close=180.0 + i,
                           source="test"))
            for right, price in (("C", 5.0), ("P", 4.0)):
                s.add(OptionContractBar(
                    ticker="AAPL",
                    expiration=date(2024, 2, 16),
                    strike=180.0,
                    right=right,
                    bar_date=d,
                    open=price, high=price + 0.5, low=price - 0.5,
                    close=price, bid=price - 0.05, ask=price + 0.05,
                    mid=price, volume=100, source="test",
                ))

    counts = options_replay.replay_ticker(
        "AAPL", date(2024, 1, 1), date(2024, 1, 10))
    assert counts["option_iv_rank"] >= 1

    with session_scope() as s:
        obs = s.execute(
            select(MarketObservation)
            .where(MarketObservation.pattern == "option_iv_rank")
            .where(MarketObservation.ticker == "AAPL")
        ).scalars().all()
        assert len(obs) >= 1
        feats = json.loads(obs[0].features)
        assert "iv" in feats and "rank_pct" in feats


# `select` import used in the replay test.
from sqlalchemy import select  # noqa: E402
