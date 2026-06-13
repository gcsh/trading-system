"""MITS Phase 11.B.1 — ThetaData stock backfill tests.

Verifies the JSON envelope parser, the normalization layer, and the
callback's silver-row writes against the StockBar table.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List

import pytest


# ── parsing ────────────────────────────────────────────────────────────


def test_parse_daily_payload_normalizes_rows() -> None:
    from backend.bot.data import thetadata_stocks as ts

    payload = {
        "header": {"format": ["date", "open", "high", "low", "close", "volume"]},
        "response": [{
            "data": [
                {"date": "20240102", "open": 100.0, "high": 102.0,
                  "low": 99.0, "close": 101.5, "volume": 1_234_567},
                {"date": "20240103", "open": 101.5, "high": 103.0,
                  "low": 100.5, "close": 102.7, "volume": 2_345_678},
            ]
        }],
    }
    body = json.dumps(payload)
    rows = ts._parse_bars(body)
    assert len(rows) == 2
    bar = ts._normalize_bar(rows[0], ticker="AAPL", interval="1d",
                                  default_date=None, interval_ms=None)
    assert bar is not None
    assert bar["ticker"] == "AAPL"
    assert bar["interval"] == "1d"
    assert bar["bar_ts"] == datetime(2024, 1, 2)
    assert bar["close"] == 101.5
    assert bar["volume"] == 1_234_567


def test_parse_intraday_payload_ms_of_day() -> None:
    from backend.bot.data import thetadata_stocks as ts

    payload = {
        "response": [{
            "data": [
                {"date": "20240102", "ms_of_day": 34_200_000,  # 9:30 ET
                  "open": 192.0, "high": 192.5, "low": 191.5,
                  "close": 192.2, "volume": 12345},
                {"date": "20240102", "ms_of_day": 34_260_000,  # 9:31 ET
                  "open": 192.2, "high": 192.8, "low": 191.9,
                  "close": 192.4, "volume": 23456},
            ]
        }],
    }
    body = json.dumps(payload)
    raw = ts._parse_bars(body)
    bar = ts._normalize_bar(raw[0], ticker="SPY", interval="1m",
                                  default_date=None, interval_ms=60_000)
    assert bar["bar_ts"] == datetime(2024, 1, 2, 9, 30, 0)
    bar2 = ts._normalize_bar(raw[1], ticker="SPY", interval="1m",
                                  default_date=None, interval_ms=60_000)
    assert bar2["bar_ts"] == datetime(2024, 1, 2, 9, 31, 0)


def test_fetch_daily_history_raises_on_non_200(monkeypatch) -> None:
    from backend.bot.data import thetadata_stocks as ts

    def fake_get(path, params):
        return (500, "server died")

    monkeypatch.setattr(ts, "_http_get", fake_get)
    with pytest.raises(RuntimeError, match="status=500"):
        ts.fetch_daily_history("AAPL", date(2024, 1, 1), date(2024, 1, 5))


def test_fetch_daily_history_returns_empty_on_472(monkeypatch) -> None:
    from backend.bot.data import thetadata_stocks as ts

    monkeypatch.setattr(ts, "_http_get", lambda *a, **k: (472, ""))
    out = ts.fetch_daily_history("AAPL", date(2024, 1, 1), date(2024, 1, 5))
    assert out == []


# ── silver writer ─────────────────────────────────────────────────────


def test_write_silver_bars_idempotent(temp_db) -> None:
    from sqlalchemy import select

    from backend.bot.data.thetadata_stocks import write_silver_bars
    from backend.db import session_scope
    from backend.models.stock_bar import StockBar

    rows = [{
        "ticker": "AAPL", "interval": "1d",
        "bar_ts": datetime(2024, 1, 2),
        "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.5,
        "volume": 1_000_000, "vwap": None, "trades": None,
    }]
    assert write_silver_bars(rows) == 1
    # Re-write the same row — must be a no-op.
    assert write_silver_bars(rows) == 0
    with session_scope() as s:
        count = s.execute(
            select(StockBar).where(StockBar.ticker == "AAPL")
        ).scalars().all()
        assert len(count) == 1


# ── callback ──────────────────────────────────────────────────────────


def test_daily_callback_writes_rows_and_returns_summary(
        temp_db, monkeypatch) -> None:
    from sqlalchemy import select

    from backend.bot.data import thetadata_stocks as ts
    from backend.db import session_scope
    from backend.models.stock_bar import StockBar

    fake_rows = [
        {"ticker": "MSFT", "interval": "1d",
          "bar_ts": datetime(2024, 1, 2),
          "open": 370.0, "high": 372.0, "low": 369.0, "close": 371.0,
          "volume": 20_000_000, "vwap": None, "trades": None},
        {"ticker": "MSFT", "interval": "1d",
          "bar_ts": datetime(2024, 1, 3),
          "open": 371.0, "high": 373.5, "low": 370.0, "close": 373.0,
          "volume": 18_000_000, "vwap": None, "trades": None},
    ]

    monkeypatch.setattr(ts, "fetch_daily_history",
                              lambda t, s, e: list(fake_rows))
    # Stub the bronze writer so we don't hit S3.
    monkeypatch.setattr(ts, "write_bronze_bars",
                              lambda *args, **kwargs: None)

    result = ts.daily_backfill_callback("MSFT",
                                                date(2024, 1, 1),
                                                date(2024, 1, 3))
    assert result.rows_written == 2
    assert result.last_completed_date == date(2024, 1, 3)

    with session_scope() as s:
        rows = s.execute(
            select(StockBar).where(StockBar.ticker == "MSFT")
            .where(StockBar.interval == "1d")
        ).scalars().all()
        assert len(rows) == 2


def test_intraday_callback_factory_emits_per_interval(
        temp_db, monkeypatch) -> None:
    from backend.bot.data import thetadata_stocks as ts

    monkeypatch.setattr(ts, "fetch_intraday_history",
                              lambda t, s, e, interval="1m": [])
    monkeypatch.setattr(ts, "write_bronze_bars",
                              lambda *a, **k: None)
    cb = ts.intraday_backfill_callback_factory("1m")
    result = cb("AMD", date(2024, 1, 2), date(2024, 1, 2))
    # No rows but the chunk still marks complete.
    assert result.rows_written == 0
    assert result.last_completed_date == date(2024, 1, 2)
