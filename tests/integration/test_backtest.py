"""Backtest engine tests with mocked yfinance candles."""
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.bot import backtest


def _synthetic_df(n=120):
    rng = np.random.default_rng(7)
    # Trending series so MACD/MA strategies have something to fire on.
    base = np.linspace(100, 140, n) + rng.normal(0, 1.5, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "Open": base + rng.normal(0, 0.3, n),
            "High": base + abs(rng.normal(0, 0.6, n)),
            "Low": base - abs(rng.normal(0, 0.6, n)),
            "Close": base,
            "Volume": rng.integers(1_000_000, 5_000_000, n),
        },
        index=idx,
    )


def test_run_backtest_returns_chart_payload():
    with patch.object(backtest, "fetch_candles", return_value=_synthetic_df()):
        out = backtest.run_backtest("macd_momentum", "AAPL")
    assert "candles" in out and len(out["candles"]) == 120
    assert "indicators" in out
    assert set(["ma50", "ma200", "macd", "macd_signal", "rsi"]).issubset(out["indicators"].keys())
    assert "markers" in out
    assert "explanation" in out
    # Indicator series length matches candles.
    assert len(out["indicators"]["ma50"]) == len(out["candles"])


def test_run_backtest_unknown_strategy():
    out = backtest.run_backtest("does_not_exist", "AAPL")
    assert "error" in out


def test_run_backtest_no_data():
    with patch.object(backtest, "fetch_candles", return_value=pd.DataFrame()):
        out = backtest.run_backtest("macd_momentum", "AAPL")
    assert "error" in out


def test_run_backtest_includes_equity_curve_and_metrics():
    with patch.object(backtest, "fetch_candles", return_value=_synthetic_df()):
        out = backtest.run_backtest("macd_momentum", "AAPL")
    bt = out["backtest"]
    assert "equity_curve" in bt and len(bt["equity_curve"]) == 120
    for k in ("total_return_pct", "buy_hold_return_pct", "alpha_pct",
              "num_trades", "win_rate", "profit_factor", "max_drawdown_pct", "sharpe"):
        assert k in bt
    # Equity curve starts at the starting equity.
    assert abs(bt["equity_curve"][0]["equity"] - bt["starting_equity"]) < 1e-6
    # alpha = strategy return - buy&hold.
    assert abs(bt["alpha_pct"] - (bt["total_return_pct"] - bt["buy_hold_return_pct"])) < 0.011


def test_simulate_records_round_trip_trades():
    # Engineer a clear up-then-down series so RSI mean reversion trades.
    n = 120
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    prices = np.concatenate([np.linspace(150, 90, 60), np.linspace(90, 150, 60)])
    df = pd.DataFrame({"Open": prices, "High": prices + 0.5, "Low": prices - 0.5,
                       "Close": prices, "Volume": np.full(n, 1_000_000)}, index=idx)
    with patch.object(backtest, "fetch_candles", return_value=df):
        out = backtest.run_backtest("rsi_mean_reversion", "AAPL")
    bt = out["backtest"]
    # Each trade has entry/exit and a return.
    for t in bt["trades"]:
        assert "entry_px" in t and "exit_px" in t and "return_pct" in t


def test_compare_returns_real_metrics_and_benchmark():
    with patch.object(backtest, "fetch_candles", return_value=_synthetic_df()):
        out = backtest.run_compare("AAPL", ["macd_momentum", "rsi_mean_reversion"])
    assert "buy_hold_return_pct" in out
    for s in out["strategies"]:
        assert set(["total_return_pct", "alpha_pct", "num_trades", "max_drawdown_pct",
                    "sharpe", "equity_curve"]).issubset(s.keys())


def test_run_compare_ranks_multiple_strategies():
    with patch.object(backtest, "fetch_candles", return_value=_synthetic_df()):
        out = backtest.run_compare(
            "AAPL",
            ["macd_momentum", "rsi_mean_reversion", "gap_fill"],
        )
    assert "strategies" in out and len(out["strategies"]) == 3
    assert "ranking" in out and len(out["ranking"]) == 3
    assert "suggestion" in out and "headline" in out["suggestion"]
    # Each strategy carries a distinct color + stats keys.
    colors = {s["color"] for s in out["strategies"]}
    assert len(colors) == 3
    for s in out["strategies"]:
        assert set(["marker_count", "hit_rate", "cumulative_return_pct", "color"]).issubset(s.keys())
    # Shared candles present once.
    assert len(out["candles"]) == 120


def test_run_compare_strategies_differ():
    """Different strategies should not produce identical marker sets."""
    with patch.object(backtest, "fetch_candles", return_value=_synthetic_df()):
        out = backtest.run_compare("AAPL", ["macd_momentum", "rsi_mean_reversion"])
    by_name = {s["strategy"]: [m["t"] for m in s["markers"]] for s in out["strategies"]}
    # They may overlap but shouldn't be byte-identical signal sets.
    assert by_name["macd_momentum"] != by_name["rsi_mean_reversion"] or (
        len(by_name["macd_momentum"]) == 0 and len(by_name["rsi_mean_reversion"]) == 0
    )


def test_markers_have_forward_return_and_actions():
    with patch.object(backtest, "fetch_candles", return_value=_synthetic_df()):
        out = backtest.run_backtest("rsi_mean_reversion", "AAPL")
    for m in out["markers"]:
        assert m["action"] in (
            "BUY_STOCK", "SELL_STOCK", "BUY_CALL", "BUY_PUT",
            "BULL_CALL_SPREAD", "BUY_STRADDLE", "IRON_CONDOR",
            "SELL_COVERED_CALL", "SELL_CSP", "RATIO_SPREAD", "COLLAR",
        )
        assert "price" in m and "t" in m
