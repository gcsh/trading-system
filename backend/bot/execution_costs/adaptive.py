"""Stage-10 adaptive spread gating from realized slippage telemetry.

The flat ``TUNABLES.spread_bps_floor`` works for backtesting but ignores
that a ticker's realized slippage drifts over time. This module reads the
ExecutionLog quantiles per (ticker, side?) and returns an adaptive floor
the cost estimator can use instead.

Two surfaces:
  • ``adaptive_spread_floor(ticker, side?)`` — single number for the cost
    estimator to take ``max(floor, atr_pct × mult)`` against
  • ``spread_quantiles(ticker)`` — full p50/p75/p90/p95/p99 for the UI

Falls back to the static ``TUNABLES.spread_bps_floor`` when there aren't
enough observations.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.execution_log import ExecutionLog

logger = logging.getLogger(__name__)

MIN_SAMPLES = 10              # below this we fall back to the static floor
DEFAULT_QUANTILE = 0.75       # p75 is the institutional default


def _quantile(samples: List[float], q: float) -> Optional[float]:
    if not samples:
        return None
    s = sorted(samples)
    idx = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return round(s[idx], 2)


def _load_recent_slippage(ticker: str, side: Optional[str] = None,
                            limit: int = 200) -> List[float]:
    with session_scope() as session:
        q = (select(ExecutionLog)
               .where(ExecutionLog.ticker == ticker.upper())
               .order_by(ExecutionLog.timestamp.desc()).limit(limit))
        if side:
            q = q.where(ExecutionLog.side == side.upper())
        rows = session.execute(q).scalars().all()
        return [abs(float(r.slippage_bps or 0.0)) for r in rows
                  if r.slippage_bps is not None]


def adaptive_spread_floor(ticker: str, *, side: Optional[str] = None,
                            quantile: float = DEFAULT_QUANTILE) -> float:
    """Return the realized slippage at the given quantile, falling back to
    the static floor when we don't have enough samples."""
    samples = _load_recent_slippage(ticker, side=side)
    if len(samples) < MIN_SAMPLES:
        return float(getattr(TUNABLES, "spread_bps_floor", 1.0))
    q_val = _quantile(samples, quantile)
    static_floor = float(getattr(TUNABLES, "spread_bps_floor", 1.0))
    # Never go BELOW the static floor — adaptive only widens, never tightens
    return max(static_floor, float(q_val or static_floor))


def spread_quantiles(ticker: str, side: Optional[str] = None) -> Dict[str, Any]:
    """Return the full slippage quantile bundle for the UI."""
    samples = _load_recent_slippage(ticker, side=side)
    if not samples:
        return {
            "ticker": ticker.upper(),
            "samples": 0,
            "fallback_floor_bps": float(getattr(TUNABLES, "spread_bps_floor", 1.0)),
            "note": "insufficient ExecutionLog samples — using static floor",
        }
    return {
        "ticker": ticker.upper(),
        "side": side,
        "samples": len(samples),
        "p50": _quantile(samples, 0.50),
        "p75": _quantile(samples, 0.75),
        "p90": _quantile(samples, 0.90),
        "p95": _quantile(samples, 0.95),
        "p99": _quantile(samples, 0.99),
        "adaptive_floor_bps": adaptive_spread_floor(ticker, side=side),
        "static_floor_bps": float(getattr(TUNABLES, "spread_bps_floor", 1.0)),
    }
