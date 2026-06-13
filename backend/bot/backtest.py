"""Run a strategy over real historical candles to visualize + explain it.

This powers the "draw the strategy on a real chart" feature: we compute the
indicator series bar-by-bar, replay the strategy's ``analyze`` on each bar's
snapshot, and return the candles + indicator lines + the points where the
strategy fired, plus a plain-English explanation.

Technical strategies (macd_momentum, rsi_mean_reversion, vwap_reversion,
trend_pullback, gap_fill, opening_range_breakout, macd) produce real markers.
Options/news strategies depend on data we don't have historically and will
mostly show no markers — the explanation says so.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, MACD, SMAIndicator
from ta.volatility import BollingerBands

from backend.bot.market_data import STUB_DEFAULTS
from backend.config import TUNABLES
from backend.bot.strategies.adaptive import AdaptiveStrategy
from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY, get_strategy
from backend.bot.strategies.base import Action


def _resolve_strategy(name: str):
    """Resolve a strategy by name, treating 'adaptive' as the meta-selector."""
    if not name or name == "adaptive":
        return AdaptiveStrategy()
    return get_strategy(name)

logger = logging.getLogger(__name__)

STRATEGY_EXPLAINERS: Dict[str, str] = {
    "adaptive": "Adaptive picks the highest-confidence signal across all 15 strategies on each bar. Markers show whichever sub-strategy fired; the MA/MACD/RSI panes are drawn for general context.",
    "macd_momentum": "Buys when the MACD line crosses above its signal line with trend strength (ADX). The MACD/signal lines are drawn in the lower pane; a buy marker appears at each bullish cross.",
    "rsi_mean_reversion": "Buys when RSI drops into oversold territory (<30) and sells when overbought (>70). The RSI pane shows the 30/70 bands; markers sit at the crossings.",
    "vwap_reversion": "Fades moves away from VWAP — buys when price dips well below VWAP expecting reversion. VWAP is drawn on the price pane.",
    "trend_pullback": "In an uptrend (price above the 50/200 MAs), buys pullbacks toward the 50-MA. Both MAs are drawn on the price pane.",
    "macd": "Generic MACD cross strategy. MACD and signal lines in the lower pane.",
    "opening_range_breakout": "Buys a breakout above the first 30-minute range high. The ORB level is marked on the price pane.",
    "gap_fill": "Trades the fade of an opening gap back toward the prior close.",
    "bull_call_spread": "Bullish options spread — needs an options chain + IV that we don't have historically, so few/no markers appear on a price chart.",
    "earnings_straddle": "Volatility play around earnings — needs an earnings calendar + IV; not chartable on price alone.",
    "iron_condor": "Range-bound options income — needs IV rank; not chartable on price alone.",
    "news_catalyst_momentum": "Trades on news sentiment + catalysts — needs a historical news feed; markers won't appear here.",
    "covered_call_wheel": "Income strategy requiring an existing stock position + options chain.",
    "cash_secured_put": "Sells puts for income — needs an options chain.",
    "ratio_spread": "Options ratio spread — needs an options chain.",
    "collar": "Hedging overlay on an existing position — needs an options chain.",
    "zero_dte_scalp": "0DTE index scalping — needs intraday options data.",
}


# Which theory overlays best illustrate each strategy on the chart.
STRATEGY_THEORIES: Dict[str, List[str]] = {
    "adaptive": ["moving_avg", "support_resistance"],
    "macd_momentum": ["moving_avg", "trend_channel"],
    "rsi_mean_reversion": ["bollinger", "support_resistance"],
    "vwap_reversion": ["vwap", "bollinger"],
    "trend_pullback": ["moving_avg", "trend_channel", "fibonacci"],
    "macd": ["moving_avg", "trend_channel"],
    "opening_range_breakout": ["support_resistance", "trend_channel"],
    "gap_fill": ["support_resistance", "vwap"],
    "news_catalyst_momentum": ["trend_channel", "support_resistance"],
}


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] for c in df.columns]
    return df


# Short-lived candle cache so rapid UI interactions (switching strategies,
# periods, or the auto-run on mount) don't hammer yfinance and trip its rate
# limiter — the #1 cause of intermittent "no data" / fetch failures.
_CANDLE_CACHE: Dict[Tuple[str, str, str], Tuple[float, pd.DataFrame]] = {}
_CANDLE_TTL_SEC = TUNABLES.candle_cache_ttl
_CANDLE_LOCK = threading.Lock()
# Last data-quality report per (ticker, period, interval), surfaced to the UI.
_QUALITY: Dict[Tuple[str, str, str], dict] = {}

_OHLC = ["Open", "High", "Low", "Close"]


def _clean_and_assess(df: pd.DataFrame, key: Tuple[str, str, str]) -> pd.DataFrame:
    """Validate + clean candles, recording a quality report for the UI.

    Drops bars that are unusable (NaN OHLC, duplicate timestamps, broken
    high/low, non-positive prices) so a bad print can't corrupt a backtest or
    an indicator. The report tells the user exactly what was checked/removed.
    """
    df = df.copy()
    n0 = len(df)
    for col in _OHLC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    have = [c for c in _OHLC if c in df.columns]
    df = df.dropna(subset=have) if have else df
    nan_dropped = n0 - len(df)

    dups = int(df.index.duplicated().sum())
    if dups:
        df = df[~df.index.duplicated(keep="last")]
    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    ohlc_bad = 0
    if set(_OHLC).issubset(df.columns) and len(df):
        bad = (
            (df["High"] < df["Low"]) | (df["High"] < df["Open"]) | (df["High"] < df["Close"])
            | (df["Low"] > df["Open"]) | (df["Low"] > df["Close"]) | (df["Close"] <= 0)
        )
        ohlc_bad = int(bad.sum())
        if ohlc_bad:
            df = df[~bad]

    removed = nan_dropped + dups + ohlc_bad
    _QUALITY[key] = {
        "source": "yahoo",
        "adjusted": True,
        "bars": len(df),
        "dropped_nan": int(nan_dropped),
        "duplicates_removed": int(dups),
        "ohlc_violations_removed": int(ohlc_bad),
        "ok": removed == 0 and len(df) > 0,
        "note": "clean" if removed == 0 else f"removed {removed} bad bar(s)",
    }
    return df


def fetch_candles(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Download daily/intraday candles with caching, resilient retries, and a
    data-quality guard.

    Uses ``auto_adjust=True`` so prices are corrected for splits AND dividends
    (the latest bar stays at the real price; history is back-adjusted) — this
    makes backtest returns dividend-correct. Returns an empty DataFrame on any
    failure (never raises) so callers surface a clean "no data" message.
    """
    key = (ticker.upper(), period, interval)
    now = time.monotonic()
    with _CANDLE_LOCK:
        hit = _CANDLE_CACHE.get(key)
        if hit and (now - hit[0]) < _CANDLE_TTL_SEC and not hit[1].empty:
            return hit[1]

    import yfinance as yf

    df = pd.DataFrame()
    for attempt in range(3):
        try:
            raw = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if raw is not None and not raw.empty:
                df = _clean_and_assess(_flatten(raw), key)
                break
        except Exception as exc:  # rate limit, network blip, bad ticker
            logger.warning("yfinance fetch failed for %s (attempt %d): %s", ticker, attempt + 1, exc)
        time.sleep(0.6 * (attempt + 1))

    if not df.empty:
        with _CANDLE_LOCK:
            _CANDLE_CACHE[key] = (now, df)
    return df


