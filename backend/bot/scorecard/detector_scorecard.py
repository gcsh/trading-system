"""MITS Phase 6 (P6.2) — Detector performance scorecard.

Aggregates closed Trade rows per detector. Two views:

  * Per-detector scorecard: trades + win rate + P&L + attribution
    score over a time window.
  * Leaderboard: scorecards for every known detector, sorted by
    attribution score.

Detector → trade mapping is best-effort:

  * If ``Trade.detail_json["eod_bias"]["top_pattern"] == detector``,
    that's the strongest link — the EOD pass committed to that pattern.
  * If ``Trade.detail_json["pattern"] == detector`` (set by upstream
    detector-fired strategies), that counts too.
  * As a fallback, ``Trade.strategy == detector`` covers
    strategy-named-after-detector cases.

This same mapping is what `live_outcome_ingest` uses, so the corpus
and scorecard are aligned.

`attribution_score`: exponentially-decayed P&L sum. A trade closed
``d`` days ago contributes ``pnl * 2 ** (-d / half_life)`` where
``half_life`` is `TUNABLES.detector_attribution_decay_half_life_days`.
This rewards detectors that have been earning recently AND
consistently.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, or_, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# Valid window strings the API accepts.
_VALID_WINDOWS = {"7", "30", "all"}


def _window_to_cutoff(window: str) -> Optional[datetime]:
    """Convert a window keyword to a UTC cutoff datetime, or None for 'all'."""
    if not window:
        return None
    w = str(window).strip().lower()
    if w in {"all", "*"}:
        return None
    try:
        days = int(w)
    except (TypeError, ValueError):
        days = TUNABLES.detector_scorecard_default_window_days
    days = max(1, days)
    return datetime.utcnow() - timedelta(days=days)


def _trade_pattern_keys(trade) -> List[str]:
    """Return every possible detector-name key this trade maps to.

    Accepts either a Trade ORM row (attached to a session) or a plain
    dict copy. We resolve fields by ``getattr`` first then ``[]`` so
    callers don't have to convert.
    """
    keys: List[str] = []
    if not trade:
        return keys

    def _get(name):
        if isinstance(trade, dict):
            return trade.get(name)
        return getattr(trade, name, None)

    strategy = _get("strategy")
    if strategy and isinstance(strategy, str) and strategy.strip():
        keys.append(strategy.strip())
    detail_json = _get("detail_json")
    if detail_json:
        try:
            detail = json.loads(detail_json)
        except Exception:
            detail = {}
        if isinstance(detail, dict):
            eod = detail.get("eod_bias") or {}
            if isinstance(eod, dict):
                tp = eod.get("top_pattern")
                if tp:
                    keys.append(str(tp).strip())
            pat = detail.get("pattern")
            if pat:
                keys.append(str(pat).strip())
    # Dedupe while preserving order.
    out: List[str] = []
    seen: set = set()
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _hold_minutes(trade) -> Optional[float]:
    """Best-effort hold-time in minutes for a closed trade row."""
    if not trade:
        return None
    ts = trade.get("timestamp") if isinstance(trade, dict) \
        else getattr(trade, "timestamp", None)
    if not ts:
        return None
    try:
        delta = datetime.utcnow() - ts
        return max(0.0, delta.total_seconds() / 60.0)
    except Exception:
        return None


def _attribution_weight(trade_ts: datetime,
                                now: datetime,
                                half_life_days: float) -> float:
    """Exponential decay weight. Older trades count less."""
    if not trade_ts:
        return 1.0
    try:
        days = max(0.0, (now - trade_ts).total_seconds() / 86400.0)
    except Exception:
        return 1.0
    if half_life_days <= 0:
        return 1.0
    return math.pow(2.0, -days / half_life_days)


def _fetch_closed_trades(cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
    """Pull every closed Trade with a non-null pnl in the window.

    Returns plain dicts (not ORM rows) so callers can use them without
    a live session. Keys mirror the Trade columns we need:
    timestamp, ticker, pnl, strategy, detail_json, instrument,
    contracts, price, quantity, status.
    """
    out: List[Dict[str, Any]] = []
    with session_scope() as s:
        q = (select(
                  Trade.id, Trade.timestamp, Trade.ticker, Trade.pnl,
                  Trade.strategy, Trade.detail_json, Trade.instrument,
                  Trade.contracts, Trade.price, Trade.quantity,
                  Trade.status,
              )
              .where(Trade.status.in_((
                  "closed", "filled_closed",
                  "closed_by_exit_manager",
                  "closed_by_thesis_health",
              )))
              .where(Trade.pnl.is_not(None)))
        if cutoff is not None:
            q = q.where(Trade.timestamp >= cutoff)
        for row in s.execute(q).all():
            (tid, ts, tkr, pnl, strat, detail_json,
              instrument, contracts, price, quantity, status) = row
            out.append({
                "id": tid, "timestamp": ts, "ticker": tkr, "pnl": pnl,
                "strategy": strat, "detail_json": detail_json,
                "instrument": instrument, "contracts": contracts,
                "price": price, "quantity": quantity, "status": status,
            })
    return out


def build_detector_scorecard(detector_name: str,
                                       window: str = "30") -> Dict[str, Any]:
    """Compute the per-detector scorecard payload for the API."""
    if not detector_name:
        raise ValueError("detector_name required")
    name = detector_name.strip()
    cutoff = _window_to_cutoff(window)
    half_life = float(TUNABLES.detector_attribution_decay_half_life_days)
    now = datetime.utcnow()

    rows = _fetch_closed_trades(cutoff)
    total_trades = 0
    closed_trades = 0
    win_count = 0
    loss_count = 0
    realized_pnl = 0.0
    notional_total = 0.0
    holds: List[float] = []
    attribution = 0.0

    for t in rows:
        keys = _trade_pattern_keys(t)
        if name not in keys:
            continue
        total_trades += 1
        closed_trades += 1
        pnl = float(t.get("pnl") or 0.0)
        realized_pnl += pnl
        if pnl > 0:
            win_count += 1
        elif pnl < 0:
            loss_count += 1
        attribution += pnl * _attribution_weight(
            t.get("timestamp"), now, half_life)
        # Notional baseline for pct return.
        try:
            if t.get("instrument") == "option" and t.get("contracts"):
                notional_total += abs(
                    float(t.get("price") or 0.0)
                    * float(t.get("contracts") or 0) * 100.0)
            else:
                notional_total += abs(
                    float(t.get("price") or 0.0)
                    * float(t.get("quantity") or 0.0))
        except Exception:
            pass
        h = _hold_minutes(t)
        if h is not None:
            holds.append(h)

    win_rate = (win_count / closed_trades) if closed_trades > 0 else None
    avg_hold = (sum(holds) / len(holds)) if holds else None
    realized_pct = (
        realized_pnl / notional_total if notional_total > 0 else None
    )
    return {
        "detector_name": name,
        "window": str(window),
        "window_cutoff": cutoff.isoformat() if cutoff else None,
        "total_trades": total_trades,
        "closed_trades": closed_trades,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": (round(win_rate, 4)
                          if win_rate is not None else None),
        "realized_pnl_dollars": round(realized_pnl, 2),
        "realized_pnl_pct": (round(realized_pct, 4)
                                      if realized_pct is not None else None),
        "avg_hold_minutes": (round(avg_hold, 1)
                                      if avg_hold is not None else None),
        "attribution_score": round(attribution, 4),
        "half_life_days": half_life,
    }


def build_leaderboard(window: str = "30") -> List[Dict[str, Any]]:
    """Walk every detector seen in the registry + every distinct
    pattern referenced by trade.detail_json, build a scorecard for each,
    sort by attribution_score desc.
    """
    names: set = set()
    try:
        from backend.bot.detectors import all_detectors
        for det in all_detectors():
            names.add(det.pattern)
    except Exception:
        logger.debug("detector registry unavailable", exc_info=True)

    # Also include patterns observed in trade rows (so post-Pine
    # imports or strategy-named detectors don't disappear from the
    # leaderboard).
    cutoff = _window_to_cutoff(window)
    rows = _fetch_closed_trades(cutoff)
    for t in rows:
        for k in _trade_pattern_keys(t):
            if k:
                names.add(k)

    out: List[Dict[str, Any]] = []
    for n in sorted(names):
        card = build_detector_scorecard(n, window=window)
        if card.get("closed_trades", 0) == 0:
            # Include zero-trade cards but mark them so the UI can
            # collapse them into a "no trades" group.
            card["status"] = "no_trades"
        else:
            card["status"] = "active"
        out.append(card)
    # Sort: active first, by attribution descending; no-trades last.
    out.sort(key=lambda c: (
        0 if c.get("status") == "active" else 1,
        -float(c.get("attribution_score") or 0.0),
    ))
    return out


def cumulative_pnl_series(detector_name: str,
                                  window: str = "30") -> List[Dict[str, Any]]:
    """Return the cumulative P&L time series for a detector's trades."""
    if not detector_name:
        return []
    name = detector_name.strip()
    cutoff = _window_to_cutoff(window)
    rows = _fetch_closed_trades(cutoff)
    points: List[Tuple[datetime, float]] = []
    for t in rows:
        if name in _trade_pattern_keys(t):
            points.append((t.get("timestamp") or datetime.utcnow(),
                                  float(t.get("pnl") or 0.0)))
    points.sort(key=lambda p: p[0])
    out: List[Dict[str, Any]] = []
    cum = 0.0
    for ts, pnl in points:
        cum += pnl
        out.append({
            "timestamp": ts.isoformat(),
            "pnl": round(pnl, 2),
            "cumulative_pnl": round(cum, 2),
        })
    return out


__all__ = [
    "build_detector_scorecard",
    "build_leaderboard",
    "cumulative_pnl_series",
]
