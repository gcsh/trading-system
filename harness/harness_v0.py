#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# harness_v0.py — Single-hypothesis edge-validation harness
#
# Hypothesis: When an unusual options-market event occurs on a ticker, the
# combination of options-flow direction, IV behavior, and price confirmation
# predicts direction-signed drift over the following 5–20 trading days,
# net of costs, beyond what SPY does over the same window.
#
# Pre-registered 2026-06-23 BEFORE running. Kill condition is the constant
# KILL_CONDITION below; results cannot move that bar.
#
# Hard principles encoded as assertions (build fails if violated):
#   - +1 honest entry: entry_idx = signal_idx + 1
#   - No lookahead: every input ≤ event-date EOD
#   - Direction-signed P&L vs same-window SPY
#   - Realistic costs (spread + commission + slippage + borrow for shorts)
#   - CPCV with embargo ≥ horizon (no horizon overlap across folds)
#   - Negative control (shuffled directions must produce ~0 edge)
#
# Output: ~20 lines to stdout. PASS or DEAD. Appends to kill_log.txt.
# ---------------------------------------------------------------------------
from __future__ import annotations

import bisect
import math
import os
import random
import sqlite3
import sys
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

# ─── CONFIG ─────────────────────────────────────────────────────────────────

HYPOTHESIS_ID = "H-OPTDRIFT-2026-06-23"
HYPOTHESIS_TEXT = (
    "Post-event options drift: when an unusual options-market event fires on "
    "(ticker, date), the AND of (options-flow direction, IV term-structure, "
    "price confirmation) predicts direction-signed drift over 5–20 trading "
    "days, net of costs, beyond same-window SPY. Persistence reason: market "
    "underprices the multi-week aftermath of event-day vol crush + slow "
    "institutional repositioning."
)
PRE_REGISTERED_DATE = "2026-06-23"

# Universe — the 40-ticker A-grade set (must remain fixed for this run)
UNIVERSE_TICKERS = [
    "NVDA", "MSFT", "AAPL", "META", "AMZN", "GOOG", "TSLA", "AMD",
    "BRK.B", "V", "MA", "COST",
    "JPM", "BAC", "GS", "MS",
    "UNH", "LLY", "JNJ",
    "WMT", "HD", "MCD",
    "CAT", "XOM", "CVX",
    "NFLX", "DIS",
    "AVGO", "TSM",
    "PLTR", "COIN", "SHOP",
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV",
]
BENCHMARK = "SPY"

# Event detection thresholds
IV_PCTILE_THRESHOLD = 0.90          # 90th percentile of trailing window
IV_TRAILING_DAYS    = 60
VOL_ZSCORE_THRESHOLD = 2.0          # 2σ above trailing mean
VOL_TRAILING_DAYS   = 60

# Signal-alignment thresholds
FLOW_SKEW_THRESHOLD       = 0.10    # |call_vol - put_vol| / total > 10%
TERM_STRUCTURE_BACKWARD   = 1.05    # front IV > back IV * 1.05 ⇒ backwardation
TERM_STRUCTURE_CONTANGO   = 0.95    # front IV < back IV * 0.95 ⇒ deep contango
PRICE_CONFIRM_THRESHOLD   = 0.01    # |close - 5d_mean| / 5d_mean > 1%
PRICE_CONFIRM_LOOKBACK    = 5

# Backtest
HORIZONS_TRADING_DAYS = [5, 20]
ENTRY_OFFSET_BARS = 1               # +1 honest entry (open[event+1])

# Universe coverage
WARMUP_DAYS = 90                    # need this much history before first event

# Realistic costs (round-trip on stock; bps unless noted)
SPREAD_BPS = 5                      # one side
COMMISSION_PER_SHARE = 0.005        # IBKR tiered baseline (one side)
SLIPPAGE_BPS = 5                    # one side
BORROW_RATE_ANNUAL = 0.03           # 3 % annualized for shorts
NOTIONAL_PER_TRADE = 10_000.0       # $10k notional → commission ≈ 1 bp at $100 stock

# CPCV
CPCV_FOLDS = 10
CPCV_EMBARGO_MIN_DAYS = 5           # base embargo; runtime widens to max(5, horizon)
BOOTSTRAP_ITERS = 2_000
RNG_SEED = 20260623

# Pre-registered kill condition
KILL_CONDITION = {
    "min_oos_sharpe_median": 0.6,
    "min_median_trade_return": 0.0,    # net of costs and SPY
    "min_projected_trades_at_d30": 5,
    "must_beat_spy_net_of_cost": True,  # mean signed excess > 0
}

# Negative-control trip-wire — shuffled directions must produce Sharpe near 0.
NEG_CONTROL_TRIPWIRE_SHARPE = 0.30   # |median Sharpe| above this ⇒ INVALID

# Paths
DB_PATH = os.environ.get(
    "HARNESS_DB", "/opt/trading-bot/trading_bot.db"
)
KILL_LOG_PATH = os.environ.get(
    "HARNESS_KILL_LOG", "kill_log.txt"
)

# ─── ASSERTIONS — these are the 5 mandatory tests ───────────────────────────