def get_quality(ticker: str, period: str, interval: str) -> Optional[dict]:
    return _QUALITY.get((ticker.upper(), period, interval))


def compute_indicators(df: pd.DataFrame) -> Dict[str, pd.Series]:
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series(0.0, index=close.index)
    n = len(close)
    nan_series = pd.Series([float("nan")] * n, index=close.index)
    macd = MACD(close=close)
    bb = BollingerBands(close=close, window=20, window_dev=2)
    # ta's ADX indexes by `window`, so it raises IndexError when there are
    # fewer bars than ~2x the window (e.g. a 1-month daily chart). Guard it.
    adx_window = 14
    adx = (
        ADXIndicator(high=high, low=low, close=close, window=adx_window).adx()
        if n > adx_window * 2
        else nan_series
    )
    return {
        "rsi": RSIIndicator(close=close, window=14).rsi() if n > 14 else nan_series,
        "macd": macd.macd(),
        "macd_signal": macd.macd_signal(),
        "macd_hist": macd.macd_diff(),
        "ma50": SMAIndicator(close, window=min(50, max(1, n))).sma_indicator(),
        "ma200": SMAIndicator(close, window=min(200, max(1, n))).sma_indicator(),
        "adx": adx,
        "bb_high": bb.bollinger_hband(),
        "bb_low": bb.bollinger_lband(),
        "avg_volume": volume.rolling(20).mean(),
    }


