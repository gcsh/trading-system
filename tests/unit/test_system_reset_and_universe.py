"""Locks in the fresh-start contract and the watchlist→engine union.

Both surfaces were touched recently without explicit test coverage —
this test exists so future changes that break either contract fail
loudly here rather than in the live UI.
"""
from backend.bot.system_reset import (
    EXTERNAL_CACHE_TABLES,
    PAPER_STATE_TABLES,
    fresh_start,
)


class TestFreshStartContract:
    def test_paper_state_table_inventory(self):
        """The PAPER_STATE_TABLES list is the operator's promise:
        every bot-generated table is cleared. Lock the current
        membership so anyone who adds a new bot-state table is
        forced to read the docstring + update this list."""
        labels = {label for _, label in PAPER_STATE_TABLES}
        # If you add a new bot-state model and don't list it here,
        # this test fails — and that's the point.
        expected = {
            "trades",
            "decision_log",
            "execution_log",
            "paper_positions",
            "portfolio_snapshots",
            "regime_episode_snapshots",
            # Telegram pending messages — a fresh-start wipes these so
            # we don't push stale alerts about the previous run's
            # trades that no longer exist after the reset.
            "telegram_outbox",
            # MITS Phase 16.B / 18.B — decision provenance + the
            # counterfactual replay cache keyed off provenance_id.
            "decision_provenance",
            "counterfactual_replays",
            # Fix N=6 (2026-06-13) — child tables of trades that
            # carry FK to trades.id. Must wipe before the parent so
            # fresh_start doesn't crash on FK constraint failure.
            "eod_prediction_outcomes",
            "brain_predictions",
        }
        assert labels == expected, (
            f"PAPER_STATE_TABLES changed. New: {labels - expected}. "
            f"Missing: {expected - labels}. Update this test only after "
            f"confirming the new table really belongs (or really doesn't)."
        )

    def test_external_cache_kept_list_documented(self):
        """External-cache tables MUST be in the documented keep-list
        so the next reader knows they were considered."""
        assert "bot_config" in EXTERNAL_CACHE_TABLES
        assert "earnings_call_intel" in EXTERNAL_CACHE_TABLES
        assert "fred_observations" in EXTERNAL_CACHE_TABLES
        assert "watchlist_items" in EXTERNAL_CACHE_TABLES

    def test_fresh_start_zeroes_account(self, temp_db):
        report = fresh_start(starting_cash=5000.0)
        assert report.account_after["starting_cash"] == 5000.0
        assert report.account_after["cash"] == 5000.0
        assert report.account_after["realized_pnl"] == 0.0
        assert report.starting_cash == 5000.0

    def test_fresh_start_is_idempotent(self, temp_db):
        r1 = fresh_start(5000.0)
        r2 = fresh_start(5000.0)
        # Second call should clear zero rows (nothing left to clear).
        assert all(v == 0 for v in r2.cleared.values()), (
            f"second fresh_start cleared {r2.cleared}"
        )
        # Account still at $5k.
        assert r2.account_after["cash"] == 5000.0


class TestScanUniverseUnion:
    """Locks in the engine's contract: scan list = config.tickers ∪ watchlist."""

    def test_endpoint_returns_union(self, client, monkeypatch):
        # Seed config + watchlist with known tickers.
        from backend.db import session_scope
        from backend.models.config import load_config, save_config
        from backend.models.watchlist import WatchlistItem

        with session_scope() as s:
            cfg = load_config(s)
            cfg["tickers"] = ["AAPL", "SPY"]
            save_config(s, cfg)
            s.query(WatchlistItem).delete()
            s.add(WatchlistItem(ticker="NVDA"))
            s.add(WatchlistItem(ticker="aapl"))   # duplicate, lower-case
            s.add(WatchlistItem(ticker="TSLA"))

        r = client.get("/authority/scan-universe").json()
        tickers = r["tickers"]
        # AAPL deduped; case-folded to upper.
        assert "AAPL" in tickers
        assert "SPY" in tickers
        assert "NVDA" in tickers
        assert "TSLA" in tickers
        # No duplicates.
        assert len(tickers) == len(set(tickers))
        # Sources split correctly.
        assert set(r["from_config"]) >= {"AAPL", "SPY"}
        assert "NVDA" in r["from_watchlist"]
        assert "TSLA" in r["from_watchlist"]
        # AAPL is in config, so NOT in from_watchlist even though watchlist also has it.
        assert "AAPL" not in r["from_watchlist"]


import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    import os
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)
