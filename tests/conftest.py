"""Shared fixtures: in-memory DB, mocked Robinhood/yfinance/NewsAPI, sample data."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("NEWS_API_KEY", "")
os.environ.setdefault("DISABLE_SCHEDULER", "1")


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the SQLite DB at a fresh per-test file and re-init."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from backend import config as cfg_mod
    from backend import db as db_mod

    cfg_mod.SETTINGS.db_path = str(db_path)
    db_mod._engine = None
    db_mod._SessionLocal = None
    db_mod.init_db(str(db_path))
    yield db_path
    db_mod._engine = None
    db_mod._SessionLocal = None


@pytest.fixture()
def sample_history():
    """Synthetic 60-day price/volume DataFrame the technical module accepts."""
    rng = np.random.default_rng(42)
    closes = 100 + np.cumsum(rng.normal(0, 1, 60))
    df = pd.DataFrame(
        {
            "Open": closes + rng.normal(0, 0.3, 60),
            "High": closes + abs(rng.normal(0, 0.5, 60)),
            "Low": closes - abs(rng.normal(0, 0.5, 60)),
            "Close": closes,
            "Volume": rng.integers(1_000_000, 5_000_000, 60),
        }
    )
    return df


@pytest.fixture()
def oversold_history():
    """Engineered series that pushes RSI below 30."""
    closes = np.concatenate([np.linspace(150, 80, 30), np.full(30, 79.0)])
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.1,
            "Low": closes - 0.1,
            "Close": closes,
            "Volume": np.full(60, 1_000_000),
        }
    )
    return df


@pytest.fixture()
def overbought_history():
    closes = np.concatenate([np.linspace(80, 150, 30), np.full(30, 151.0)])
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.1,
            "Low": closes - 0.1,
            "Close": closes,
            "Volume": np.full(60, 1_000_000),
        }
    )
    return df


@pytest.fixture()
def momentum_history():
    """Steady uptrend with a recent volume spike — momentum buy candidate."""
    closes = np.linspace(80, 120, 59)
    closes = np.append(closes, 121.0)
    volumes = np.full(60, 1_000_000)
    volumes[-1] = 3_000_000
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.5,
            "Low": closes - 0.5,
            "Close": closes,
            "Volume": volumes,
        }
    )
    return df


@pytest.fixture()
def fake_news_client():
    client = MagicMock()
    client.get_everything.return_value = {
        "articles": [
            {
                "title": "AAPL beats earnings estimates and raises guidance",
                "description": "Strong quarter with record iPhone sales.",
                "url": "https://example.com/1",
                "publishedAt": "2026-05-01T00:00:00Z",
            },
            {
                "title": "AAPL upgraded by analysts after strong report",
                "description": "Positive outlook for next year.",
                "url": "https://example.com/2",
                "publishedAt": "2026-05-02T00:00:00Z",
            },
        ]
    }
    return client


@pytest.fixture()
def mock_rh():
    """Mock the robin_stocks module surface used by Executor."""
    rh = MagicMock()
    rh.orders.order_buy_market.return_value = {"id": "order-1", "state": "queued"}
    rh.orders.order_sell_market.return_value = {"id": "order-2", "state": "queued"}
    rh.orders.order_buy_limit.return_value = {"id": "order-3"}
    rh.orders.order_sell_limit.return_value = {"id": "order-4"}
    rh.orders.order_buy_option_limit.return_value = {"id": "opt-1"}
    rh.orders.order_sell_option_limit.return_value = {"id": "opt-2"}
    rh.profiles.load_account_profile.return_value = {"buying_power": "5000"}
    rh.profiles.load_portfolio_profile.return_value = {"equity": "25000"}
    rh.account.get_open_stock_positions.return_value = [{"quantity": "0"}]
    rh.login.return_value = {"detail": "logged_in"}
    return rh
