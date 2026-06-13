"""Data-quality guard + multi-source cross-validation (mocked, no network)."""
import numpy as np
import pandas as pd

import backend.bot.backtest as bt
import backend.bot.data.validate as v


# ── quality guard: bad bars are dropped + reported ───────────────────────────

def test_clean_and_assess_drops_nan_dup_and_broken_bars():
    idx = pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22"])
    df = pd.DataFrame(
        {
            "Open":  [10, 11, 11, 12, 13, 14],
            "High":  [10.5, 11.5, 11.5, 12.5, 9.0, 14.5],   # 5th bar High<Low (broken)
            "Low":   [9.5, 10.5, 10.5, 11.5, 13.5, 13.5],
            "Close": [10, 11, 11, np.nan, 13.2, 14],         # 4th bar NaN close
            "Volume": [1, 1, 1, 1, 1, 1],
        },
        index=idx,  # 2nd/3rd are duplicate dates
    )
    key = ("TEST", "3mo", "1d")
    out = bt._clean_and_assess(df, key)
    rep = bt._QUALITY[key]

    assert rep["dropped_nan"] == 1
    assert rep["duplicates_removed"] == 1
    assert rep["ohlc_violations_removed"] == 1
    assert rep["ok"] is False
    assert rep["adjusted"] is True
    # every surviving bar is internally consistent
    assert (out["High"] >= out["Low"]).all()
    assert (out["Close"] > 0).all()
    assert out.index.is_monotonic_increasing


def test_clean_and_assess_passes_clean_data():
    n = 30
    close = 100 + np.arange(n) * 0.5
    df = pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": [1000] * n},
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )
    key = ("CLEAN", "3mo", "1d")
    bt._clean_and_assess(df, key)
    rep = bt._QUALITY[key]
    assert rep["ok"] is True
    assert rep["dropped_nan"] == 0 and rep["duplicates_removed"] == 0 and rep["ohlc_violations_removed"] == 0
    assert rep["bars"] == n


# ── crypto providers self-gate WITHOUT touching the network ──────────────────

def test_crypto_providers_skip_equities_offline():
    # is_crypto gate returns None before any HTTP call for stock symbols.
    assert v._coinbase("AAPL", 120) is None
    assert v._kraken("AAPL", 120) is None
    assert v._binance("MSFT", 120) is None


# ── cross_validate consensus logic (mocked sources) ──────────────────────────

def _primary_df():
    idx = pd.to_datetime(["2026-05-20", "2026-05-21", "2026-05-22"])
    return pd.DataFrame(
        {"Open": [100, 101, 102], "High": [100, 101, 102], "Low": [100, 101, 102],
         "Close": [100.0, 101.0, 102.0], "Volume": [1, 1, 1]},
        index=idx,
    )


def test_cross_validate_all_agree(monkeypatch):
    monkeypatch.setattr(bt, "fetch_candles", lambda *a, **k: _primary_df())
    ref = {"2026-05-20": 100.0, "2026-05-21": 101.0, "2026-05-22": 102.0}
    monkeypatch.setattr(v, "PROVIDERS", {
        "src_a": (lambda t, d: dict(ref), False),
        "src_b": (lambda t, d: dict(ref), False),
    })
    v._CACHE.clear()
    r = v.cross_validate("TEST")
    assert r["status"] == "ok"
    assert r["checked"] == 2 and r["agree_count"] == 2 and r["agree"] is True


def test_cross_validate_flags_divergence(monkeypatch):
    monkeypatch.setattr(bt, "fetch_candles", lambda *a, **k: _primary_df())
    good = {"2026-05-20": 100.0, "2026-05-21": 101.0, "2026-05-22": 102.0}
    bad = {"2026-05-20": 100.0, "2026-05-21": 101.0, "2026-05-22": 108.0}  # ~6% off
    monkeypatch.setattr(v, "PROVIDERS", {
        "good": (lambda t, d: dict(good), False),
        "bad": (lambda t, d: dict(bad), False),
    })
    v._CACHE.clear()
    r = v.cross_validate("TEST")
    assert r["checked"] == 2 and r["agree_count"] == 1 and r["agree"] is False


def test_cross_validate_no_reference(monkeypatch):
    monkeypatch.setattr(bt, "fetch_candles", lambda *a, **k: _primary_df())
    monkeypatch.setattr(v, "PROVIDERS", {"none": (lambda t, d: None, False)})
    v._CACHE.clear()
    r = v.cross_validate("TEST")
    assert r["status"] == "no_reference"
    assert r["sources"] == []