def _zigzag(closes: List[float], pct: float = TUNABLES.zigzag_pct) -> List[dict]:
    """ZigZag swing pivots: alternating highs/lows confirmed by a % reversal.

    The backbone for price-action support/resistance and a pragmatic
    Elliott-wave-style labeling (not a strict EW count — a readable swing map).
    """
    n = len(closes)
    if n < 3:
        return []
    piv: List[dict] = []
    ext_i, ext_v = 0, closes[0]
    trend = 0  # 0 unknown, 1 up, -1 down
    for i in range(1, n):
        v = closes[i]
        if trend >= 0 and v >= ext_v:
            ext_i, ext_v, trend = i, v, 1
        elif trend <= 0 and v <= ext_v:
            ext_i, ext_v, trend = i, v, -1
        elif trend == 1 and v <= ext_v * (1 - pct):
            piv.append({"index": ext_i, "price": round(ext_v, 2), "kind": "H"})
            ext_i, ext_v, trend = i, v, -1
        elif trend == -1 and v >= ext_v * (1 + pct):
            piv.append({"index": ext_i, "price": round(ext_v, 2), "kind": "L"})
            ext_i, ext_v, trend = i, v, 1
    piv.append({"index": ext_i, "price": round(ext_v, 2), "kind": "H" if trend == 1 else "L"})
    return piv


def compute_overlays(df: pd.DataFrame, ind: Dict[str, pd.Series]) -> Dict[str, Any]:
    """Pre-compute the drawable data for each classic chart theory.

    Returned to the frontend so the chart can *render the theory*: support /
    resistance, Bollinger bands, anchored VWAP, Fibonacci retracement, a swing
    (Elliott-style) wave map, and a linear-regression trend channel.
    """
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series(1.0, index=close.index)
    closes = close.tolist()
    n = len(closes)
    if n == 0:
        return {}

    def ser(s: pd.Series) -> List[Optional[float]]:
        out = []
        for v in s.tolist():
            out.append(None if (v != v) else round(float(v), 3))
        return out

    # --- Bollinger Bands (20, 2σ) ---
    mid = close.rolling(20, min_periods=1).mean()
    sd = close.rolling(20, min_periods=2).std()
    bollinger = {"mid": ser(mid), "upper": ser(mid + 2 * sd), "lower": ser(mid - 2 * sd)}

    # --- Anchored VWAP (from window start) ---
    typical = (high + low + close) / 3.0
    cum_v = vol.cumsum().replace(0, pd.NA)
    vwap = (typical * vol).cumsum() / cum_v
    vwap_series = ser(vwap.astype(float))

    # --- Swing pivots (zigzag) → waves + support/resistance ---
    pivots = _zigzag(closes, pct=0.045)
    # Label the most recent run of pivots like a 1-2-3-4-5 / A-B-C map.
    wave_labels = ["1", "2", "3", "4", "5", "A", "B", "C"]
    recent = pivots[-8:]
    waves = []
    for k, p in enumerate(recent):
        waves.append({**p, "t": df.index[p["index"]].isoformat(), "label": wave_labels[k] if k < len(wave_labels) else ""})

    last_px = closes[-1]
    res_levels = sorted({p["price"] for p in pivots if p["kind"] == "H" and p["price"] >= last_px})[:3]
    sup_levels = sorted({p["price"] for p in pivots if p["kind"] == "L" and p["price"] <= last_px}, reverse=True)[:3]
    # Fallbacks so the lines always show something useful.
    if not res_levels:
        res_levels = [round(float(high.tail(min(60, n)).max()), 2)]
    if not sup_levels:
        sup_levels = [round(float(low.tail(min(60, n)).min()), 2)]
    support_resistance = (
        [{"price": p, "kind": "resistance"} for p in res_levels]
        + [{"price": p, "kind": "support"} for p in sup_levels]
    )

    # --- Fibonacci retracement over the dominant swing ---
    hi_i = int(close.values.argmax())
    lo_i = int(close.values.argmin())
    swing_low = round(float(closes[lo_i]), 2)
    swing_high = round(float(closes[hi_i]), 2)
    uptrend = lo_i < hi_i  # low came first → measuring an up-move's pullbacks
    rng = swing_high - swing_low
    fib_ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    fibonacci = {
        "low": swing_low, "high": swing_high, "uptrend": uptrend,
        "levels": [
            {"ratio": r, "price": round(swing_high - r * rng if uptrend else swing_low + r * rng, 2)}
            for r in fib_ratios
        ],
    }

    # --- Linear-regression trend channel ---
    xs = np.arange(n, dtype=float)
    trend_channel = None
    if n >= 2:
        slope, intercept = np.polyfit(xs, np.array(closes, dtype=float), 1)
        fit = slope * xs + intercept
        resid = np.array(closes) - fit
        half = float(np.max(np.abs(resid))) if len(resid) else 0.0
        trend_channel = {
            "slope": round(float(slope), 5),
            "intercept": round(float(intercept), 3),
            "half_width": round(half, 3),
            "n": n,
        }

    return {
        "support_resistance": support_resistance,
        "bollinger": bollinger,
        "vwap": vwap_series,
        "fibonacci": fibonacci,
        "waves": waves,
        "trend_channel": trend_channel,
    }


