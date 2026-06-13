"""Regression: watchlist yfinance quote must survive multi-index columns.

yfinance sometimes returns a column MultiIndex (e.g. ('Close','SPY')), which made
`float(df["Close"].iloc[-1])` raise `TypeError: ... not 'Series'` and silently
broke every watchlist quote. The fetcher now flattens columns first.
"""
import pandas as pd
import yfinance

from backend.api.routes import watchlist as wl


def _multiindex_df():
    idx = pd.date_range("2026-05-20", periods=3, freq="D")
    cols = pd.MultiIndex.from_tuples([("Close", "SPY"), ("Open", "SPY")])
    return pd.DataFrame([[100.0, 99.0], [101.0, 100.0], [102.0, 101.0]], index=idx, columns=cols)


def test_yf_quote_handles_multiindex_columns(monkeypatch):
    monkeypatch.setattr(yfinance, "download", lambda *a, **k: _multiindex_df())
    q = wl._yf_quote("SPY")
    assert q is not None
    assert q["price"] == 102.0
    assert q["prev_close"] == 101.0
    assert round(q["change_pct"], 4) == round((102.0 - 101.0) / 101.0 * 100, 4)


def test_yf_quote_none_on_empty(monkeypatch):
    monkeypatch.setattr(yfinance, "download", lambda *a, **k: pd.DataFrame())
    assert wl._yf_quote("SPY") is None
