"""MarketDataAdapter — turns yfinance + signals into the flat dict strategies expect.

Strategies read ~40 fields per ticker. Most come from price history (price, MAs,
RSI, MACD, VWAP, volume, gap) — those we compute. Some (iv_rank,
hist_earnings_move_avg, options chain, pre-market volume) need paid data
feeds; we stub those with conservative defaults so live trading defaults to
HOLD on strategies that depend on them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice

from backend.bot.signals import news as news_mod
from backend.bot.signals import sentiment as sentiment_mod

logger = logging.getLogger(__name__)


# Default per-ticker stubs for fields a live fetch may not supply. Tunable
# values come from config (TUNABLES) so nothing is hardcoded in logic;
# structural zeros (positions, ORB) stay literal — they're filled from the
# portfolio / intraday data, not operator-tuned. Strategies needing these will
# safely HOLD on the defaults.
from backend.config import TUNABLES as _T

STUB_DEFAULTS: Dict[str, Any] = {
    "iv_rank": _T.default_iv_rank,
    "implied_move": _T.default_implied_move,
    "hist_earnings_move_avg": _T.default_hist_earnings_move,
    "earnings_days": 999,
    "earnings_today": False,
    "has_catalyst": False,
    "catalyst_type": None,
    "shares_owned": 0,
    "position_value": 0.0,
    "unrealized_gain_pct": 0.0,
    "premarket_volume": 0,
    "orb_high": 0.0,
    "orb_low": 0.0,
    "pe_ratio": _T.default_pe_ratio,
    "eps_growth": _T.default_eps_growth,
    "sector_rsi": _T.default_sector_rsi,
    "sector_trend": "neutral",
    "range_3w_pct": _T.default_range_3w_pct,
}


@dataclass
class MarketSnapshot:
    """One ticker's full data dict plus diagnostic info."""

    data: Dict[str, Any]
    source_errors: List[str]


def _last(series: pd.Series) -> float:
    try:
        return float(series.iloc[-1])
    except Exception:
        return 0.0


def _fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(
        ticker, period=period, interval=interval, progress=False, auto_adjust=False
    )
    # yfinance returns MultiIndex columns when multiple tickers are requested;
    # for our single-ticker calls we flatten to plain columns.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    return df


def _fetch_intraday(ticker: str) -> Optional[pd.DataFrame]:
    """5-minute bars for the current session. Returns None on failure."""
    try:
        import yfinance as yf

        df = yf.download(
            ticker, period="1d", interval="5m", progress=False, auto_adjust=False
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        return df if not df.empty else None
    except Exception:
        return None


def _safe_info(ticker: str) -> Dict[str, Any]:
    try:
        import yfinance as yf

        return yf.Ticker(ticker).info or {}
    except Exception:
        return {}


def _orb_levels(intraday: Optional[pd.DataFrame]) -> tuple[float, float]:
    """First 30 minutes (6 x 5-min bars) high/low."""
    if intraday is None or intraday.empty:
        return 0.0, 0.0
    head = intraday.head(6)
    try:
        return float(head["High"].max()), float(head["Low"].min())
    except Exception:
        return 0.0, 0.0


def _vix_close() -> float:
    """Best-effort VIX last close. Default 18 (long-term average-ish)."""
    try:
        import yfinance as yf

        vix = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=False)
        if isinstance(vix.columns, pd.MultiIndex):
            vix.columns = [c[0] for c in vix.columns]
        return _last(vix["Close"]) if not vix.empty else _T.vix_fallback
    except Exception:
        return _T.vix_fallback


def _regime_for(symbol: str) -> Dict[str, Any]:
    """Trend + ADX for any anchor symbol (SPY for stocks, BTC-USD for crypto)."""
    try:
        df = _fetch_history(symbol, period="6mo", interval="1d")
        if df.empty:
            return {"trend": "neutral", "adx": _T.spy_adx_fallback}
        close, high, low = df["Close"], df["High"], df["Low"]
        sma50 = SMAIndicator(close, window=min(50, len(close))).sma_indicator()
        sma200 = SMAIndicator(close, window=min(200, len(close))).sma_indicator()
        adx = ADXIndicator(high=high, low=low, close=close, window=14).adx()
        trend = "bullish" if _last(close) > _last(sma50) > _last(sma200) else (
            "bearish" if _last(close) < _last(sma50) < _last(sma200) else "neutral"
        )
        return {"trend": trend, "adx": _last(adx)}
    except Exception:
        return {"trend": "neutral", "adx": _T.spy_adx_fallback}