def _bar_snapshot(df: pd.DataFrame, ind: Dict[str, pd.Series], i: int) -> Dict[str, Any]:
    """Build the flat snapshot dict the strategies expect, for bar ``i``."""
    def val(series, default=0.0):
        try:
            v = float(series.iloc[i])
            return default if (v != v) else v  # NaN guard
        except Exception:
            return default

    close = df["Close"]
    price = val(close)
    prev_close = float(close.iloc[i - 1]) if i > 0 else price
    open_px = float(df["Open"].iloc[i]) if "Open" in df.columns else price
    data: Dict[str, Any] = dict(STUB_DEFAULTS)
    data.update(
        price=price,
        prev_close=prev_close,
        open_price=open_px,
        rsi=val(ind["rsi"], 50.0),
        macd=val(ind["macd"]),
        macd_signal=val(ind["macd_signal"]),
        macd_hist=val(ind["macd_hist"]),
        prev_macd_hist=float(ind["macd_hist"].iloc[i - 1]) if i > 0 and not pd.isna(ind["macd_hist"].iloc[i - 1]) else 0.0,
        ma50=val(ind["ma50"], price),
        ma200=val(ind["ma200"], price),
        adx=val(ind["adx"], 20.0),
        vwap=val(ind["ma50"], price),  # proxy: intraday VWAP unavailable on daily history
        rsi_5m=val(ind["rsi"], 50.0),
        volume=val(df["Volume"]) if "Volume" in df.columns else 0.0,
        avg_volume=val(ind["avg_volume"], 1.0),
        gap_pct=(open_px - prev_close) / prev_close if prev_close else 0.0,
        # market-wide context: assume neutral/bullish so trend strategies can fire
        spy_trend="bullish",
        spy_adx=val(ind["adx"], 20.0),
        market_trend="bullish",
        vix=16.0,
    )
    return data


def _candles_and_series(df: pd.DataFrame, ind: Dict[str, pd.Series]):
    """Build the chart-ready candle list + shared indicator series once."""
    closes = df["Close"].astype(float).tolist()
    timestamps = [t.isoformat() for t in df.index]
    candles: List[dict] = []
    series = {"ma50": [], "ma200": [], "macd": [], "macd_signal": [], "rsi": []}
    keymap = {"ma50": "ma50", "ma200": "ma200", "macd": "macd", "macd_signal": "macd_signal", "rsi": "rsi"}
    for i in range(len(df)):
        candles.append({
            "t": timestamps[i],
            "open": float(df["Open"].iloc[i]) if "Open" in df.columns else closes[i],
            "high": float(df["High"].iloc[i]) if "High" in df.columns else closes[i],
            "low": float(df["Low"].iloc[i]) if "Low" in df.columns else closes[i],
            "close": closes[i],
            "volume": float(df["Volume"].iloc[i]) if "Volume" in df.columns else 0.0,
        })
        for out_key, ind_key in keymap.items():
            try:
                v = float(ind[ind_key].iloc[i])
                series[out_key].append(None if v != v else round(v, 4))
            except Exception:
                series[out_key].append(None)
    return candles, series, closes, timestamps


