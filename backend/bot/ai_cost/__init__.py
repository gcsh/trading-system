"""Stage-12.B6 AI Cost telemetry.

Tracks Anthropic API spend per call so we can attribute cost back to
trades / signals / decision points and answer:

  • How much did this trade cost in AI?
  • What's our alpha-per-API-dollar?
  • Which surface (memo, narrative, brain, agents) burns the most spend?

Lightweight in-process accumulator + a SQLite-backed history table for
durable rollups. No retries, no batching — each successful
``messages.create`` call records ``(model, tokens_in, tokens_out, cost,
surface, trade_id)``.

The wrapper is **opt-in**: existing modules continue to call
``client.messages.create(...)`` directly and emit no telemetry. Callers
that want cost tracking call ``record_usage(...)`` after their API call.
This avoids monkey-patching the Anthropic SDK and keeps cost tracking
explicit at the call site.

Pricing constants live in ``PRICING`` below — update when Anthropic ships
new tiers. Numbers in USD per 1M tokens.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# USD per 1M tokens (input, output). Kept conservative — under-billing in
# code is worse than over-billing. Update when Anthropic posts new tiers.
PRICING: Dict[str, Dict[str, float]] = {
    # Opus tier
    "claude-opus-4-7":    {"in": 15.00, "out": 75.00},
    "claude-opus-4-6":    {"in": 15.00, "out": 75.00},
    # Sonnet tier
    "claude-sonnet-4-7":  {"in":  3.00, "out": 15.00},
    "claude-sonnet-4-6":  {"in":  3.00, "out": 15.00},
    # Haiku tier
    "claude-haiku-4-5":   {"in":  0.80, "out":  4.00},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.00},
}

# Default to Sonnet pricing for unknown models — pessimistic but cheap.
_DEFAULT_TIER = {"in": 3.00, "out": 15.00}


@dataclass
class CostEntry:
    ts: str
    surface: str                # memo | narrative | brain | meta_ai | chat | agents | other
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float             # dollars (not cents)
    trade_id: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── in-process ring buffer + lock ────────────────────────────────────────


_LOCK = threading.Lock()
_RING: List[CostEntry] = []
_RING_MAX = 5000
_BY_SURFACE: Dict[str, Dict[str, float]] = defaultdict(
    lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
)


def _price_for(model: str, tokens_in: int, tokens_out: int) -> float:
    tier = PRICING.get(model) or _DEFAULT_TIER
    return round(
        (tokens_in / 1_000_000) * tier["in"]
        + (tokens_out / 1_000_000) * tier["out"],
        6,
    )


def record_usage(*,
                    surface: str,
                    model: str,
                    tokens_in: int,
                    tokens_out: int,
                    trade_id: Optional[int] = None,
                    extra: Optional[Dict[str, Any]] = None) -> CostEntry:
    """Record one API call's cost. Returns the entry (so callers can log
    or attach it to a trade)."""
    cost = _price_for(model, max(0, int(tokens_in)), max(0, int(tokens_out)))
    entry = CostEntry(
        ts=datetime.utcnow().isoformat(),
        surface=str(surface or "other"),
        model=str(model or "unknown"),
        tokens_in=int(tokens_in or 0),
        tokens_out=int(tokens_out or 0),
        cost_usd=cost,
        trade_id=trade_id,
        extra=extra or {},
    )
    with _LOCK:
        _RING.append(entry)
        if len(_RING) > _RING_MAX:
            del _RING[: len(_RING) - _RING_MAX]
        agg = _BY_SURFACE[entry.surface]
        agg["calls"] += 1
        agg["tokens_in"] += entry.tokens_in
        agg["tokens_out"] += entry.tokens_out
        agg["cost_usd"] = round(agg["cost_usd"] + entry.cost_usd, 6)
    return entry


def record_from_response(*,
                            surface: str,
                            model: str,
                            response: Any,
                            trade_id: Optional[int] = None,
                            extra: Optional[Dict[str, Any]] = None) -> Optional[CostEntry]:
    """Convenience: pull usage tokens from an Anthropic response object.

    Anthropic SDK returns ``response.usage = {input_tokens, output_tokens}``.
    We're tolerant if the shape changes — anything missing falls through to 0.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        return record_usage(
            surface=surface, model=model,
            tokens_in=tokens_in, tokens_out=tokens_out,
            trade_id=trade_id, extra=extra,
        )
    except Exception:
        logger.debug("record_from_response failed", exc_info=True)
        return None


# ── rollups for the endpoint + dashboards ────────────────────────────────


def recent_entries(limit: int = 100) -> List[Dict[str, Any]]:
    with _LOCK:
        return [e.to_dict() for e in _RING[-limit:][::-1]]


def by_surface() -> Dict[str, Dict[str, Any]]:
    with _LOCK:
        return {k: dict(v) for k, v in _BY_SURFACE.items()}


def totals() -> Dict[str, Any]:
    with _LOCK:
        calls = sum(v["calls"] for v in _BY_SURFACE.values())
        tokens_in = sum(v["tokens_in"] for v in _BY_SURFACE.values())
        tokens_out = sum(v["tokens_out"] for v in _BY_SURFACE.values())
        cost_usd = round(sum(v["cost_usd"] for v in _BY_SURFACE.values()), 6)
    return {
        "calls": calls, "tokens_in": tokens_in, "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "cost_per_call_usd": (round(cost_usd / calls, 6) if calls else 0.0),
    }


def alpha_per_dollar(*, limit: int = 2000) -> Dict[str, Any]:
    """Cost vs realized P&L. Joins ``CostEntry.trade_id`` to ``Trade.pnl``
    so we can ask "what's our $ profit per $ of API spend?"."""
    try:
        from backend.db import session_scope
        from backend.models.trade import Trade
        from sqlalchemy import select
        with session_scope() as session:
            # P1.2 — ROI = live-P&L / live-cost. Synthetic trades have
            # PnL but no Claude cost, so including them deflates the
            # apparent ROI of the AI brain.
            rows = list(session.execute(
                select(Trade)
                .where(Trade.pnl.is_not(None))
                .where(Trade.status != "closed_by_reset")
                .where(Trade.signal_source != "historical_replay")
                .limit(limit)
            ).scalars().all())
            pnl_by_id = {int(r.id): float(r.pnl) for r in rows}
    except Exception:
        pnl_by_id = {}

    with _LOCK:
        attributed_cost = 0.0
        attributed_pnl = 0.0
        for e in _RING:
            if e.trade_id is None:
                continue
            attributed_cost += e.cost_usd
            if e.trade_id in pnl_by_id:
                attributed_pnl += pnl_by_id[e.trade_id]
    return {
        "attributed_cost_usd": round(attributed_cost, 6),
        "attributed_pnl_usd": round(attributed_pnl, 2),
        "alpha_per_dollar": (round(attributed_pnl / attributed_cost, 2)
                                if attributed_cost > 0 else None),
    }


def reset() -> None:
    """Test helper — clear the ring buffer + surface aggregates."""
    with _LOCK:
        _RING.clear()
        _BY_SURFACE.clear()
