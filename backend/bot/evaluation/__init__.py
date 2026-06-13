"""Walk-forward evaluation harness — Stage-1 honest backtest.

Splits an ordered sequence of labels into consecutive (train, test) windows so
a model or strategy is **never** scored on data it could have learned from.
This is the only way to honestly measure out-of-sample edge.

Two split strategies are provided:

  • ``walk_forward_split`` — fixed-size training window slides forward
  • ``expanding_split`` — training window grows; common for low-data regimes

The harness is metric-agnostic: pass any callable ``(train, test) -> dict``.
The returned per-window dicts can be aggregated into a stability profile.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from backend.bot.labeling import TradeLabel
from backend.bot.metrics import (
    brier_score,
    calibration_error,
    expectancy,
    profit_factor,
    win_rate,
)

logger = logging.getLogger(__name__)


@dataclass
class WindowResult:
    window_index: int
    train_size: int
    test_size: int
    train_start: Optional[str]
    train_end: Optional[str]
    test_start: Optional[str]
    test_end: Optional[str]
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {**self.__dict__}


# ── split iterators ─────────────────────────────────────────────────────────


def walk_forward_split(labels: Sequence[TradeLabel],
                        train_size: int = 100,
                        test_size: int = 30,
                        step: Optional[int] = None,
                        ) -> Iterator[Tuple[List[TradeLabel], List[TradeLabel]]]:
    """Yield (train, test) tuples with a sliding window of fixed sizes.

    Labels MUST already be sorted by timestamp ascending. ``step`` defaults to
    ``test_size`` (non-overlapping test segments). With 200 labels, train=100,
    test=30 → 4 windows.
    """
    step = step or test_size
    n = len(labels)
    start = 0
    idx = 0
    while start + train_size + test_size <= n:
        train = list(labels[start:start + train_size])
        test = list(labels[start + train_size:start + train_size + test_size])
        yield train, test
        start += step
        idx += 1


def expanding_split(labels: Sequence[TradeLabel],
                     initial_train: int = 50,
                     test_size: int = 20,
                     step: Optional[int] = None,
                     ) -> Iterator[Tuple[List[TradeLabel], List[TradeLabel]]]:
    """Train set grows; useful when total data is small (e.g. early trial)."""
    step = step or test_size
    n = len(labels)
    train_end = initial_train
    while train_end + test_size <= n:
        train = list(labels[0:train_end])
        test = list(labels[train_end:train_end + test_size])
        yield train, test
        train_end += step


# ── evaluators ──────────────────────────────────────────────────────────────


def _window_metrics(test: Sequence[TradeLabel]) -> Dict[str, Any]:
    """Metrics each window gets scored on. Cheap; pure given the labels."""
    pnls = [l.pnl for l in test if l.pnl is not None]
    preds = [l.win_probability for l in test
             if l.win_probability is not None and l.win is not None]
    outs = [l.win for l in test
            if l.win_probability is not None and l.win is not None]
    return {
        "n": len(test),
        "closed": len(pnls),
        "win_rate": win_rate(pnls),
        "expectancy": expectancy(pnls),
        "profit_factor": profit_factor(pnls),
        "brier": brier_score(preds, outs) if preds else None,
        "calibration_error": calibration_error(preds, outs) if preds else None,
        "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
    }


def walk_forward_evaluate(labels: Sequence[TradeLabel],
                           train_size: int = 100,
                           test_size: int = 30,
                           expanding: bool = False,
                           ) -> Dict[str, Any]:
    """Run a walk-forward over the labels and return per-window + aggregated
    metrics. Picks the split mode based on ``expanding``.

    Returns ``{"windows": [...], "summary": {...}, "params": {...}}``. The UI
    can show stability over time + average out-of-sample edge.
    """
    if not labels:
        return {"windows": [], "summary": {}, "params": {
            "train_size": train_size, "test_size": test_size, "expanding": expanding}}

    sorted_labels = sorted(labels, key=lambda l: l.timestamp or "")
    splitter = (expanding_split(sorted_labels, train_size, test_size)
                 if expanding else
                 walk_forward_split(sorted_labels, train_size, test_size))

    windows: List[Dict[str, Any]] = []
    pnls_all: List[float] = []
    wr_all: List[float] = []
    pf_all: List[float] = []
    ece_all: List[float] = []
    for idx, (train, test) in enumerate(splitter):
        result = WindowResult(
            window_index=idx,
            train_size=len(train),
            test_size=len(test),
            train_start=train[0].timestamp if train else None,
            train_end=train[-1].timestamp if train else None,
            test_start=test[0].timestamp if test else None,
            test_end=test[-1].timestamp if test else None,
            metrics=_window_metrics(test),
        )
        windows.append(result.to_dict())
        m = result.metrics
        if m.get("win_rate") is not None:
            wr_all.append(m["win_rate"])
        if isinstance(m.get("profit_factor"), (int, float)):
            pf_all.append(float(m["profit_factor"]))
        if m.get("calibration_error") is not None:
            ece_all.append(m["calibration_error"])
        pnls_all.append(m.get("total_pnl", 0.0))

    summary: Dict[str, Any] = {}
    if windows:
        summary = {
            "n_windows": len(windows),
            "mean_win_rate": round(sum(wr_all) / len(wr_all), 4) if wr_all else None,
            "mean_profit_factor": round(sum(pf_all) / len(pf_all), 4) if pf_all else None,
            "mean_calibration_error": round(sum(ece_all) / len(ece_all), 4) if ece_all else None,
            "cumulative_pnl": round(sum(pnls_all), 2),
            # Stability — fewer windows above 50% win rate ⇒ less robust edge.
            "windows_above_50pct_winrate": sum(1 for w in wr_all if w >= 0.5),
        }
    return {
        "windows": windows,
        "summary": summary,
        "params": {"train_size": train_size, "test_size": test_size,
                    "expanding": expanding},
    }
