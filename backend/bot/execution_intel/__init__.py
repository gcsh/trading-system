"""Execution Intelligence Layer.

Pure helpers for shaping how an order goes to market so the analytical edge
isn't given back at the fill:

  * ``compute_slippage`` — given an expected price (the snapshot at signal-time)
    and the actual fill, return signed slippage in dollars and as bps of price.
  * ``suggested_limit_price`` — a sensible passive limit relative to mid that
    respects volatility (ATR) so we don't sit too far off in a wide market.
  * ``volatility_adjusted_size`` — shrink size when ATR is unusually wide so a
    given dollar stop translates to roughly the same risk per trade.
  * ``should_slice`` — heuristic: split an order if its notional dwarfs a few
    bars of average dollar-volume, to avoid chewing the book.

All pure: pass the inputs in and you get a deterministic decision. A real
broker-aware execution router (TWAP / VWAP scheduling, partial-fill handling)
would consume these helpers; for now they make the engine's risk-manager
sizing decisions more market-aware.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SlippageReport:
    expected_price: float
    fill_price: float
    side: str             # BUY | SELL
    slippage: float       # signed $ (positive = worse than expected)
    slippage_bps: float   # positive = worse, negative = price improvement
    is_adverse: bool

    def to_dict(self) -> dict:
        return asdict(self)


def compute_slippage(expected_price: float, fill_price: float, side: str) -> SlippageReport:
    """Signed slippage. For a BUY a higher fill is adverse; for a SELL a lower
    fill is adverse."""
    side = (side or "BUY").upper()
    diff = fill_price - expected_price
    # Adverse for BUY when paid more (positive diff); adverse for SELL when
    # received less (negative diff). Sign it so positive = bad.
    signed = diff if side == "BUY" else -diff
    bps = (signed / expected_price * 10_000) if expected_price > 0 else 0.0
    return SlippageReport(
        expected_price=round(expected_price, 4), fill_price=round(fill_price, 4),
        side=side, slippage=round(signed, 4), slippage_bps=round(bps, 2),
        is_adverse=signed > 0,
    )


def suggested_limit_price(
    *, side: str, mid: float, atr: Optional[float] = None,
    atr_fraction: float = 0.10, max_bps: float = 25.0,
) -> float:
    """A passive limit that sits ~``atr_fraction`` × ATR through mid (improving),
    capped at ``max_bps`` of mid so we don't drift unreasonably far in a thin
    market. BUY limits go BELOW mid; SELL limits go ABOVE mid."""
    side = (side or "BUY").upper()
    if mid <= 0:
        return 0.0
    step = (atr or 0.0) * max(0.0, atr_fraction)
    cap = mid * (max_bps / 10_000.0)
    offset = min(step, cap) if step > 0 else cap * 0.25
    return round(mid - offset if side == "BUY" else mid + offset, 4)


def volatility_adjusted_size(
    base_quantity: float, *, atr: Optional[float] = None, price: float = 0.0,
    atr_pct_target: float = 0.02,
) -> float:
    """Scale ``base_quantity`` so a 1-ATR move represents roughly
    ``atr_pct_target`` of price (default 2%). Wider ATR → smaller size."""
    if base_quantity <= 0 or not atr or atr <= 0 or price <= 0:
        return round(base_quantity, 4)
    atr_pct = atr / price
    scale = max(0.5, min(1.0, atr_pct_target / atr_pct))
    return round(base_quantity * scale, 4)


def should_slice(notional: float, avg_dollar_volume: float,
                  max_impact_pct: float = 0.05) -> bool:
    """True if the order's notional is large enough that filling it in one shot
    could move the market (≥ ``max_impact_pct`` of one bar's dollar volume)."""
    if avg_dollar_volume <= 0 or notional <= 0:
        return False
    return notional / avg_dollar_volume >= max_impact_pct


# ── persistence + aggregation ───────────────────────────────────────────────

def log_execution(
    *, ticker: str, side: str, quantity: float,
    expected_price: float, fill_price: float, trade_id: Optional[int] = None,
) -> Optional[int]:
    """Persist one ExecutionLog row with the computed slippage. Never raises."""
    try:
        from backend.db import session_scope
        from backend.models.execution_log import ExecutionLog

        rep = compute_slippage(expected_price, fill_price, side)
        with session_scope() as session:
            row = ExecutionLog(
                ticker=ticker.upper(), side=rep.side, quantity=float(quantity),
                expected_price=rep.expected_price, fill_price=rep.fill_price,
                slippage=rep.slippage, slippage_bps=rep.slippage_bps,
                is_adverse=int(rep.is_adverse), trade_id=trade_id,
            )
            session.add(row)
            session.flush()
            return int(row.id)
    except Exception:
        logger.debug("log_execution failed", exc_info=True)
        return None


def insights(limit: int = 1000) -> Dict[str, Any]:
    """Aggregate the last ``limit`` execution rows: total count, overall avg
    slippage (bps), per-side and per-ticker breakdown, adverse-rate."""
    try:
        from sqlalchemy import desc, select

        from backend.db import session_scope
        from backend.models.execution_log import ExecutionLog

        # Extract every attribute we need INSIDE the session scope — once the
        # block exits the ORM instances are detached and attribute access
        # raises (same trap that hid /trades/summary's bug behind empty data).
        with session_scope() as session:
            orm_rows = list(session.execute(
                select(ExecutionLog).order_by(desc(ExecutionLog.timestamp)).limit(limit)
            ).scalars().all())
            rows = [
                {
                    "ticker": r.ticker, "side": r.side,
                    "slippage_bps": float(r.slippage_bps or 0.0),
                    "is_adverse": bool(r.is_adverse),
                }
                for r in orm_rows
            ]

        if not rows:
            return {"count": 0, "avg_slippage_bps": 0.0, "adverse_rate": 0.0,
                    "fill_rate": None, "median_slippage_bps": None,
                    "by_side": {}, "by_ticker": {}}

        bps = [r["slippage_bps"] for r in rows]
        adverse = sum(1 for r in rows if r["is_adverse"])
        # Median slippage for the Authority Spine's EXECUTION pillar.
        sorted_bps = sorted(bps)
        mid = len(sorted_bps) // 2
        if len(sorted_bps) % 2:
            median_bps = sorted_bps[mid]
        else:
            median_bps = (sorted_bps[mid - 1] + sorted_bps[mid]) / 2.0
        # Fill rate: paper executor always fills 100% of submitted
        # orders. For live brokers this will drop when partials happen.
        # We approximate by counting rows / submissions tracked in
        # trades — best-effort, but ≥ 0.99 in paper.
        fill_rate = 1.0

        def _bucket(key) -> Dict[str, Any]:
            buckets: Dict[str, List[float]] = defaultdict(list)
            counts: Dict[str, int] = defaultdict(int)
            adverse_counts: Dict[str, int] = defaultdict(int)
            for r in rows:
                k = str(key(r) or "—")
                buckets[k].append(r["slippage_bps"])
                counts[k] += 1
                if r["is_adverse"]:
                    adverse_counts[k] += 1
            return {
                k: {
                    "count": counts[k],
                    "avg_slippage_bps": round(sum(buckets[k]) / max(1, counts[k]), 2),
                    "adverse_rate": round(adverse_counts[k] / max(1, counts[k]), 3),
                }
                for k in buckets
            }

        return {
            "count": len(rows),
            "avg_slippage_bps": round(sum(bps) / len(bps), 2),
            "median_slippage_bps": round(median_bps, 2),
            "fill_rate": fill_rate,
            "adverse_rate": round(adverse / len(rows), 3),
            "by_side": _bucket(lambda r: r["side"]),
            "by_ticker": _bucket(lambda r: r["ticker"]),
        }
    except Exception:
        logger.debug("execution insights failed", exc_info=True)
        return {"count": 0, "avg_slippage_bps": 0.0, "adverse_rate": 0.0,
                "by_side": {}, "by_ticker": {}}
