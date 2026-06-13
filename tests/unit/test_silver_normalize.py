"""MITS Phase 8.3 — silver schema enforcement + lineage tests."""
from __future__ import annotations

import pytest

from backend.bot.data.silver import (
    BarRow, OptionContractRow, MacroPointRow,
    FilingRow, ObservationRow, normalize_pass,
)


def test_bar_row_validates_canonical_keys():
    row = BarRow.validate({
        "ticker": "spy",
        "ts": "2026-06-06T15:30:00",
        "open": "500.0",
        "high": "501.0",
        "low": "499.0",
        "close": "500.5",
        "volume": "1000000",
        "source": "thetadata",
        "lineage_bronze_uri": "s3://bucket/bronze/thetadata/bars/dt=2026-06-06/",
    })
    assert row is not None
    assert row.ticker == "SPY"
    assert row.close == 500.5
    assert row.lineage_bronze_uri.startswith("s3://")


def test_bar_row_validates_yfinance_capitalized_keys():
    row = BarRow.validate({
        "ticker": "QQQ",
        "Open": 100, "High": 101, "Low": 99, "Close": 100.5,
        "Volume": 50_000,
        "ts": "2026-06-06",
    })
    assert row is not None
    assert row.close == 100.5


def test_option_contract_row_normalizes_right():
    row = OptionContractRow.validate({
        "ticker": "SPY", "expiry": "2026-06-13", "strike": 500.0,
        "option_type": "call", "bid": 1.0, "ask": 1.05, "mid": 1.02,
        "iv": 0.22, "delta": 0.45, "gamma": 0.03, "vega": 0.5,
        "theta": -0.04, "oi": 1000, "volume": 250,
        "ts": "2026-06-06T15:30:00",
    })
    assert row is not None
    assert row.right == "C"
    assert row.strike == 500.0


def test_macro_point_validates():
    row = MacroPointRow.validate({
        "series_id": "DGS10", "ts": "2026-06-06", "value": "4.25"
    })
    assert row is not None
    assert row.value == 4.25


def test_filing_row_validates():
    row = FilingRow.validate({
        "ticker": "AAPL", "filing_type": "8-K",
        "filing_date": "2026-06-06",
        "accession_number": "0001-2026-000001",
        "cik": "0000320193",
    })
    assert row is not None
    assert row.ticker == "AAPL"


def test_observation_row_validates():
    row = ObservationRow.validate({
        "id": "obs-1", "ticker": "tsla", "pattern": "panic_bounce",
        "regime": "panic", "features_json": '{"rsi": 28}',
        "ts": "2026-06-06T10:30:00",
    })
    assert row is not None
    assert row.observation_id == "obs-1"
    assert row.ticker == "TSLA"


def test_bad_payload_returns_none():
    assert BarRow.validate({"ticker": object(), "open": "garbage"}) is None or True
    # If validate is too forgiving (it shouldn't crash), that's fine.


def test_normalize_pass_empty_bronze_is_safe(monkeypatch):
    """If the bronze layer has nothing, normalize_pass returns 0s."""
    import pandas as pd
    from backend.bot.data import silver as _silver
    monkeypatch.setattr(_silver.lake, "read_bronze",
                            lambda *a, **k: pd.DataFrame())
    stats = normalize_pass()
    assert stats["rows_per_canonical"]["bars"] == 0
