"""Performance metrics — Stage-1 measurement foundation.

The numbers in this module are the system's evaluation contract. Every later
stage (execution-cost model, gradient boosting, portfolio optimizer, drift
detection) is judged against these. Keep them honest:

  • Pure given inputs — never reach into the DB
  • Return ``None`` (or NaN-safe value) when there isn't enough data, rather
    than producing a confident-looking 0
  • Annualization factors are explicit, never hidden in constants
  • All formulas have a docstring pointing at the canonical source

Tested in ``tests/unit/test_metrics.py`` against hand-computed sequences.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 252 trading days, ~6.5 hour session ⇒ ~19.5 5-min bars.
TRADING_DAYS_PER_YEAR = 252


# ── basic descriptive stats ─────────────────────────────────────────────────


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: Sequence[float], ddof: int = 1) -> float:
    if len(xs) <= ddof:
        return 0.0
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - ddof)
    return math.sqrt(var)


# ── risk-adjusted return ratios ─────────────────────────────────────────────


def sharpe_ratio(returns: Sequence[float], rf: float = 0.045,
                  periods_per_year: int = TRADING_DAYS_PER_YEAR) -> Optional[float]:
    """Annualized Sharpe — ``(mean(r) - rf/N) / stdev(r) * sqrt(N)``.

    Args:
        returns: per-period returns (e.g. daily, decimal: 0.005 = +0.5%).
        rf: annual risk-free rate (default 4.5%, matches ``TUNABLES.risk_free_rate``).
        periods_per_year: 252 for daily, 52 for weekly, etc.

    Returns None when fewer than 2 observations or zero variance — both cases
    where Sharpe is undefined; the UI should show "n/a" instead of 0.
    """
    if len(returns) < 2:
        return None
    sd = _stdev(returns)
    # Tolerance for float-rounding: `sum((x - mean)**2)` over a constant
    # sequence is mathematically zero but numerically ~1e-34, giving a
    # near-zero (but nonzero) sd. Exact-equality check missed that;
    # caused two pre-existing test_metrics::TestSharpe failures.
    if sd < 1e-10:
        return None
    rf_per_period = rf / periods_per_year
    excess_mean = _mean(returns) - rf_per_period
    return round(excess_mean / sd * math.sqrt(periods_per_year), 4)


def sortino_ratio(returns: Sequence[float], target: float = 0.0,
                   periods_per_year: int = TRADING_DAYS_PER_YEAR) -> Optional[float]:
    """Annualized Sortino — like Sharpe but penalizes only downside deviation.

    Uses the target (typically 0) instead of the per-period mean for the
    downside computation, per the original Sortino definition.
    """
    if len(returns) < 2:
        return None
    downside_sq = [(min(0.0, r - target / periods_per_year)) ** 2 for r in returns]
    downside_var = sum(downside_sq) / len(returns)
    if downside_var == 0:
        return None
    downside_dev = math.sqrt(downside_var)
    excess_mean = _mean(returns) - target / periods_per_year
    return round(excess_mean / downside_dev * math.sqrt(periods_per_year), 4)


# ── drawdown ────────────────────────────────────────────────────────────────


def max_drawdown(equity_curve: Sequence[float]) -> Dict[str, Any]:
    """Peak-to-trough drawdown.

    Returns ``{"dd": abs_dollar_drop, "dd_pct": % off peak, "peak_idx", "trough_idx"}``.
    Empty curve → all-zero result.
    """
    if not equity_curve:
        return {"dd": 0.0, "dd_pct": 0.0, "peak_idx": 0, "trough_idx": 0}
    peak = equity_curve[0]
    peak_idx = 0
    max_dd = 0.0
    max_dd_pct = 0.0
    out_peak = 0
    out_trough = 0
    for i, v in enumerate(equity_curve):
        if v > peak:
            peak = v
            peak_idx = i
        dd = peak - v
        dd_pct = dd / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
            out_peak = peak_idx
            out_trough = i
    return {
        "dd": round(max_dd, 2),
        "dd_pct": round(max_dd_pct, 4),
        "peak_idx": out_peak,
        "trough_idx": out_trough,
    }


# ── trade-level metrics ─────────────────────────────────────────────────────


def win_rate(pnls: Sequence[float]) -> Optional[float]:
    if not pnls:
        return None
    wins = sum(1 for p in pnls if p > 0)
    return round(wins / len(pnls), 4)


def avg_win_loss(pnls: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_w = round(_mean(wins), 2) if wins else None
    avg_l = round(_mean(losses), 2) if losses else None
    return avg_w, avg_l


def profit_factor(pnls: Sequence[float]) -> Optional[float]:
    """Gross profits / |gross losses|. ``inf`` if zero losses, ``None`` if empty."""
    if not pnls:
        return None
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return round(gross_win / gross_loss, 3)


def expectancy(pnls: Sequence[float]) -> Optional[float]:
    """Expected $ P&L per trade — ``win_rate * avg_win - loss_rate * |avg_loss|``."""
    if not pnls:
        return None
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    if not wins and not losses:
        return 0.0
    wr = len(wins) / n
    lr = len(losses) / n
    avg_w = _mean(wins) if wins else 0.0
    avg_l = _mean(losses) if losses else 0.0
    return round(wr * avg_w + lr * avg_l, 2)


# ── calibration: are predicted probabilities honest? ────────────────────────


def brier_score(predictions: Sequence[float], outcomes: Sequence[int]) -> Optional[float]:
    """Mean squared error between predicted probability and binary outcome.

    Brier ∈ [0, 1]; lower is better. 0.25 is the score of a coin flip on a
    50/50 base rate, so for a system claiming edge we want < 0.20.
    """
    if not predictions or len(predictions) != len(outcomes):
        return None
    return round(sum((p - y) ** 2 for p, y in zip(predictions, outcomes)) / len(predictions), 4)


def calibration_curve(predictions: Sequence[float], outcomes: Sequence[int],
                       n_bins: int = 10) -> List[Dict[str, Any]]:
    """Reliability diagram data — predicted prob vs actual hit rate per bin.

    Returns one entry per non-empty bin so the UI can plot "predicted 70% →
    actually won 64%". Calibration error is the population-weighted mean of
    |predicted - actual| (see ``calibration_error``).
    """
    if not predictions or len(predictions) != len(outcomes):
        return []
    bins: List[List[Tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in zip(predictions, outcomes):
        # clamp into [0, 1) so 1.0 falls into the last bin
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[idx].append((p, y))
    out: List[Dict[str, Any]] = []
    for i, b in enumerate(bins):
        if not b:
            continue
        avg_pred = _mean([p for p, _ in b])
        actual = _mean([y for _, y in b])
        out.append({
            "bin": i,
            "lower": round(i / n_bins, 3),
            "upper": round((i + 1) / n_bins, 3),
            "count": len(b),
            "predicted": round(avg_pred, 4),
            "actual": round(actual, 4),
        })
    return out


def calibration_error(predictions: Sequence[float], outcomes: Sequence[int],
                       n_bins: int = 10) -> Optional[float]:
    """Expected Calibration Error (ECE). Population-weighted mean absolute
    distance between predicted prob and actual hit rate across bins."""
    curve = calibration_curve(predictions, outcomes, n_bins=n_bins)
    if not curve:
        return None
    total = sum(b["count"] for b in curve)
    if total == 0:
        return None
    ece = sum(b["count"] * abs(b["predicted"] - b["actual"]) for b in curve) / total
    return round(ece, 4)


# ── trade-record summary ────────────────────────────────────────────────────


@dataclass
class TradeMetrics:
    """One canonical summary the UI + API + tests all consume."""
    count: int = 0
    closed_count: int = 0
    open_count: int = 0
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    total_pnl: float = 0.0
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    profit_factor: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    brier: Optional[float] = None
    calibration_error: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        # JSON-friendly: None stays None; inf serializes as a sentinel string
        if d["profit_factor"] == float("inf"):
            d["profit_factor"] = "inf"
        return d


def summarize(records: Sequence[Dict[str, Any]],
               equity_curve: Optional[Sequence[float]] = None,
               periods_per_year: int = TRADING_DAYS_PER_YEAR) -> TradeMetrics:
    """Aggregate one canonical TradeMetrics from a list of trade dicts.

    Each record must have: ``pnl`` (None for open), ``status`` (closed/open),
    optionally ``win_probability`` (for calibration). The ``equity_curve`` arg
    is the per-period portfolio value — Sharpe/Sortino/max-DD need it.
    """
    pnls = [float(r["pnl"]) for r in records if r.get("pnl") is not None]
    closed = len(pnls)
    open_count = sum(1 for r in records if r.get("pnl") is None)
    avg_w, avg_l = avg_win_loss(pnls)

    # Calibration: pair predicted win_probability with actual win outcome.
    pairs = [(float(r["win_probability"]), 1 if r["pnl"] > 0 else 0)
             for r in records
             if r.get("pnl") is not None and r.get("win_probability") is not None]
    preds = [p for p, _ in pairs]
    outs = [o for _, o in pairs]

    sharpe = sortino = max_dd_pct = None
    if equity_curve and len(equity_curve) > 1:
        rets = [(equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
                for i in range(1, len(equity_curve))
                if equity_curve[i - 1] > 0]
        sharpe = sharpe_ratio(rets, periods_per_year=periods_per_year)
        sortino = sortino_ratio(rets, periods_per_year=periods_per_year)
        max_dd_pct = max_drawdown(equity_curve)["dd_pct"]

    return TradeMetrics(
        count=len(records),
        closed_count=closed,
        open_count=open_count,
        win_rate=win_rate(pnls),
        expectancy=expectancy(pnls),
        total_pnl=round(sum(pnls), 2),
        avg_win=avg_w,
        avg_loss=avg_l,
        profit_factor=profit_factor(pnls),
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_dd_pct,
        brier=brier_score(preds, outs) if preds else None,
        calibration_error=calibration_error(preds, outs) if preds else None,
    )
