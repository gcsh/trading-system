"""Train the LightGBM next-bar-direction model.

Run with::

    .venv/bin/python -m backend.bot.ai.train --tickers SPY AAPL TSLA --period 2y

Pulls daily OHLCV from yfinance, computes the same features the live model
uses (RSI, MACD, MA ratios, etc), and labels each row by whether the *next*
day's close is above the current. Trains a binary classifier and writes the
booster to ``ML_MODEL_PATH``.

This is intentionally simple — a real production model would use intraday
data, walk-forward validation, and proper feature engineering. The point
here is to give the bot a working ML baseline out of the box.
"""
from __future__ import annotations

import argparse
import logging
import os
from typing import List

import numpy as np
import pandas as pd

from backend.bot.ai.ml_signal import FEATURE_NAMES

logger = logging.getLogger(__name__)


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the feature columns the live model expects."""
    from ta.momentum import RSIIndicator
    from ta.trend import ADXIndicator, MACD, SMAIndicator

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_ind = MACD(close=close)
    ma50 = SMAIndicator(close=close, window=50).sma_indicator()
    ma200 = SMAIndicator(close=close, window=200).sma_indicator()
    adx = ADXIndicator(high=high, low=low, close=close, window=14).adx()
    avg_volume = volume.rolling(20).mean()
    rsi_5m = rsi  # Daily proxy; intraday would differ.
    momentum_5m = close.pct_change(5)
    gap_pct = (df["Open"] - close.shift(1)) / close.shift(1) * 100
    range_3w = (close.rolling(15).max() - close.rolling(15).min()) / close

    features = pd.DataFrame(
        {
            "rsi": rsi,
            "macd": macd_ind.macd(),
            "macd_signal": macd_ind.macd_signal(),
            "macd_hist": macd_ind.macd_diff(),
            "ma50_ratio": close / ma50,
            "ma200_ratio": close / ma200,
            "volume_ratio": volume / avg_volume,
            "iv_rank": 30,  # Placeholder — historical IV needs another source.
            "adx": adx,
            "vix": 18,  # Placeholder.
            "news_score": 0,
            "pe_ratio": 20,
            "earnings_days": 30,
            "gap_pct": gap_pct,
            "unrealized_gain_pct": 0,
            "rsi_5m": rsi_5m,
            "momentum_5m": momentum_5m,
            "range_3w_pct": range_3w,
        }
    )
    return features


def _build_dataset(tickers: List[str], period: str = "2y") -> tuple[pd.DataFrame, pd.Series]:
    import yfinance as yf

    rows: List[pd.DataFrame] = []
    labels: List[pd.Series] = []
    for ticker in tickers:
        df = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=False)
        if df is None or df.empty or len(df) < 250:
            logger.warning("skipping %s — insufficient data", ticker)
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        feats = _build_features(df)
        # Label: 1 if next-day close > today's close.
        label = (df["Close"].shift(-1) > df["Close"]).astype(int)
        combined = pd.concat([feats, label.rename("y")], axis=1).dropna()
        rows.append(combined[FEATURE_NAMES])
        labels.append(combined["y"])
    if not rows:
        raise RuntimeError("no usable tickers")
    X = pd.concat(rows, axis=0).reset_index(drop=True)
    y = pd.concat(labels, axis=0).reset_index(drop=True)
    return X, y


def train(tickers: List[str], period: str, output: str) -> str:
    import lightgbm as lgb

    X, y = _build_dataset(tickers, period)
    logger.info("training on %d rows, %d features", len(X), X.shape[1])
    split = int(len(X) * 0.85)
    X_train, X_val = X.iloc[:split], X.iloc[split:]
    y_train, y_val = y.iloc[:split], y.iloc[split:]
    train_set = lgb.Dataset(X_train, label=y_train)
    val_set = lgb.Dataset(X_val, label=y_val, reference=train_set)
    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "verbose": -1,
    }
    booster = lgb.train(
        params,
        train_set,
        num_boost_round=400,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(40)],
    )
    booster.save_model(output)
    val_pred = booster.predict(X_val)
    acc = float(np.mean((val_pred > 0.5) == y_val.values))
    logger.info("validation accuracy: %.4f  saved to %s", acc, output)
    return output


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=["SPY", "AAPL", "MSFT", "TSLA", "NVDA"])
    parser.add_argument("--period", default="2y")
    parser.add_argument("--output", default="./ml_model.txt")
    args = parser.parse_args()
    train(args.tickers, args.period, args.output)


if __name__ == "__main__":
    main()