def _replay_strategy(strategy, ticker, df, ind, closes, timestamps, forward_bars, warmup=TUNABLES.backtest_warmup_bars):
    """Run one strategy over the bars, return (markers, stats)."""
    markers: List[dict] = []
    last_action = None
    for i in range(warmup, len(df)):
        snap = _bar_snapshot(df, ind, i)
        try:
            sig = strategy.analyze(ticker, snap)
        except Exception:
            continue
        if sig.action == Action.HOLD or sig.action == last_action:
            continue
        last_action = sig.action
        fwd = None
        if i + forward_bars < len(closes) and closes[i]:
            fwd = round((closes[i + forward_bars] - closes[i]) / closes[i] * 100, 2)
        markers.append({
            "t": timestamps[i], "index": i, "price": round(closes[i], 2),
            "action": sig.action.value, "confidence": round(sig.confidence, 3),
            "reason": sig.reason, "forward_return_pct": fwd,
        })

    buys = [m for m in markers if m["action"].startswith("BUY") and m["forward_return_pct"] is not None]
    sells = [m for m in markers if m["action"].startswith("SELL") and m["forward_return_pct"] is not None]
    buy_hits = sum(1 for m in buys if m["forward_return_pct"] > 0)
    sell_hits = sum(1 for m in sells if m["forward_return_pct"] < 0)
    graded = len(buys) + len(sells)
    hit_rate = (buy_hits + sell_hits) / graded if graded else None
    # "If you took every signal in its direction" cumulative forward return.
    cumulative = sum(
        (m["forward_return_pct"] if m["action"].startswith("BUY") else -m["forward_return_pct"])
        for m in markers if m["forward_return_pct"] is not None
    )
    stats = {
        "marker_count": len(markers),
        "graded": graded,
        "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "cumulative_return_pct": round(cumulative, 2),
        "avg_forward_return_pct": round(cumulative / graded, 2) if graded else None,
    }
    return markers, stats


def _max_drawdown(equity: List[float]) -> float:
    """Worst peak-to-trough decline as a positive percentage."""
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return round(worst * 100, 2)


