"""Technical analysis signals built on top of the ``ta`` library."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator
from ta.volatility import BollingerBands


@dataclass
class TechnicalSnapshot:
    """Latest computed values for a single ticker."""

    price: float
    rsi: float
    macd: float
    macd_signal: float
    sma20: float
    sma50: float
    sma200: float
    bb_upper: float
    bb_lower: float
    volume: float
    avg_volume: float

    @property
    def volume_spike(self) -> float:
        if self.avg_volume <= 0:
            return 0.0
        return self.volume / self.avg_volume

    @property
    def above_sma50(self) -> bool:
        return self.price > self.sma50

    @property
    def macd_bullish(self) -> bool:
        return self.macd > self.macd_signal


def _last(value) -> float:
    try:
        return float(value.iloc[-1])
    except Exception:
        return 0.0


def compute_snapshot(df: pd.DataFrame) -> Optional[TechnicalSnapshot]:
    """Build a :class:`TechnicalSnapshot` from a Close/Volume history.

    Returns ``None`` if there is not enough data to compute meaningful values
    (need at least ~50 rows for the 50-MA, though indicators degrade gracefully
    on shorter series).
    """
    if df is None or df.empty or "Close" not in df.columns:
        return None
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series([0] * len(close))

    if len(close) < 20:
        return None

    rsi = RSIIndicator(close=close, window=14).rsi()
    macd_ind = MACD(close=close)
    bb = BollingerBands(close=close, window=20, window_dev=2)

    sma20 = SMAIndicator(close=close, window=20).sma_indicator()
    sma50 = SMAIndicator(close=close, window=min(50, len(close))).sma_indicator()
    sma200 = SMAIndicator(close=close, window=min(200, len(close))).sma_indicator()

    avg_volume = volume.rolling(window=min(20, len(volume))).mean()

    return TechnicalSnapshot(
        price=_last(close),
        rsi=_last(rsi),
        macd=_last(macd_ind.macd()),
        macd_signal=_last(macd_ind.macd_signal()),
        sma20=_last(sma20),
        sma50=_last(sma50),
        sma200=_last(sma200),
        bb_upper=_last(bb.bollinger_hband()),
        bb_lower=_last(bb.bollinger_lband()),
        volume=_last(volume),
        avg_volume=_last(avg_volume),
    )


def fetch_history(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Fetch OHLCV history from yfinance. Imported lazily so tests can mock it."""
    import yfinance as yf

    return yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
