"""Stage-18b — Wave 2 sources (FINRA short volume, CFTC COT)."""
import os
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.data.cot import CotClient, CotRow, latest_for as cot_latest, refresh as cot_refresh
from backend.bot.data.finra import (
    FinraClient,
    ShortVolumeRow,
    latest_for as finra_latest,
    refresh as finra_refresh,
    short_pressure,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


# ── FINRA ───────────────────────────────────────────────────────────────


def _short_rows(ticker, *, days_back=5, pcts=None):
    out = []
    base = date(2026, 5, 28)
    pcts = pcts or [0.30] * days_back
    for i, p in enumerate(pcts):
        out.append(ShortVolumeRow(
            ticker=ticker, settlement_date=base - timedelta(days=i),
            short_volume=p * 1_000_000, total_volume=1_000_000,
        ))
    return out


class TestFinra:
    def test_upsert_and_latest(self, temp_db):
        rows = _short_rows("NVDA", pcts=[0.35, 0.30, 0.28])
        cl = FinraClient(fetcher=lambda *_a, **_kw: rows)
        out = finra_refresh(client=cl)
        # Only one settlement date per call (target_date); the fetcher
        # returns multiple days but they all have distinct dates so dedup
        # inserts them all.
        assert out["rows_inserted"] == 3
        latest = finra_latest("NVDA")
        assert latest is not None
        assert latest["short_interest"] > 0

    def test_short_pressure_rising_high(self, temp_db):
        rows = _short_rows("NVDA", pcts=[0.45, 0.42, 0.38, 0.32, 0.28])
        cl = FinraClient(fetcher=lambda *_a, **_kw: rows)
        finra_refresh(client=cl)
        p = short_pressure("NVDA")
        assert p["level"] == "high"
        assert p["trend"] == "rising"

    def test_short_pressure_unknown_for_missing(self, temp_db):
        p = short_pressure("XYZ")
        assert p["level"] == "unknown"
        assert p["sample_size"] == 0

    def test_ticker_filter_keeps_only_watchlist(self, temp_db):
        # Fetcher returns 3 tickers; we keep only NVDA.
        rows = (_short_rows("NVDA", pcts=[0.4])
                 + _short_rows("XYZ", pcts=[0.3])
                 + _short_rows("AAPL", pcts=[0.2]))
        cl = FinraClient(fetcher=lambda *_a, **_kw: rows)
        out = finra_refresh(tickers=["NVDA"], client=cl)
        assert out["rows_inserted"] == 1
        assert finra_latest("NVDA") is not None
        assert finra_latest("XYZ") is None


# ── COT ─────────────────────────────────────────────────────────────────


def _cot_rows(instrument, *, dates=None):
    dates = dates or [date(2026, 5, 27)]
    return [
        CotRow(instrument=instrument, report_date=d,
                 noncomm_long=80_000, noncomm_short=20_000,
                 comm_long=50_000, comm_short=110_000,
                 open_interest=250_000)
        for d in dates
    ]


class TestCot:
    def test_upsert_es(self, temp_db):
        cl = CotClient(fetcher=lambda **_kw: _cot_rows("ES"))
        out = cot_refresh(client=cl)
        assert out["rows_inserted"] == 1
        latest = cot_latest("ES")
        assert latest is not None
        assert latest["noncommercial_net"] == 60_000      # 80k − 20k

    def test_idempotent(self, temp_db):
        cl = CotClient(fetcher=lambda **_kw: _cot_rows("ES"))
        cot_refresh(client=cl)
        again = cot_refresh(client=cl)
        assert again["rows_inserted"] == 0

    def test_three_instruments(self, temp_db):
        rows = (_cot_rows("ES") + _cot_rows("TY") + _cot_rows("DX"))
        cl = CotClient(fetcher=lambda **_kw: rows)
        out = cot_refresh(client=cl)
        assert out["rows_inserted"] == 3
        assert cot_latest("ES") is not None
        assert cot_latest("TY") is not None
        assert cot_latest("DX") is not None


# ── Endpoints (cold start) ─────────────────────────────────────────────


class TestEndpoints:
    def test_finra_endpoint_empty(self, client):
        body = client.get("/finra/short-interest/NVDA").json()
        assert body["ticker"] == "NVDA"
        assert body["latest"] is None
        assert body["pressure"]["level"] == "unknown"

    def test_cot_snapshot_empty(self, client):
        body = client.get("/cot/snapshot").json()
        assert "ES" in body["positioning"]
        assert body["positioning"]["ES"] is None