def _spy_regime() -> Dict[str, Any]:
    """SPY trend + ADX for equity regime detection."""
    r = _regime_for("SPY")
    return {"spy_trend": r["trend"], "spy_adx": r["adx"]}


class MarketDataAdapter:
    """Stateless adapter — call :meth:`snapshot(ticker)` once per cycle."""

    def __init__(self, news_client=None) -> None:
        self._news_client = news_client
        self._spy_cache: Dict[str, Any] | None = None
        self._vix_cache: Optional[float] = None
        self._crypto_cache: Dict[str, Any] | None = None

    def _crypto_context(self, ticker: str) -> Dict[str, Any]:
        """Regime anchored on BTC (not SPY) for crypto symbols."""
        from backend.bot.market_profile import regime_anchor

        if self._crypto_cache is None:
            r = _regime_for(regime_anchor(ticker))
            self._crypto_cache = {"spy_trend": r["trend"], "spy_adx": r["adx"], "market_trend": r["trend"]}
        return self._crypto_cache

    def _market_context(self) -> Dict[str, Any]:
        if self._spy_cache is None:
            self._spy_cache = _spy_regime()
        if self._vix_cache is None:
            self._vix_cache = _vix_close()
        ctx = dict(self._spy_cache)
        ctx["vix"] = self._vix_cache
        if ctx["spy_trend"] == "bullish":
            ctx["market_trend"] = "bullish"
        elif ctx["spy_trend"] == "bearish":
            ctx["market_trend"] = "bearish"
        else:
            ctx["market_trend"] = "neutral"
        return ctx

    def reset_market_cache(self) -> None:
        self._spy_cache = None
        self._vix_cache = None
        self._crypto_cache = None

    def snapshot(self, ticker: str) -> MarketSnapshot:
        errors: List[str] = []
        data: Dict[str, Any] = dict(STUB_DEFAULTS)

        try:
            daily = _fetch_history(ticker)
        except Exception as exc:
            errors.append(f"daily fetch: {exc}")
            daily = pd.DataFrame()

        if not daily.empty and "Close" in daily.columns:
            close = daily["Close"].astype(float)
            high = daily["High"].astype(float)
            low = daily["Low"].astype(float)
            volume = daily["Volume"].astype(float) if "Volume" in daily.columns else pd.Series([0] * len(close))

            data["price"] = _last(close)
            data["prev_close"] = float(close.iloc[-2]) if len(close) > 1 else _last(close)
            data["open_price"] = float(daily["Open"].iloc[-1]) if "Open" in daily.columns else data["price"]
            data["high_52w"] = float(close.tail(252).max())
            data["volume"] = _last(volume)
            data["avg_volume"] = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else _last(volume)

            data["rsi"] = _last(RSIIndicator(close=close, window=14).rsi())
            macd = MACD(close=close)
            data["macd"] = _last(macd.macd())
            data["macd_signal"] = _last(macd.macd_signal())
            hist = macd.macd_diff()
            data["macd_hist"] = _last(hist)
            data["prev_macd_hist"] = float(hist.iloc[-2]) if len(hist) > 1 else 0.0
            data["ma50"] = _last(SMAIndicator(close, window=min(50, len(close))).sma_indicator())
            data["ma200"] = _last(SMAIndicator(close, window=min(200, len(close))).sma_indicator())
            data["adx"] = _last(ADXIndicator(high=high, low=low, close=close, window=14).adx())
            # EMA50/EMA200 + ATR — needed by the EMA50 momentum continuation
            # strategy (STRAT.1) and any agent that wants exponential trend
            # filtering instead of the simple-MA version above. Computed
            # here once so every strategy reads from the same source.
            data["ema50"] = _last(EMAIndicator(close, window=min(50, len(close))).ema_indicator())
            data["ema200"] = _last(EMAIndicator(close, window=min(200, len(close))).ema_indicator())
            data["atr"] = _last(AverageTrueRange(
                high=high, low=low, close=close, window=14,
            ).average_true_range())

            if data["prev_close"] > 0:
                data["gap_pct"] = (data["open_price"] - data["prev_close"]) / data["prev_close"]
            else:
                data["gap_pct"] = 0.0

            data["range_3w_pct"] = float(
                (close.tail(15).max() - close.tail(15).min()) / max(close.tail(15).mean(), 1e-6)
            )

        # Real options data (free: yfinance chain + Cboe Greeks). Gives the
        # options strategies real iv_rank / implied_move / earnings instead of
        # stubs. Cached ~10 min; never blocks the cycle.
        try:
            from backend.bot.data.options import options_snapshot, premarket_volume

            opt = options_snapshot(ticker, float(data.get("price") or 0.0))
            for k in ("iv_rank", "implied_move", "earnings_days", "earnings_today", "has_options"):
                if k in opt:
                    data[k] = opt[k]
            pv = premarket_volume(ticker)
            if pv is not None:
                data["premarket_volume"] = pv
        except Exception as exc:
            errors.append(f"options: {exc}")

        # Heatseeker (GEX) + Flowseeker (options flow) context — cached &
        # best-effort; their pipelines validate before this reaches strategies.
        # If a feed is down, strategies just don't see the keys (no change).
        try:
            from backend.bot.signals.gex import gex_context

            data.update(gex_context(ticker))
        except Exception as exc:
            errors.append(f"gex: {exc}")
        try:
            from backend.bot.signals.flow import flow_context

            data.update(flow_context(ticker))
        except Exception as exc:
            errors.append(f"flow: {exc}")

        intraday = _fetch_intraday(ticker)
        if intraday is not None and not intraday.empty:
            try:
                vwap = VolumeWeightedAveragePrice(
                    high=intraday["High"], low=intraday["Low"],
                    close=intraday["Close"], volume=intraday["Volume"], window=14,
                ).volume_weighted_average_price()
                data["vwap"] = _last(vwap)
                data["rsi_5m"] = _last(RSIIndicator(intraday["Close"], window=14).rsi())
                if len(intraday["Close"]) >= 2:
                    last = float(intraday["Close"].iloc[-1])
                    prev = float(intraday["Close"].iloc[-2])
                    data["momentum_5m"] = (last - prev) / max(prev, 1e-6) * 100
                else:
                    data["momentum_5m"] = 0.0
            except Exception as exc:
                errors.append(f"intraday calc: {exc}")
            high, low = _orb_levels(intraday)
            data["orb_high"] = high
            data["orb_low"] = low

        # News sentiment (if NEWS_API_KEY set)
        try:
            news_snap = news_mod.news_snapshot(ticker, client=self._news_client)
            data["news_score"] = news_snap.average_sentiment
            data["news_age_hours"] = self._news_age_hours(news_snap)
            data["has_catalyst"] = abs(news_snap.average_sentiment) > 0.5 and len(news_snap.items) > 0
        except Exception as exc:
            errors.append(f"news: {exc}")
            data["news_score"] = 0.0
            data["news_age_hours"] = 999

        # Fundamentals (yfinance)
        info = _safe_info(ticker)
        if info:
            try:
                if info.get("trailingPE") not in (None, ""):
                    data["pe_ratio"] = float(info["trailingPE"])
                if info.get("revenueGrowth") not in (None, ""):
                    data["eps_growth"] = float(info["revenueGrowth"])
                if info.get("recommendationKey"):
                    data["analyst_rating"] = info["recommendationKey"]
            except Exception as exc:
                errors.append(f"fundamentals: {exc}")

        # Market-wide context (cached across tickers in the same cycle).
        data.update(self._market_context())
        # Crypto trades 24/7 and marches to BTC, not SPY — override the regime.
        from backend.bot.market_profile import is_crypto

        if is_crypto(ticker):
            data.update(self._crypto_context(ticker))
        data["time_of_day"] = datetime.now(timezone.utc).astimezone().strftime("%H:%M")

        return MarketSnapshot(data=data, source_errors=errors)

    @staticmethod
    def _news_age_hours(snap) -> float:
        """Hours since the most-recent article in the snapshot."""
        if not snap.items:
            return 999.0
        latest = snap.items[0].published_at
        if not latest:
            return 999.0
        try:
            stripped = latest.replace("Z", "+00:00")
            when = datetime.fromisoformat(stripped)
            age = datetime.now(timezone.utc) - when
            return max(0.0, age.total_seconds() / 3600.0)
        except Exception:
            return 999.0
