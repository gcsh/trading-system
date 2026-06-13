"""Stage-18a — FRED, Market Breadth, and SEC EDGAR data sources.

Pinned:
  • FRED client returns empty when no key set; refresh is a no-op
  • FRED upsert deduplicates by (series_id, date)
  • FRED latest / history / change_pct / yield_curve_inverted work on cache
  • Breadth compute_breadth produces correct %s for a small synthetic universe
  • Breadth regime_health verdict bands hit the right buckets
  • Breadth refresh persists a snapshot
  • EDGAR client falls through silently without user agent
  • EDGAR ticker→CIK map caches in-process
  • EDGAR recent_filings filters to requested forms + limit
  • EDGAR has_material_event flags 8-K with material item codes
  • Endpoints respond cleanly with no data (cold start)
"""
import json
import os
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import backend.bot.data.edgar as edgar_mod
from backend.bot.breadth import (
    BreadthStats,
    TickerHistory,
    compute_breadth,
    refresh as breadth_refresh,
    regime_health,
)
from backend.bot.data.edgar import (
    DEFAULT_FORMS,
    MATERIAL_8K_ITEMS,
    EdgarClient,
    FilingRow,
    has_material_event,
    recent_filings_cached,
    refresh_ticker,
)
from backend.bot.data.fred import (
    CANONICAL_SERIES,
    FredClient,
    FredObs,
    change_pct,
    history,
    latest,
    macro_snapshot,
    refresh as fred_refresh,
    yield_curve_inverted,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


# ── FRED ────────────────────────────────────────────────────────────────


class TestFred:
    def test_no_key_no_refresh(self, temp_db):
        cl = FredClient(api_key="")
        out = fred_refresh(client=cl)
        assert out["available"] is False
        assert out.get("reason")

    def test_upsert_dedupes(self, temp_db):
        # Inject a fake fetcher so we don't hit the network.
        rows = [FredObs(date=date(2026, 5, 28), value=4.25),
                  FredObs(date=date(2026, 5, 29), value=4.27)]
        def fake_fetch(series_id, *, api_key, limit=365):
            return rows
        cl = FredClient(api_key="x", fetcher=fake_fetch)
        first = fred_refresh(series=["DFF"], client=cl)
        assert first["results"]["DFF"] == 2
        # Second call: same data → 0 new rows
        second = fred_refresh(series=["DFF"], client=cl)
        assert second["results"]["DFF"] == 0

    def test_helpers_on_cached(self, temp_db):
        # Seed the cache directly
        rows = [FredObs(date=date(2026, 5, 1) + timedelta(days=i),
                          value=4.0 + i * 0.01) for i in range(40)]
        cl = FredClient(api_key="x", fetcher=lambda *a, **kw: rows)
        fred_refresh(series=["DFF"], client=cl)
        l = latest("DFF")
        assert l is not None and l.value > 4.0
        h = history("DFF", limit=10)
        assert len(h) == 10
        # change_pct over 30 days should be positive (linear ramp)
        chg = change_pct("DFF", days=30)
        assert chg is not None and chg > 0

    def test_yield_curve_inverted(self, temp_db):
        cl_10 = FredClient(api_key="x", fetcher=lambda *a, **kw: [
            FredObs(date=date(2026, 5, 28), value=3.8)
        ])
        cl_2 = FredClient(api_key="x", fetcher=lambda *a, **kw: [
            FredObs(date=date(2026, 5, 28), value=4.2)
        ])
        fred_refresh(series=["DGS10"], client=cl_10)
        fred_refresh(series=["DGS2"], client=cl_2)
        assert yield_curve_inverted() is True

    def test_macro_snapshot_shape(self, temp_db):
        snap = macro_snapshot()
        assert "DFF" in snap
        assert "DGS10" in snap
        assert "yield_curve_inverted" in snap
        assert "spread_10y_2y" in snap


# ── Market Breadth ──────────────────────────────────────────────────────


def _synthetic_history(tickers, *, trending_pct=0.6):
    """Build per-ticker price history where ``trending_pct`` of tickers
    are clearly above their 20/50/200 DMAs and the rest below."""
    out = {}
    for i, t in enumerate(tickers):
        if i / len(tickers) < trending_pct:
            # Uptrend: 100 → 200 over 250 days
            closes = [100.0 + 0.4 * j for j in range(250)]
        else:
            # Downtrend
            closes = [200.0 - 0.4 * j for j in range(250)]
        dates = [datetime(2026, 1, 1) + timedelta(days=j) for j in range(250)]
        out[t] = TickerHistory(ticker=t, closes=closes, dates=dates)
    return out


class TestBreadth:
    def test_compute_correct_percentages(self):
        hist = _synthetic_history(["A", "B", "C", "D", "E"], trending_pct=0.6)
        stats = compute_breadth(hist)
        assert isinstance(stats, BreadthStats)
        # 3 of 5 tickers trending up → 60% above 200-DMA
        assert stats.pct_above_200dma == pytest.approx(0.6, abs=0.01)
        assert stats.sample_size == 5

    def test_compute_empty_input(self):
        assert compute_breadth({}) is None

    def test_refresh_persists(self, temp_db):
        def fake_fetcher(tickers, **kw):
            return _synthetic_history(list(tickers)[:10])
        out = breadth_refresh(history_fetcher=fake_fetcher)
        assert out["snapshots_written"] == 1
        # Idempotent within same date
        again = breadth_refresh(history_fetcher=fake_fetcher)
        assert again["snapshots_written"] == 0

    def test_regime_health_no_data(self, temp_db):
        rh = regime_health()
        assert rh["verdict"] == "unknown"

    def test_regime_health_healthy_advance(self, temp_db):
        def fake_fetcher(tickers, **kw):
            return _synthetic_history(list(tickers), trending_pct=0.80)
        breadth_refresh(history_fetcher=fake_fetcher)
        rh = regime_health()
        assert rh["verdict"] == "healthy_advance"
        assert rh["pct_above_50dma"] >= 0.65

    def test_regime_health_broken(self, temp_db):
        def fake_fetcher(tickers, **kw):
            return _synthetic_history(list(tickers), trending_pct=0.20)
        breadth_refresh(history_fetcher=fake_fetcher)
        rh = regime_health()
        assert rh["verdict"] == "broken"


# ── SEC EDGAR ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_edgar_cache():
    edgar_mod._TICKER_MAP_CACHE.clear()
    yield
    edgar_mod._TICKER_MAP_CACHE.clear()


class _StubFetcher:
    """Stub HTTP getter for EDGAR — returns canned JSON by URL match."""
    def __init__(self, ticker_map=None, submissions=None):
        self.ticker_map = ticker_map or {}
        self.submissions = submissions or {}

    def __call__(self, url, *, user_agent):
        if "company_tickers.json" in url:
            return json.dumps({
                str(i): {"ticker": t.upper(), "cik_str": int(c)}
                for i, (t, c) in enumerate(self.ticker_map.items())
            }).encode()
        for cik, payload in self.submissions.items():
            if cik in url:
                return json.dumps(payload).encode()
        return b"{}"


def _filings_payload(forms, dates, items=None):
    """Build the SEC submissions JSON shape from parallel lists."""
    return {
        "filings": {
            "recent": {
                "accessionNumber": [f"acc-{i}" for i in range(len(forms))],
                "form": forms,
                "filingDate": dates,
                "primaryDocument": [f"doc-{i}.htm" for i in range(len(forms))],
                "items": items or [""] * len(forms),
            }
        }
    }


class TestEdgar:
    def test_no_user_agent_falls_through(self, temp_db):
        cl = EdgarClient(user_agent="")
        out = refresh_ticker("NVDA", client=cl)
        assert out["available"] is False

    def test_ticker_to_cik_caches(self, temp_db):
        fetcher = _StubFetcher(ticker_map={"NVDA": "1045810"})
        cl = EdgarClient(user_agent="test/1.0", getter=fetcher)
        c1 = cl.ticker_to_cik("NVDA")
        # Second call should not need to refetch — cache is in-process.
        c2 = cl.ticker_to_cik("nvda")     # case-insensitive
        assert c1 == "0001045810" and c2 == c1

    def test_recent_filings_filters_forms(self, temp_db):
        fetcher = _StubFetcher(
            ticker_map={"NVDA": "1045810"},
            submissions={"1045810": _filings_payload(
                forms=["8-K", "10-Q", "DEF 14A", "8-K", "4"],
                dates=["2026-05-30", "2026-05-15", "2026-05-10",
                          "2026-05-08", "2026-05-01"],
                items=["2.02", "", "", "5.02", ""],
            )},
        )
        cl = EdgarClient(user_agent="test/1.0", getter=fetcher)
        rows = cl.recent_filings("NVDA", forms=("8-K", "10-Q", "4"), limit=10)
        assert len(rows) == 4
        assert all(r.form in ("8-K", "10-Q", "4") for r in rows)

    def test_refresh_persists_filings(self, temp_db):
        fetcher = _StubFetcher(
            ticker_map={"NVDA": "1045810"},
            submissions={"1045810": _filings_payload(
                forms=["8-K", "10-Q"],
                dates=["2026-05-30", "2026-05-15"],
                items=["2.02", ""],
            )},
        )
        cl = EdgarClient(user_agent="test/1.0", getter=fetcher)
        first = refresh_ticker("NVDA", client=cl)
        assert first["inserted"] == 2
        # Second call is idempotent
        second = refresh_ticker("NVDA", client=cl)
        assert second["inserted"] == 0
        # Cached read
        cached = recent_filings_cached("NVDA")
        assert len(cached) == 2

    def test_material_event_detection(self, temp_db):
        # Seed a recent 8-K with item 2.02 (results announcement)
        from backend.db import session_scope
        from backend.models.edgar_filing import EdgarFiling
        with session_scope() as s:
            f = EdgarFiling(
                cik="0001045810", ticker="NVDA",
                accession_number="acc-1", form="8-K",
                filed_at=datetime.utcnow() - timedelta(hours=2),
                items="2.02",
            )
            s.add(f)
        assert has_material_event("NVDA", within_hours=24) is True
        assert has_material_event("AAPL", within_hours=24) is False


# ── Endpoints (cold-start, no real API calls) ──────────────────────────


class TestEndpointsColdStart:
    def test_fred_snapshot(self, client):
        body = client.get("/fred/snapshot").json()
        assert "snapshot" in body
        assert all(s in body["snapshot"] for s in ("DFF", "DGS10", "DGS2"))

    def test_fred_series(self, client):
        body = client.get("/fred/series/DFF").json()
        assert body["series_id"] == "DFF"
        assert body["observations"] == []

    def test_breadth_latest(self, client):
        body = client.get("/breadth/latest").json()
        assert body["snapshot"] is None
        assert body["universe_size"] > 0

    def test_breadth_health(self, client):
        body = client.get("/breadth/health").json()
        assert body["verdict"] == "unknown"

    def test_edgar_filings_empty(self, client):
        body = client.get("/edgar/filings/NVDA").json()
        assert body["filings"] == []

    def test_edgar_material_endpoint(self, client):
        body = client.get("/edgar/material/NVDA").json()
        assert body["has_material_event"] is False