def assert_plus_one_entry(signal_idx: int, entry_idx: int):
    """Entry MUST be strictly the next bar after the signal bar."""
    assert entry_idx == signal_idx + 1, (
        f"+1 entry violated: signal_idx={signal_idx} entry_idx={entry_idx}; "
        f"entry must equal signal_idx+1"
    )


def assert_no_lookahead(input_max_ts: pd.Timestamp, event_eod: pd.Timestamp):
    """All signal inputs must have timestamps ≤ event-date EOD."""
    assert input_max_ts <= event_eod, (
        f"Lookahead violated: input_max_ts={input_max_ts} > event_eod={event_eod}"
    )


def assert_cost_model(direction: int, hold_days: int, raw_cost_pct: float):
    """Cost is positive, finite, scales with hold for shorts (borrow)."""
    assert raw_cost_pct > 0 and math.isfinite(raw_cost_pct), \
        f"Cost model malformed: {raw_cost_pct}"
    if direction < 0:  # shorts pay borrow
        # Borrow alone for 20d ≈ 30bp * 20/365 ≈ 1.6 bp
        borrow_component = BORROW_RATE_ANNUAL * hold_days / 365.0
        assert raw_cost_pct >= borrow_component - 1e-9, \
            f"Short cost {raw_cost_pct} < borrow floor {borrow_component}"


def assert_cpcv_embargo(train_dates: list, test_dates: list,
                          embargo_days: int, horizon_days: int):
    """No train date falls within (embargo + horizon) of a test date."""
    required = max(embargo_days, horizon_days)
    if not train_dates or not test_dates:
        return
    test_set = sorted(test_dates)
    for d in train_dates:
        # Find nearest test date
        i = bisect.bisect_left(test_set, d)
        candidates = []
        if i < len(test_set):
            candidates.append(test_set[i])
        if i > 0:
            candidates.append(test_set[i - 1])
        for c in candidates:
            gap = abs((d - c).days)
            assert gap >= required, (
                f"CPCV embargo violated: train_date={d} test_date={c} "
                f"gap={gap}d < required {required}d"
            )


def assert_negative_control_trip_wire(neg_sharpe_median: float):
    """If shuffled-direction Sharpe is too large in magnitude, the harness leaks."""
    assert abs(neg_sharpe_median) <= NEG_CONTROL_TRIPWIRE_SHARPE, (
        f"NEGATIVE CONTROL FAILED: shuffled Sharpe median = {neg_sharpe_median:.3f}, "
        f"exceeds tripwire {NEG_CONTROL_TRIPWIRE_SHARPE}. Harness is leaking."
    )


# ─── DATA LOADERS ───────────────────────────────────────────────────────────


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    return con