def _sharpe(returns: List[float]) -> float:
    """Annualised Sharpe from per-bar (daily) returns."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return round((mean / std) * (252 ** 0.5), 2)


def simulate_strategy(
    strategy, ticker, df, ind, closes, timestamps,
    warmup: int = TUNABLES.backtest_warmup_bars,
    starting_equity: float = TUNABLES.backtest_starting_equity,
    commission_bps: float = TUNABLES.backtest_commission_bps,
    *,
    broker: str = "local_paper",
    apply_realistic_costs: bool = True,
) -> Dict[str, Any]:
    """Long/flat equity simulation.

    Goes all-in long on a BUY signal, exits flat on a SELL signal or when the
    signal's stop-loss / take-profit is hit. Tracks an equity curve and books
    each round-trip trade. This is a real (if simple) P&L backtest on the
    underlying — not options-aware, and using close-to-close fills.
    """
    from backend.bot.execution_costs import estimate_total_cost

    comm = commission_bps / 10_000.0
    cash = starting_equity
    shares = 0.0
    entry_px = 0.0
    entry_t = None
    stop = None
    target = None
    entry_cost_dollar = 0.0
    trades: List[dict] = []
    equity_curve: List[dict] = []
    equity_vals: List[float] = []
    last_action = None
    total_costs_dollar = 0.0

    def _bar_snap_for_cost(i: int) -> dict:
        """Tiny snapshot for the cost estimator — uses ATR-ish + volume so
        the spread/slippage models have real inputs to chew on."""
        snap = {"price": float(closes[i]), "volume_avg": float(df["Volume"].iloc[max(0, i - 20):i].mean() or 0)}
        # ATR-ish: average true range from the last 14 bars (approx).
        if "High" in df.columns and i >= 14:
            highs = df["High"].iloc[i - 14:i].to_numpy()
            lows = df["Low"].iloc[i - 14:i].to_numpy()
            snap["atr"] = float((highs - lows).mean())
        return snap

    def close_position(i, reason):
        nonlocal cash, shares, entry_px, entry_t, stop, target, total_costs_dollar, entry_cost_dollar
        price = closes[i]
        # Realistic cost on exit (spread + slippage; commission per broker).
        exit_cost = 0.0
        if apply_realistic_costs and shares > 0 and price > 0:
            est = estimate_total_cost(
                broker=broker, instrument="stock", side="SELL",
                quantity=shares, price=price, snapshot=_bar_snap_for_cost(i),
            )
            exit_cost = est.total
            total_costs_dollar += exit_cost
            proceeds = shares * price - exit_cost
        else:
            proceeds = shares * price * (1 - comm)
        cash += proceeds
        gross_ret_pct = (price - entry_px) / entry_px * 100 if entry_px else 0.0
        round_trip_cost = round(entry_cost_dollar + exit_cost, 4)
        net_dollar = proceeds - (entry_px * shares + entry_cost_dollar) if entry_px else 0.0
        net_ret_pct = (net_dollar / (entry_px * shares) * 100) if entry_px and shares else 0.0
        trades.append({
            "entry_t": entry_t, "entry_px": round(entry_px, 2),
            "exit_t": timestamps[i], "exit_px": round(price, 2),
            "return_pct": round(gross_ret_pct, 2),
            "net_return_pct": round(net_ret_pct, 2),
            "round_trip_cost": round_trip_cost,
            "reason": reason,
            "stop_px": round(stop, 2) if stop else None,
            "target_px": round(target, 2) if target else None,
        })
        entry_cost_dollar = 0.0
        shares = 0.0
        entry_px = 0.0
        entry_t = None
        stop = None
        target = None

    for i in range(len(df)):
        price = closes[i]
        # Manage an open position first (stop / target on close).
        if shares > 0:
            if stop and price <= stop:
                close_position(i, "stop")
            elif target and price >= target:
                close_position(i, "target")

        if i >= warmup:
            snap = _bar_snapshot(df, ind, i)
            try:
                sig = strategy.analyze(ticker, snap)
            except Exception:
                sig = None
            if sig and sig.action != Action.HOLD and sig.action != last_action:
                last_action = sig.action
                act = sig.action.value
                # Don't open on the final bar — there's no future bar to exit
                # on, so the end-of-data force-close would otherwise book a
                # zero-duration trade (entry and exit on the same candle).
                if act.startswith("BUY") and shares == 0 and cash > 0 and price > 0 and i < len(df) - 1:
                    if apply_realistic_costs:
                        # Estimate cost on the FULL cash allocation; pay it
                        # from cash and reduce the resulting share count.
                        est_in = estimate_total_cost(
                            broker=broker, instrument="stock", side="BUY",
                            quantity=cash / price, price=price,
                            snapshot=_bar_snap_for_cost(i),
                        )
                        entry_cost_dollar = est_in.total
                        total_costs_dollar += entry_cost_dollar
                        invest = max(0.0, cash - entry_cost_dollar)
                    else:
                        invest = cash * (1 - comm)
                        entry_cost_dollar = cash - invest
                    shares = invest / price if price else 0.0
                    cash = 0.0
                    entry_px = price
                    entry_t = timestamps[i]
                    stop = price * (1 - sig.stop_loss / 100) if sig.stop_loss else None
                    target = price * (1 + sig.take_profit / 100) if sig.take_profit else None
                elif act.startswith("SELL") and shares > 0:
                    close_position(i, "signal")

        eq = cash + shares * price
        equity_vals.append(eq)
        equity_curve.append({"t": timestamps[i], "equity": round(eq, 2)})

    # Mark-to-market any position still open at the data boundary. This is the
    # current open trade, not a real exit signal — tag it "open" so the chart
    # shows it as "holding" rather than drawing a phantom SELL at the live bar.
    if shares > 0:
        close_position(len(df) - 1, "open")
        equity_vals[-1] = cash
        equity_curve[-1]["equity"] = round(cash, 2)

    # Defensive: never report a zero-duration (same-candle) round-trip.
    trades = [t for t in trades if t["entry_t"] != t["exit_t"]]

    final_equity = equity_vals[-1] if equity_vals else starting_equity
    total_return = (final_equity - starting_equity) / starting_equity * 100
    bh_start = closes[warmup] if len(closes) > warmup else closes[0]
    bh_return = (closes[-1] - bh_start) / bh_start * 100 if bh_start else 0.0

    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] < 0]
    gross_win = sum(t["return_pct"] for t in wins)
    gross_loss = abs(sum(t["return_pct"] for t in losses))
    # Per-bar returns for Sharpe.
    bar_returns = [
        (equity_vals[i] - equity_vals[i - 1]) / equity_vals[i - 1]
        for i in range(1, len(equity_vals)) if equity_vals[i - 1] > 0
    ]
    # Net returns (after Stage-2 realistic costs) — compare gross above vs net.
    net_trades = [t.get("net_return_pct", t["return_pct"]) for t in trades]
    net_wins = [r for r in net_trades if r > 0]
    net_losses = [r for r in net_trades if r < 0]
    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "num_trades": len(trades),
        "total_return_pct": round(total_return, 2),
        "buy_hold_return_pct": round(bh_return, 2),
        "alpha_pct": round(total_return - bh_return, 2),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_win_pct": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (round(gross_win, 2) if gross_win else 0.0),
        "max_drawdown_pct": _max_drawdown(equity_vals),
        "sharpe": _sharpe(bar_returns),
        "final_equity": round(final_equity, 2),
        "starting_equity": starting_equity,
        # Stage-2 cost transparency
        "broker": broker,
        "realistic_costs_applied": apply_realistic_costs,
        "total_costs_dollar": round(total_costs_dollar, 2),
        "net_win_rate": (round(len(net_wins) / len(net_trades), 3)
                          if net_trades else None),
        "net_avg_win_pct": (round(sum(net_wins) / len(net_wins), 2)
                              if net_wins else 0.0),
        "net_avg_loss_pct": (round(sum(net_losses) / len(net_losses), 2)
                                if net_losses else 0.0),
    }


def run_backtest(
    strategy_name: str,
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
    forward_bars: int = TUNABLES.backtest_forward_bars,
    raise_on_unknown: bool = True,
) -> Dict[str, Any]:
    """Replay a strategy over historical candles. Returns chart-ready data.

    On unknown strategy: raises FastAPI ``HTTPException(404)`` by default so
    the frontend's err branch fires cleanly. Internal callers that need the
    legacy "return error dict" behavior can pass ``raise_on_unknown=False``.
    """
    try:
        strategy = _resolve_strategy(strategy_name)
    except ValueError:
        if raise_on_unknown:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=404,
                detail=f"strategy not registered: {strategy_name}",
            )
        return {"error": f"unknown strategy: {strategy_name}"}

    df = fetch_candles(ticker, period=period, interval=interval)
    if df.empty or "Close" not in df.columns:
        return {"error": f"no candle data for {ticker}"}

    ind = compute_indicators(df)
    candles, series, closes, timestamps = _candles_and_series(df, ind)
    markers, stats = _replay_strategy(strategy, ticker, df, ind, closes, timestamps, forward_bars)
    sim = simulate_strategy(strategy, ticker, df, ind, closes, timestamps)
    overlays = compute_overlays(df, ind)

    return {
        "ticker": ticker,
        "strategy": strategy_name,
        "period": period,
        "interval": interval,
        "candles": candles,
        "indicators": series,
        "overlays": overlays,
        "data_quality": get_quality(ticker, period, interval),
        "default_theories": STRATEGY_THEORIES.get(strategy_name, ["support_resistance", "moving_avg"]),
        "markers": markers,
        "marker_count": stats["marker_count"],
        "hit_rate": stats["hit_rate"],
        "cumulative_return_pct": stats["cumulative_return_pct"],
        "forward_bars": forward_bars,
        "backtest": sim,
        "explanation": STRATEGY_EXPLAINERS.get(strategy_name, "Runs the strategy bar-by-bar over historical prices and marks where it would have signalled."),
    }


# Distinct colors so each strategy's markers stand out on the shared chart.
COMPARE_COLORS = [
    "#0e8a5f", "#2c6bd1", "#c026d3", "#b3791b", "#d4485c",
    "#06b6d4", "#6d5efc", "#e0552b", "#1f9e54", "#9333ea",
]


def run_compare(
    ticker: str,
    strategy_names: List[str],
    period: str = "6mo",
    interval: str = "1d",
    forward_bars: int = TUNABLES.backtest_forward_bars,
) -> Dict[str, Any]:
    """Run several strategies over the SAME candles and rank them."""
    df = fetch_candles(ticker, period=period, interval=interval)
    if df.empty or "Close" not in df.columns:
        return {"error": f"no candle data for {ticker}"}
    ind = compute_indicators(df)
    candles, series, closes, timestamps = _candles_and_series(df, ind)

    per_strategy: List[dict] = []
    benchmark = None
    for idx, name in enumerate(strategy_names):
        try:
            strat = _resolve_strategy(name)
        except ValueError:
            continue
        markers, stats = _replay_strategy(strat, ticker, df, ind, closes, timestamps, forward_bars)
        sim = simulate_strategy(strat, ticker, df, ind, closes, timestamps)
        if benchmark is None:
            benchmark = sim["buy_hold_return_pct"]
        per_strategy.append({
            "strategy": name,
            "color": COMPARE_COLORS[idx % len(COMPARE_COLORS)],
            "markers": markers,
            **stats,
            # Real backtest metrics:
            "total_return_pct": sim["total_return_pct"],
            "alpha_pct": sim["alpha_pct"],
            "num_trades": sim["num_trades"],
            "bt_win_rate": sim["win_rate"],
            "profit_factor": sim["profit_factor"],
            "max_drawdown_pct": sim["max_drawdown_pct"],
            "sharpe": sim["sharpe"],
            "equity_curve": sim["equity_curve"],
            "trades": sim["trades"],
            "final_equity": sim["final_equity"],
            "explanation": STRATEGY_EXPLAINERS.get(name, ""),
        })

    # Rank by real total return, tie-broken by Sharpe; no-trade strategies last.
    def score(s):
        if s["num_trades"] == 0:
            return -1e9
        return s["total_return_pct"] * 100 + s["sharpe"]
    ranked = sorted(per_strategy, key=score, reverse=True)

    suggestion = _build_suggestion(ticker, ranked, period, benchmark)
    return {
        "ticker": ticker,
        "period": period,
        "interval": interval,
        "forward_bars": forward_bars,
        "candles": candles,
        "indicators": series,
        "strategies": per_strategy,
        "ranking": [s["strategy"] for s in ranked],
        "buy_hold_return_pct": benchmark,
        "suggestion": suggestion,
    }


def _build_suggestion(ticker: str, ranked: List[dict], period: str, benchmark: Optional[float]) -> Dict[str, Any]:
    """Proactive recommendation using the real backtest P&L numbers."""
    traded = [s for s in ranked if s["num_trades"] > 0]
    silent = [s["strategy"] for s in ranked if s["num_trades"] == 0]
    bh = f"{benchmark:+.1f}%" if benchmark is not None else "n/a"
    if not traded:
        return {
            "headline": f"No strategy placed a trade on {ticker} in the last {period}.",
            "detail": f"Buy & hold would have returned {bh}. Try a longer period, a more active ticker, or different strategies."
                      + (f" No setups for: {', '.join(s.replace('_',' ') for s in silent)}." if silent else ""),
            "best": None,
        }
    best = traded[0]
    beat_bh = benchmark is not None and best["total_return_pct"] > benchmark
    lines = [
        f"Over the last {period} on {ticker}, **{best['strategy'].replace('_',' ')}** had the best backtest: "
        f"**{best['total_return_pct']:+.1f}%** across {best['num_trades']} trades "
        f"({int((best['bt_win_rate'] or 0)*100)}% win-rate, profit factor {best['profit_factor']}, "
        f"max drawdown {best['max_drawdown_pct']}%, Sharpe {best['sharpe']}).",
        f"Buy & hold over the same window: {bh}, so this strategy "
        + (f"**beat** buy-and-hold by {best['alpha_pct']:+.1f}%." if beat_bh else f"**lagged** buy-and-hold ({best['alpha_pct']:+.1f}% alpha)."),
    ]
    if len(traded) > 1:
        runner = traded[1]
        lines.append(
            f"Runner-up: {runner['strategy'].replace('_',' ')} at {runner['total_return_pct']:+.1f}% "
            f"({runner['num_trades']} trades, Sharpe {runner['sharpe']})."
        )
    losers = [s for s in traded if s["total_return_pct"] < 0]
    if losers:
        lines.append("Lost money here (avoid for this ticker/regime): "
                     + ", ".join(s["strategy"].replace("_", " ") for s in losers) + ".")
    if silent:
        lines.append("No setups for: " + ", ".join(s.replace("_", " ") for s in silent)
                     + " — expected for options/catalyst strategies without that data.")
    if not beat_bh:
        lines.append("⚠️ None of these beat simply holding the stock in this window — consider that the honest baseline.")
    return {
        "headline": f"Best on {ticker}: {best['strategy'].replace('_',' ')} ({best['total_return_pct']:+.1f}%)",
        "detail": " ".join(lines),
        "best": best["strategy"],
    }