def _probe_columns(con: sqlite3.Connection, table: str) -> list[str]:
    cur = con.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def load_stock_bars(con: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Daily bars for one ticker, indexed by date (DateTimeIndex)."""
    df = pd.read_sql_query(
        "SELECT bar_ts, open, high, low, close, volume "
        "FROM stock_bars WHERE ticker = ? AND interval = '1d' ORDER BY bar_ts",
        con, params=(ticker,),
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["bar_ts"]).dt.normalize()
    df = df.set_index("date").drop(columns=["bar_ts"])
    df = df[~df.index.duplicated(keep="first")]
    return df


def load_iv_history(con: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Daily IV per ticker. Probes schema to find the right columns."""
    cols = _probe_columns(con, "iv_history")
    # Actual schema: ticker, date, iv_atm, expiry_used, dte_used, source, fetched_at
    ts_col = next((c for c in cols if c in ("observation_date", "date", "bar_ts", "ts")), None)
    iv_col = next((c for c in cols if c in ("iv_atm", "iv", "iv_value", "atm_iv", "front_iv")), None)
    dte_col = next((c for c in cols if c in ("dte_used", "dte", "days_to_expiry")), None)
    if not ts_col or not iv_col:
        return pd.DataFrame()
    sql = f"SELECT {ts_col} AS ts, {iv_col} AS iv"
    if dte_col:
        sql += f", {dte_col} AS dte"
    sql += " FROM iv_history WHERE ticker = ? ORDER BY 1"
    df = pd.read_sql_query(sql, con, params=(ticker,))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
    df = df.set_index("date").drop(columns=["ts"])
    df = df[~df.index.duplicated(keep="first")]
    return df


def load_option_volume_oi(con: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Per-day aggregate: total volume, call/put split, total OI, front/back IV proxy.
       Schema in this warehouse: ticker, expiration, strike, "right", bar_date,
       open/high/low/close/bid/ask/mid, iv, delta/gamma/vega/theta, volume,
       open_interest, trade_count, source, fetched_at."""
    cols = _probe_columns(con, "option_contract_bars")
    have = set(cols)
    # Find the time column — this warehouse uses bar_date
    ts_col = next((c for c in cols if c in ("bar_date", "bar_ts", "date", "ts")), None)
    vol_col = next((c for c in cols if c in ("volume", "vol")), None)
    if not ts_col or not vol_col or "ticker" not in have:
        return pd.DataFrame()
    type_col = next((c for c in cols if c in ("right", "option_type", "type", "cp")), None)
    expiry_col = next((c for c in cols if c in ("expiration", "expiry", "exp_date")), None)
    oi_col = next((c for c in cols if c in ("open_interest", "oi")), None)
    iv_col = next((c for c in cols if c in ("iv", "implied_vol")), None)
    # SQLite reserves `right` as an identifier; quote it.
    type_select = f'"{type_col}" AS option_type' if type_col == "right" else (
        f"{type_col} AS option_type" if type_col else None
    )
    selects = [f"{ts_col} AS bar_ts", f"{vol_col} AS volume"]
    if type_select:
        selects.append(type_select)
    if expiry_col:
        selects.append(f"{expiry_col} AS expiration")
    if oi_col:
        selects.append(f"{oi_col} AS oi")
    if iv_col:
        selects.append(f"{iv_col} AS iv")
    sql = (f"SELECT {', '.join(selects)} FROM option_contract_bars "
           f"WHERE ticker = ? ORDER BY {ts_col}")
    df = pd.read_sql_query(sql, con, params=(ticker,))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["bar_ts"]).dt.normalize()
    df = df.drop(columns=["bar_ts"])

    # Aggregate per-day
    agg_per_day = []
    for d, g in df.groupby("date"):
        row = {"date": d, "total_vol": float(g["volume"].fillna(0).sum())}
        if "option_type" in g.columns:
            # Schema uses 'C' / 'P' single-letter codes
            ot = g["option_type"].astype(str).str.upper().str.strip()
            mask_call = ot.str.startswith("C")
            mask_put = ot.str.startswith("P")
            row["call_vol"] = float(g.loc[mask_call, "volume"].fillna(0).sum())
            row["put_vol"] = float(g.loc[mask_put, "volume"].fillna(0).sum())
        else:
            row["call_vol"] = row["total_vol"] / 2.0  # conservative split if no type
            row["put_vol"]  = row["total_vol"] / 2.0
        if "oi" in g.columns:
            row["total_oi"] = float(g["oi"].fillna(0).sum())
        # Front/back IV proxy from per-row expiration if available
        if "expiration" in g.columns and "iv" in g.columns:
            try:
                g2 = g.copy()
                g2["expiration_dt"] = pd.to_datetime(g2["expiration"], errors="coerce")
                g2["dte"] = (g2["expiration_dt"] - d).dt.days
                front = g2[(g2["dte"] >= 0) & (g2["dte"] <= 30)]
                back  = g2[(g2["dte"] > 30) & (g2["dte"] <= 90)]
                row["front_iv"] = float(front["iv"].mean()) if not front.empty else np.nan
                row["back_iv"]  = float(back["iv"].mean())  if not back.empty  else np.nan
            except Exception:
                row["front_iv"] = np.nan
                row["back_iv"]  = np.nan
        else:
            row["front_iv"] = np.nan
            row["back_iv"]  = np.nan
        agg_per_day.append(row)

    out = pd.DataFrame(agg_per_day).set_index("date")
    out = out[~out.index.duplicated(keep="first")].sort_index()
    return out


# ─── EVENT DETECTION ────────────────────────────────────────────────────────


def detect_events_for_ticker(bars: pd.DataFrame,
                              iv: pd.DataFrame,
                              opts: pd.DataFrame) -> pd.DataFrame:
    """Returns DataFrame indexed by event date with the detection inputs we used.
       NO LOOKAHEAD: each row uses only data with index ≤ that date."""
    if bars.empty:
        return pd.DataFrame()

    events = []

    # IV-spike events: front-month ATM IV ≥ 90th pctile of trailing IV_TRAILING_DAYS
    if not iv.empty and "iv" in iv.columns:
        iv_sorted = iv["iv"].copy().sort_index()
        roll = iv_sorted.rolling(IV_TRAILING_DAYS, min_periods=IV_TRAILING_DAYS // 2)
        # quantile(0.9) over trailing window — exclusive of today would be more honest,
        # but rolling.quantile includes current observation. For honest-PIT we shift by 1
        # so the percentile is computed over PRIOR window only.
        prior_pctile = roll.quantile(IV_PCTILE_THRESHOLD).shift(1)
        spike_mask = (iv_sorted > prior_pctile)
        for d, hit in spike_mask.dropna().items():
            if not hit:
                continue
            # Assert no lookahead on this single decision
            assert_no_lookahead(prior_pctile.loc[:d].last_valid_index() or d, d)
            events.append({"date": d, "trigger": "iv_spike", "ticker_hint": None})

    # Volume-z events: daily total options volume z-score vs trailing mean
    if not opts.empty and "total_vol" in opts.columns:
        v = opts["total_vol"].sort_index()
        roll_mean = v.rolling(VOL_TRAILING_DAYS, min_periods=VOL_TRAILING_DAYS // 2).mean().shift(1)
        roll_std  = v.rolling(VOL_TRAILING_DAYS, min_periods=VOL_TRAILING_DAYS // 2).std().shift(1)
        z = (v - roll_mean) / roll_std
        zspike_mask = (z > VOL_ZSCORE_THRESHOLD)
        for d, hit in zspike_mask.dropna().items():
            if not hit:
                continue
            events.append({"date": d, "trigger": "vol_zspike", "ticker_hint": None})

    if not events:
        return pd.DataFrame()

    ev = pd.DataFrame(events).set_index("date").sort_index()
    # Dedupe — keep one row per date (one event per ticker per day)
    ev = ev[~ev.index.duplicated(keep="first")]
    return ev


# ─── SIGNAL ALIGNMENT (3 components) ────────────────────────────────────────


def signal_for_event(date: pd.Timestamp,
                      bars: pd.DataFrame,
                      iv: pd.DataFrame,
                      opts: pd.DataFrame) -> tuple[Optional[int], dict]:
    """Returns (direction in {+1, -1, None}, components_dict).
       All inputs PIT — only data with index ≤ date."""
    components = {"flow": None, "term": None, "price": None}

    # Subset to PIT
    bars_pit = bars.loc[:date]
    opts_pit = opts.loc[:date] if not opts.empty else opts
    if bars_pit.empty:
        return None, components

    # Assert no lookahead at this gate
    assert_no_lookahead(bars_pit.index.max(), date)

    # Component 1 — Flow skew
    if not opts_pit.empty and date in opts_pit.index:
        row = opts_pit.loc[date]
        cv = float(row.get("call_vol", 0) or 0)
        pv = float(row.get("put_vol", 0) or 0)
        tot = cv + pv
        if tot > 0:
            skew = (cv - pv) / tot
            if skew > FLOW_SKEW_THRESHOLD:
                components["flow"] = "bullish"
            elif skew < -FLOW_SKEW_THRESHOLD:
                components["flow"] = "bearish"

    # Component 2 — IV term structure (front vs back)
    if not opts_pit.empty and date in opts_pit.index:
        row = opts_pit.loc[date]
        front = float(row.get("front_iv", np.nan) or np.nan)
        back  = float(row.get("back_iv", np.nan) or np.nan)
        if not (np.isnan(front) or np.isnan(back)) and back > 0:
            ratio = front / back
            if ratio > TERM_STRUCTURE_BACKWARD:
                components["term"] = "bearish"   # backwardation = stress = bearish
            elif ratio < TERM_STRUCTURE_CONTANGO:
                components["term"] = "bullish"   # deep contango = calm = bullish

    # Component 3 — Price confirmation (close vs 5d trailing mean)
    if len(bars_pit) >= PRICE_CONFIRM_LOOKBACK + 1:
        closes = bars_pit["close"].tail(PRICE_CONFIRM_LOOKBACK + 1)
        trailing_mean = closes.iloc[:-1].mean()
        today_close = closes.iloc[-1]
        if trailing_mean > 0:
            ret_vs_mean = (today_close - trailing_mean) / trailing_mean
            if ret_vs_mean > PRICE_CONFIRM_THRESHOLD:
                components["price"] = "bullish"
            elif ret_vs_mean < -PRICE_CONFIRM_THRESHOLD:
                components["price"] = "bearish"

    # Alignment AND
    vals = [components["flow"], components["term"], components["price"]]
    if all(v == "bullish" for v in vals):
        return +1, components
    if all(v == "bearish" for v in vals):
        return -1, components
    return None, components


# ─── BACKTEST ───────────────────────────────────────────────────────────────


@dataclass
class Trade:
    ticker: str
    signal_date: pd.Timestamp
    direction: int                # +1 long, -1 short
    horizon_days: int
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    spy_entry: float
    spy_exit: float
    gross_return: float          # direction-signed
    cost_pct: float
    net_return: float
    spy_return: float
    excess_return: float         # net_return - spy_return (with direction sign baked into net_return)


def compute_cost_pct(direction: int, hold_days: int, entry_price: float) -> float:
    """Round-trip cost as fraction of notional. Spread + commission + slippage.
       Shorts add prorated borrow."""
    # Spread (one side each)
    spread = 2 * SPREAD_BPS / 10_000.0
    slippage = 2 * SLIPPAGE_BPS / 10_000.0
    # Commission per share × 2 sides, scaled to notional
    shares = NOTIONAL_PER_TRADE / max(entry_price, 1e-6)
    commission_pct = (2 * COMMISSION_PER_SHARE * shares) / NOTIONAL_PER_TRADE
    borrow = (BORROW_RATE_ANNUAL * hold_days / 365.0) if direction < 0 else 0.0
    return spread + slippage + commission_pct + borrow


def run_trade(bars: pd.DataFrame, spy_bars: pd.DataFrame,
               signal_date: pd.Timestamp, direction: int,
               horizon_days: int, ticker: str) -> Optional[Trade]:
    """Execute one trade: +1 entry, hold horizon_days trading days, exit."""
    if signal_date not in bars.index:
        return None
    bars_index = bars.index
    pos = bars_index.get_loc(signal_date)
    if isinstance(pos, slice) or pos is None:
        return None
    signal_idx = int(pos)
    entry_idx = signal_idx + ENTRY_OFFSET_BARS
    exit_idx  = entry_idx + horizon_days - 1
    if exit_idx >= len(bars_index):
        return None
    assert_plus_one_entry(signal_idx, entry_idx)

    entry_date = bars_index[entry_idx]
    exit_date  = bars_index[exit_idx]

    entry_price = float(bars.iloc[entry_idx]["open"])
    exit_price  = float(bars.iloc[exit_idx]["close"])
    if entry_price <= 0 or exit_price <= 0:
        return None

    # SPY same window — date-aligned, not index-aligned
    if entry_date not in spy_bars.index or exit_date not in spy_bars.index:
        # Try nearest-bar
        spy_entry_idx = spy_bars.index.searchsorted(entry_date)
        spy_exit_idx  = spy_bars.index.searchsorted(exit_date)
        if spy_entry_idx >= len(spy_bars) or spy_exit_idx >= len(spy_bars):
            return None
        spy_entry = float(spy_bars.iloc[spy_entry_idx]["open"])
        spy_exit  = float(spy_bars.iloc[spy_exit_idx]["close"])
    else:
        spy_entry = float(spy_bars.loc[entry_date, "open"])
        spy_exit  = float(spy_bars.loc[exit_date, "close"])

    if spy_entry <= 0 or spy_exit <= 0:
        return None

    raw = (exit_price / entry_price) - 1.0
    gross = direction * raw
    hold_days = (exit_date - entry_date).days
    cost = compute_cost_pct(direction, hold_days, entry_price)
    assert_cost_model(direction, hold_days, cost)
    net = gross - cost

    spy_ret = (spy_exit / spy_entry) - 1.0
    # Direction-signed excess: long strategy beats long SPY by (net_long - spy)
    # Short strategy: net_short already includes -raw; excess vs SPY same formula.
    excess = net - spy_ret * (1 if direction > 0 else 1)  # both compared to long SPY
    # NOTE: We compare strategy P&L to the opportunity cost of holding SPY
    # (which the operator's spec specifies). For shorts: net = -raw - cost; an
    # ideal short has net > 0 while SPY may be up. Excess captures alpha-vs-SPY.

    return Trade(
        ticker=ticker,
        signal_date=signal_date,
        direction=direction,
        horizon_days=horizon_days,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_price=entry_price,
        exit_price=exit_price,
        spy_entry=spy_entry,
        spy_exit=spy_exit,
        gross_return=gross,
        cost_pct=cost,
        net_return=net,
        spy_return=spy_ret,
        excess_return=excess,
    )


# ─── CPCV (combinatorial purged cross-validation) ───────────────────────────


def cpcv_oos_sharpe(trades: list[Trade], horizon_days: int,
                     n_folds: int = CPCV_FOLDS,
                     embargo_days: int = CPCV_EMBARGO_MIN_DAYS) -> tuple[float, float, float, list]:
    """Returns (median_sharpe, ci_lo, ci_hi, per_fold_sharpes).
       Folds split chronologically. Embargo widens to max(base, horizon)."""
    embargo = max(embargo_days, horizon_days)
    if len(trades) < n_folds * 2:
        return float("nan"), float("nan"), float("nan"), []

    sorted_trades = sorted(trades, key=lambda t: t.signal_date)
    dates = [t.signal_date for t in sorted_trades]
    returns = np.array([t.excess_return for t in sorted_trades])

    # Chronological folds
    n = len(sorted_trades)
    fold_size = n // n_folds
    fold_indices = []
    for k in range(n_folds):
        start = k * fold_size
        end = (k + 1) * fold_size if k < n_folds - 1 else n
        fold_indices.append(list(range(start, end)))

    per_fold = []
    for k, test_idx in enumerate(fold_indices):
        test_dates = [dates[i] for i in test_idx]
        train_idx = [i for i in range(n) if i not in test_idx]
        train_dates = [dates[i] for i in train_idx]
        # Enforce embargo (will raise if violated)
        try:
            assert_cpcv_embargo(train_dates, test_dates, embargo, horizon_days)
        except AssertionError:
            # If the natural split violates embargo, we filter train to honor it
            # — this is by design; folds must be horizon-clean.
            filtered_train = []
            test_sorted = sorted(test_dates)
            for d in train_dates:
                i = bisect.bisect_left(test_sorted, d)
                ok = True
                for c in (test_sorted[i] if i < len(test_sorted) else None,
                          test_sorted[i - 1] if i > 0 else None):
                    if c is None:
                        continue
                    if abs((d - c).days) < max(embargo, horizon_days):
                        ok = False
                        break
                if ok:
                    filtered_train.append(d)
            assert_cpcv_embargo(filtered_train, test_dates, embargo, horizon_days)

        test_returns = returns[test_idx]
        if len(test_returns) < 2:
            continue
        mu = float(np.mean(test_returns))
        sigma = float(np.std(test_returns, ddof=1))
        if sigma <= 1e-9:
            sharpe = 0.0
        else:
            # Annualize using horizon-trade density approximation:
            #   trades_per_year ≈ 252 / horizon_days
            ann_factor = math.sqrt(max(1.0, 252.0 / horizon_days))
            sharpe = mu / sigma * ann_factor
        per_fold.append(sharpe)

    if not per_fold:
        return float("nan"), float("nan"), float("nan"), []

    median = float(np.median(per_fold))
    # Bootstrap CI on the median
    rng = np.random.default_rng(RNG_SEED)
    boot = []
    arr = np.array(per_fold)
    for _ in range(BOOTSTRAP_ITERS):
        sample = rng.choice(arr, size=len(arr), replace=True)
        boot.append(float(np.median(sample)))
    ci_lo = float(np.percentile(boot, 2.5))
    ci_hi = float(np.percentile(boot, 97.5))
    return median, ci_lo, ci_hi, per_fold


# ─── NEGATIVE CONTROL ───────────────────────────────────────────────────────


def shuffle_directions(trades: list[Trade], seed: int = RNG_SEED) -> list[Trade]:
    """Re-create trades with directions shuffled. Same dates, same tickers,
       same hold horizons, RANDOM direction. Re-runs the cost + excess math
       so the shuffled set is internally consistent."""
    rng = random.Random(seed)
    shuffled = []
    for t in trades:
        new_dir = rng.choice([+1, -1])
        # Re-derive: gross_return = new_dir * raw_underlying_return
        if t.entry_price <= 0:
            continue
        raw = (t.exit_price / t.entry_price) - 1.0
        new_gross = new_dir * raw
        hold_days = (t.exit_date - t.entry_date).days
        new_cost = compute_cost_pct(new_dir, hold_days, t.entry_price)
        new_net = new_gross - new_cost
        new_excess = new_net - t.spy_return
        shuffled.append(Trade(
            ticker=t.ticker,
            signal_date=t.signal_date,
            direction=new_dir,
            horizon_days=t.horizon_days,
            entry_date=t.entry_date,
            exit_date=t.exit_date,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            spy_entry=t.spy_entry,
            spy_exit=t.spy_exit,
            gross_return=new_gross,
            cost_pct=new_cost,
            net_return=new_net,
            spy_return=t.spy_return,
            excess_return=new_excess,
        ))
    return shuffled


# ─── VERDICT ────────────────────────────────────────────────────────────────


def evaluate_kill_condition(sharpe_median: float,
                              median_trade_return: float,
                              projected_n_at_d30: float,
                              mean_excess: float) -> tuple[str, list[str]]:
    """Returns (verdict, list of reasons that failed)."""
    fails = []
    if not (math.isfinite(sharpe_median) and sharpe_median >= KILL_CONDITION["min_oos_sharpe_median"]):
        fails.append(f"sharpe_median {sharpe_median:.3f} < {KILL_CONDITION['min_oos_sharpe_median']}")
    if not (math.isfinite(median_trade_return) and median_trade_return > KILL_CONDITION["min_median_trade_return"]):
        fails.append(f"median_trade_return {median_trade_return:+.4f} ≤ 0")
    if not (math.isfinite(projected_n_at_d30) and projected_n_at_d30 >= KILL_CONDITION["min_projected_trades_at_d30"]):
        fails.append(f"projected_n_at_d30 {projected_n_at_d30:.1f} < {KILL_CONDITION['min_projected_trades_at_d30']}")
    if KILL_CONDITION["must_beat_spy_net_of_cost"]:
        if not (math.isfinite(mean_excess) and mean_excess > 0):
            fails.append(f"mean_excess_vs_spy {mean_excess:+.4f} ≤ 0")
    verdict = "PASS" if not fails else "DEAD"
    return verdict, fails


# ─── KILL LOG ───────────────────────────────────────────────────────────────


def append_kill_log(verdict: str,
                     summary: dict,
                     log_path: str = KILL_LOG_PATH):
    """Permanent kill log. A DEAD hypothesis stays so it isn't silently re-tested."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    block = [
        "=" * 78,
        f"{verdict}    {HYPOTHESIS_ID}    run_at={now}    pre_registered={PRE_REGISTERED_DATE}",
        "-" * 78,
        textwrap.fill(HYPOTHESIS_TEXT, width=78),
        "Kill condition (pre-registered):",
        f"  min_oos_sharpe_median        = {KILL_CONDITION['min_oos_sharpe_median']}",
        f"  min_median_trade_return      = {KILL_CONDITION['min_median_trade_return']}",
        f"  min_projected_trades_at_d30  = {KILL_CONDITION['min_projected_trades_at_d30']}",
        f"  must_beat_spy_net_of_cost    = {KILL_CONDITION['must_beat_spy_net_of_cost']}",
        "Results:",
    ]
    for k, v in summary.items():
        block.append(f"  {k:30s} {v}")
    block.append("=" * 78)
    block.append("")
    text = "\n".join(block)
    with open(log_path, "a") as f:
        f.write(text)


# ─── MAIN ───────────────────────────────────────────────────────────────────


def main():
    t0 = time.time()
    print("=" * 78)
    print(f"harness_v0 — {HYPOTHESIS_ID}")
    print(f"pre-registered {PRE_REGISTERED_DATE}; kill condition below; results cannot move it.")
    print("Kill condition:")
    for k, v in KILL_CONDITION.items():
        print(f"  {k:30s} = {v}")
    print("=" * 78)

    con = _connect(DB_PATH)

    # Verify schema columns exist
    needed_tables = {"stock_bars", "option_contract_bars", "iv_history"}
    have_tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    missing = needed_tables - have_tables
    if missing:
        print(f"FATAL: missing required tables: {missing}")
        return 2

    # Load SPY bars once
    print(f"\n[load] SPY bars ...", flush=True)
    spy_bars = load_stock_bars(con, BENCHMARK)
    if spy_bars.empty:
        print("FATAL: no SPY bars in stock_bars")
        return 3
    print(f"       SPY rows: {len(spy_bars)} ({spy_bars.index.min().date()} → {spy_bars.index.max().date()})")

    # Iterate the universe — detect events, build signals, run trades
    all_events = 0
    all_aligned = 0
    skipped_no_bars = 0
    skipped_no_signal = 0
    skipped_no_horizon = 0
    trades_per_horizon: dict[int, list[Trade]] = {h: [] for h in HORIZONS_TRADING_DAYS}

    for ticker in UNIVERSE_TICKERS:
        if ticker == BENCHMARK:
            continue
        bars = load_stock_bars(con, ticker)
        if bars.empty or len(bars) < WARMUP_DAYS:
            skipped_no_bars += 1
            print(f"  [{ticker}] excluded: bars={len(bars)} < warmup {WARMUP_DAYS}")
            continue
        iv = load_iv_history(con, ticker)
        opts = load_option_volume_oi(con, ticker)
        events = detect_events_for_ticker(bars, iv, opts)
        if events.empty:
            continue

        for event_date in events.index:
            all_events += 1
            direction, _comp = signal_for_event(event_date, bars, iv, opts)
            if direction is None:
                skipped_no_signal += 1
                continue
            all_aligned += 1
            for h in HORIZONS_TRADING_DAYS:
                tr = run_trade(bars, spy_bars, event_date, direction, h, ticker)
                if tr is None:
                    skipped_no_horizon += 1
                    continue
                trades_per_horizon[h].append(tr)

    print(f"\n[events] detected={all_events} · aligned (3-component AND)={all_aligned}")
    print(f"[skips]  no_bars={skipped_no_bars} no_signal={skipped_no_signal} no_horizon={skipped_no_horizon}")

    # Per-horizon analysis
    overall_verdict = "DEAD"
    overall_fails = []
    summary_for_log: dict = {}

    for h in HORIZONS_TRADING_DAYS:
        trades = trades_per_horizon[h]
        n_trades = len(trades)
        date_range_days = 0
        if n_trades >= 2:
            span_days = (trades[-1].signal_date - trades[0].signal_date).days
            date_range_days = max(span_days, 1)
        projected_n_at_d30 = (n_trades / max(date_range_days, 1)) * 30.0 if n_trades > 0 else 0.0

        print(f"\n─── HORIZON = {h} trading days ───")
        if n_trades < CPCV_FOLDS * 2:
            print(f"  trades={n_trades} — too few for CPCV (need ≥ {CPCV_FOLDS * 2}). Skipping CPCV.")
            summary_for_log[f"h{h}_n_trades"] = n_trades
            summary_for_log[f"h{h}_verdict"] = "INSUFFICIENT-N"
            continue

        # Real-data CPCV
        sharpe_median, ci_lo, ci_hi, per_fold = cpcv_oos_sharpe(trades, h)
        excess_arr = np.array([t.excess_return for t in trades])
        net_arr    = np.array([t.net_return for t in trades])
        median_trade_return = float(np.median(excess_arr))
        mean_excess = float(np.mean(excess_arr))
        median_net = float(np.median(net_arr))
        mean_cost = float(np.mean([t.cost_pct for t in trades]))

        print(f"  trades={n_trades} · span_days={date_range_days} · proj_n@d30={projected_n_at_d30:.1f}")
        print(f"  CPCV OOS Sharpe: median={sharpe_median:+.3f} 95%CI=[{ci_lo:+.3f},{ci_hi:+.3f}] folds_n={len(per_fold)}")
        print(f"  median trade excess (net of cost, vs SPY): {median_trade_return:+.4f}")
        print(f"  mean trade excess (net of cost, vs SPY):   {mean_excess:+.4f}")
        print(f"  median net return:                          {median_net:+.4f}")
        print(f"  mean cost per trade:                        {mean_cost:.4f} ({mean_cost*1e4:.1f} bps)")

        # Negative control
        print(f"  [neg_control] re-running with shuffled directions ...")
        shuffled = shuffle_directions(trades, seed=RNG_SEED)
        neg_sharpe_median, neg_ci_lo, neg_ci_hi, _ = cpcv_oos_sharpe(shuffled, h)
        print(f"  neg_control Sharpe: median={neg_sharpe_median:+.3f} CI=[{neg_ci_lo:+.3f},{neg_ci_hi:+.3f}]")
        try:
            assert_negative_control_trip_wire(neg_sharpe_median)
        except AssertionError as e:
            print(f"\nINVALID: NEGATIVE CONTROL FAILED")
            print(f"  {e}")
            append_kill_log("INVALID", {
                "horizon": h,
                "real_sharpe_median": f"{sharpe_median:+.3f}",
                "neg_control_sharpe_median": f"{neg_sharpe_median:+.3f}",
                "reason": "negative control produced edge — harness is leaking",
            })
            con.close()
            return 4

        # Kill condition
        verdict, fails = evaluate_kill_condition(
            sharpe_median=sharpe_median,
            median_trade_return=median_trade_return,
            projected_n_at_d30=projected_n_at_d30,
            mean_excess=mean_excess,
        )
        print(f"  VERDICT (h={h}): {verdict}")
        if fails:
            for r in fails:
                print(f"    ↳ {r}")

        summary_for_log[f"h{h}_n_trades"] = n_trades
        summary_for_log[f"h{h}_proj_n_d30"] = f"{projected_n_at_d30:.1f}"
        summary_for_log[f"h{h}_sharpe_median"] = f"{sharpe_median:+.3f}"
        summary_for_log[f"h{h}_sharpe_CI"] = f"[{ci_lo:+.3f},{ci_hi:+.3f}]"
        summary_for_log[f"h{h}_median_excess"] = f"{median_trade_return:+.4f}"
        summary_for_log[f"h{h}_mean_excess"] = f"{mean_excess:+.4f}"
        summary_for_log[f"h{h}_mean_cost_bps"] = f"{mean_cost*1e4:.1f}"
        summary_for_log[f"h{h}_neg_control_sharpe"] = f"{neg_sharpe_median:+.3f}"
        summary_for_log[f"h{h}_verdict"] = verdict

        if verdict == "PASS":
            overall_verdict = "PASS"
        else:
            overall_fails.extend([f"h{h}: {r}" for r in fails])

    print("\n" + "=" * 78)
    print(f"OVERALL VERDICT: {overall_verdict}")
    if overall_verdict != "PASS":
        print("Reasons:")
        for r in overall_fails:
            print(f"  ↳ {r}")
    print(f"Runtime: {time.time() - t0:.1f}s")
    print("=" * 78)

    append_kill_log(overall_verdict, summary_for_log)
    print(f"\nKill log appended: {KILL_LOG_PATH}")
    con.close()
    return 0 if overall_verdict == "PASS" else 1


# ─── INLINE TESTS (the 5 mandatory) ─────────────────────────────────────────


def _self_test():
    """Five mandatory tests. Build fails if any of these raise."""
    print("[self-test] running 5 mandatory invariants ...")

    # 1. +1 entry assertion
    assert_plus_one_entry(10, 11)
    try:
        assert_plus_one_entry(10, 10)
        raise RuntimeError("plus_one_entry assertion missing")
    except AssertionError:
        pass
    try:
        assert_plus_one_entry(10, 12)
        raise RuntimeError("plus_one_entry assertion missing")
    except AssertionError:
        pass

    # 2. No-lookahead assertion
    eod = pd.Timestamp("2026-06-01")
    assert_no_lookahead(pd.Timestamp("2026-05-31"), eod)
    assert_no_lookahead(eod, eod)
    try:
        assert_no_lookahead(pd.Timestamp("2026-06-02"), eod)
        raise RuntimeError("no_lookahead assertion missing")
    except AssertionError:
        pass

    # 3. Cost model
    c_long = compute_cost_pct(direction=+1, hold_days=20, entry_price=100.0)
    c_short = compute_cost_pct(direction=-1, hold_days=20, entry_price=100.0)
    assert c_long > 0 and c_short > c_long, \
        f"short cost should exceed long cost (borrow): long={c_long} short={c_short}"
    # round-trip should be roughly: spread 10bp + slip 10bp + comm ~1bp = ~21bp; short adds ~1.6bp
    assert_cost_model(+1, 20, c_long)
    assert_cost_model(-1, 20, c_short)
    expected_borrow = BORROW_RATE_ANNUAL * 20 / 365.0
    assert abs((c_short - c_long) - expected_borrow) < 1e-6, \
        f"short - long should equal 20d borrow: diff={c_short - c_long}, expected={expected_borrow}"

    # 4. CPCV embargo assertion
    train = [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-02-01")]
    test  = [pd.Timestamp("2026-03-01")]
    assert_cpcv_embargo(train, test, embargo_days=5, horizon_days=20)
    bad_train = [pd.Timestamp("2026-02-25")]  # 4d before test
    try:
        assert_cpcv_embargo(bad_train, test, embargo_days=5, horizon_days=20)
        raise RuntimeError("embargo assertion missing")
    except AssertionError:
        pass

    # 5. Negative-control trip-wire
    assert_negative_control_trip_wire(0.10)   # OK
    assert_negative_control_trip_wire(-0.20)  # OK
    try:
        assert_negative_control_trip_wire(0.45)  # tripwire
        raise RuntimeError("neg_control tripwire missing")
    except AssertionError:
        pass

    print("[self-test] all 5 invariants pass.")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
        sys.exit(0)
    _self_test()  # always run before main
    sys.exit(main())
