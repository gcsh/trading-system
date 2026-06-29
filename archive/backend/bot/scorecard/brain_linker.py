"""MITS Phase 14.D — Nightly BrainPrediction → Trade resolver.

Walks every ``BrainPrediction`` row whose ``outcome == 'pending'`` and:

  1. Finds a matching ``Trade`` (same ticker, same option direction
     inferred from the suggested action, opened within 24 hours of the
     prediction's ``created_at``). Stamps ``linked_trade_id``.
  2. Once the linked trade is closed and has a non-null pnl, sets
     ``actual_pnl_pct`` + resolves ``outcome`` into win / loss / scratch.
  3. Replays bars after ``created_at`` and keyword-matches the
     ``invalidation_json`` bullets against trivial structural triggers
     ("below vwap", "below 50ema/200ema", "loses prior low", "volume
     dries up"). Sets ``invalidation_hit`` and
     ``invalidation_saved_capital`` based on the close at the trigger
     bar vs the linked trade's exit price.

Returns a stats dict for the scheduler log.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.db import session_scope
from backend.models.brain_prediction import (
    BrainPrediction,
    OUTCOME_LOSS,
    OUTCOME_NOT_TRADED,
    OUTCOME_PENDING,
    OUTCOME_SCRATCH,
    OUTCOME_WIN,
)
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# Window during which a trade is considered "caused by" a prediction.
TRADE_MATCH_HOURS = 24

# Below this absolute pnl-pct we mark the outcome as scratch instead of
# win/loss — keeps near-zero noise from polluting the calibration plot.
SCRATCH_THRESHOLD_PCT = 0.005

# Stale-prediction sweep: once a prediction is older than this many days
# and still has no linked trade, resolve it as ``not_traded``.
NOT_TRADED_AFTER_HOURS = 48


_CLOSED_STATES = (
    "closed", "filled_closed",
    "closed_by_exit_manager", "closed_by_thesis_health",
)


def _direction_from_action(action: Optional[str],
                              direction_field: Optional[str]
                              ) -> Optional[str]:
    """Resolve a normalized direction marker for trade matching.

    Returns 'call' or 'put' when the prediction was clearly directional,
    or ``None`` when the action is non-options or absent.
    """
    if direction_field:
        df = direction_field.lower()
        if "call" in df or df == "long_call":
            return "call"
        if "put" in df or df == "long_put":
            return "put"
    if not action:
        return None
    a = action.upper().strip()
    if a == "BUY_CALL":
        return "call"
    if a == "BUY_PUT":
        return "put"
    return None


def _matches_trade(pred: BrainPrediction, trade: Trade) -> bool:
    if (trade.ticker or "").upper() != (pred.ticker or "").upper():
        return False
    if not trade.timestamp:
        return False
    dt = trade.timestamp - pred.created_at
    if dt < timedelta(0):
        return False
    if dt > timedelta(hours=TRADE_MATCH_HOURS):
        return False
    expected_dir = _direction_from_action(
        pred.suggested_action, pred.suggested_direction)
    if expected_dir is None:
        # Non-options prediction → ticker + window is the strongest link.
        return True
    if trade.instrument != "option":
        return False
    return (trade.option_type or "").lower() == expected_dir


def _resolve_outcome_from_pnl(pnl_pct: float) -> str:
    if pnl_pct > SCRATCH_THRESHOLD_PCT:
        return OUTCOME_WIN
    if pnl_pct < -SCRATCH_THRESHOLD_PCT:
        return OUTCOME_LOSS
    return OUTCOME_SCRATCH


def _pnl_pct_from_trade(trade: Trade) -> Optional[float]:
    """Pull pnl as a fractional return relative to entry notional.

    Uses ``Trade.pnl`` as dollar pnl and ``Trade.price * quantity`` or
    ``price * contracts * 100`` as the notional baseline. Returns None
    when the denominator is unusable.
    """
    pnl = trade.pnl
    if pnl is None:
        return None
    try:
        price = float(trade.price or 0.0)
    except Exception:
        return None
    if trade.instrument == "option":
        notional = price * float(trade.contracts or 0) * 100.0
    else:
        notional = price * float(trade.quantity or 0.0)
    if notional <= 0:
        return None
    return float(pnl) / notional


_INVALIDATION_KEYWORDS = [
    ("vwap_break",    re.compile(r"\b(below|under|break(?:s|ing)?)\s+vwap", re.I)),
    ("ema50_break",   re.compile(r"\b(below|under)\s+(?:the\s+)?50[\s-]*(?:ema|d|day)?", re.I)),
    ("ema200_break",  re.compile(r"\b(below|under)\s+(?:the\s+)?200[\s-]*(?:ema|d|day)?", re.I)),
    ("prior_low",     re.compile(r"\b(loses|breaks|below)\s+(?:the\s+)?(?:prior|previous)\s+low", re.I)),
    ("volume_dries",  re.compile(r"\bvolume\s+(?:dries|fades|drops)", re.I)),
]


def _classify_invalidation_bullets(bullets: List[str]) -> List[str]:
    keys: List[str] = []
    for b in bullets:
        for k, rx in _INVALIDATION_KEYWORDS:
            if rx.search(b):
                keys.append(k)
                break
    return keys


def _fetch_bars_after(ticker: str, since: datetime
                        ) -> List[Dict[str, Any]]:
    """Best-effort 1h bars after ``since`` so the linker can replay
    invalidation triggers. Falls back to an empty list when the data
    layer can't service the request — the prediction simply has
    ``invalidation_hit=None`` in that case.
    """
    try:
        from backend.bot.data.bars import fetch_bars
        payload = fetch_bars(ticker, window="5d", interval="1h") or {}
        bars = payload.get("bars") or []
    except Exception:
        logger.debug("brain_linker: bar fetch failed for %s", ticker,
                       exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for b in bars:
        ts_raw = b.get("t") or b.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = (datetime.fromisoformat(ts_raw.replace("Z", ""))
                  if isinstance(ts_raw, str) else ts_raw)
        except Exception:
            continue
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.replace(tzinfo=None)
        since_naive = since.replace(tzinfo=None) if getattr(since, "tzinfo", None) else since
        if ts < since_naive:
            continue
        out.append({
            "timestamp": ts,
            "open": float(b.get("open") or 0.0),
            "high": float(b.get("high") or 0.0),
            "low": float(b.get("low") or 0.0),
            "close": float(b.get("close") or 0.0),
        })
    return out


def _running_vwap(bars: List[Dict[str, Any]]) -> List[float]:
    """Cumulative typical-price VWAP across the supplied bars."""
    vwaps: List[float] = []
    pv = 0.0
    vol = 0.0
    for b in bars:
        typ = (b["high"] + b["low"] + b["close"]) / 3.0
        v = 1.0
        pv += typ * v
        vol += v
        vwaps.append(pv / vol if vol > 0 else typ)
    return vwaps


def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] * (1 - k) + v * k)
    return out


def _trigger_bar(bars: List[Dict[str, Any]], keys: List[str]
                  ) -> Optional[Dict[str, Any]]:
    """Find the first bar at which any of the supplied invalidation
    keys triggers. Returns the bar dict (with ``trigger_key`` added) or
    ``None`` if none trigger across the replay window.
    """
    if not bars or not keys:
        return None
    closes = [b["close"] for b in bars]
    vwaps = _running_vwap(bars) if "vwap_break" in keys else []
    ema50 = _ema(closes, 50) if "ema50_break" in keys else []
    ema200 = _ema(closes, 200) if "ema200_break" in keys else []
    prior_low = bars[0]["low"] if "prior_low" in keys else None
    for i, b in enumerate(bars):
        for k in keys:
            if k == "vwap_break" and vwaps and b["close"] < vwaps[i]:
                return {**b, "trigger_key": k}
            if k == "ema50_break" and ema50 and b["close"] < ema50[i]:
                return {**b, "trigger_key": k}
            if k == "ema200_break" and ema200 and b["close"] < ema200[i]:
                return {**b, "trigger_key": k}
            if k == "prior_low" and prior_low is not None \
                    and b["close"] < prior_low:
                return {**b, "trigger_key": k}
    return None


_COMPONENT_AXES = (
    "regime_call_correct",
    "technical_call_correct",
    "options_call_correct",
    "analog_call_correct",
    "strategy_call_correct",
)


def _score_components(
    pred: BrainPrediction, trade: Optional[Trade],
    bars_after: List[Dict[str, Any]],
) -> Dict[str, Optional[bool]]:
    """MITS Phase 15.E — score each component of the original confidence
    breakdown against realized post-prediction behavior. Returns a dict
    with five keys; each value is ``Optional[bool]`` (None means we
    don't have enough information to evaluate that axis).
    """
    out: Dict[str, Optional[bool]] = {axis: None for axis in _COMPONENT_AXES}

    try:
        regime_blob = (json.loads(pred.regime_at_decision)
                        if pred.regime_at_decision else None)
    except Exception:
        regime_blob = None
    try:
        breakdown = (json.loads(pred.confidence_breakdown_at_decision)
                      if pred.confidence_breakdown_at_decision else None)
    except Exception:
        breakdown = None
    try:
        top_strat = (json.loads(pred.top_strategy_at_decision)
                       if pred.top_strategy_at_decision else None)
    except Exception:
        top_strat = None

    # regime_call_correct — did the trend at decision-time persist over
    # the replay horizon?
    if regime_blob and bars_after:
        trend_at = ((regime_blob.get("trend") or {}).get("value")
                      if isinstance(regime_blob.get("trend"), dict)
                      else None)
        closes = [b["close"] for b in bars_after if b.get("close")]
        if len(closes) >= 3 and trend_at in ("bullish", "bearish", "choppy"):
            ratio = closes[-1] / closes[0] if closes[0] else 1.0
            forward = ("bullish" if ratio > 1.005
                       else "bearish" if ratio < 0.995
                       else "choppy")
            out["regime_call_correct"] = (trend_at == forward)

    # technical_call_correct — pnl sign aligns with suggested direction.
    direction = (pred.suggested_direction or "").lower()
    if direction in ("long_call", "long_put") and pred.actual_pnl_pct is not None:
        out["technical_call_correct"] = pred.actual_pnl_pct > 0

    # options_call_correct — when the options axis was confident at
    # decision time AND we have a closed trade, treat positive pnl as
    # a correct call.
    if breakdown and trade is not None and pred.actual_pnl_pct is not None:
        opts_conf = float(breakdown.get("options") or 0.0)
        if opts_conf >= 0.5:
            out["options_call_correct"] = pred.actual_pnl_pct > 0

    # analog_call_correct — high-historical-analog confidence should
    # correspond to a positive realized pnl.
    if breakdown and pred.actual_pnl_pct is not None:
        ana_conf = float(breakdown.get("historical_analog") or 0.0)
        if ana_conf > 0:
            out["analog_call_correct"] = (
                (pred.actual_pnl_pct > 0) == (ana_conf >= 0.5)
            )

    # strategy_call_correct — when the top StrategyMatrix candidate
    # carried a cohort_win_rate ≥ 0.55 we treat it as a confident strategy
    # call; positive pnl ⇒ the strategy was right.
    if top_strat and trade is not None and pred.actual_pnl_pct is not None:
        cohort_wr = float(top_strat.get("cohort_win_rate") or 0.0)
        if cohort_wr >= 0.55:
            out["strategy_call_correct"] = pred.actual_pnl_pct > 0

    return out


def _evaluate_invalidation(
    pred: BrainPrediction,
    trade: Optional[Trade],
) -> Tuple[Optional[bool], Optional[bool]]:
    """Replay bars after the prediction and return
    ``(invalidation_hit, invalidation_saved_capital)`` or ``(None, None)``
    when we can't evaluate (e.g. no invalidation bullets, no bars).
    """
    if not pred.invalidation_json:
        return (None, None)
    try:
        bullets = json.loads(pred.invalidation_json)
    except Exception:
        return (None, None)
    if not isinstance(bullets, list) or not bullets:
        return (None, None)
    keys = _classify_invalidation_bullets([str(b) for b in bullets])
    if not keys:
        return (None, None)
    bars = _fetch_bars_after(pred.ticker, pred.created_at)
    if not bars:
        return (None, None)
    trigger = _trigger_bar(bars, keys)
    if trigger is None:
        return (False, None)
    # Saved-capital evaluation requires a closed trade with a fill price
    # at exit. When there's no linked trade we can't establish "vs what".
    if trade is None or trade.status not in _CLOSED_STATES:
        return (True, None)
    exit_price = trade.price
    if exit_price is None:
        return (True, None)
    # If the trigger bar's close was higher than the eventual exit price
    # (for calls), the operator-style "would have saved capital" is
    # True. For puts the inequality flips.
    direction = _direction_from_action(
        pred.suggested_action, pred.suggested_direction)
    if direction == "call":
        saved = trigger["close"] > float(exit_price)
    elif direction == "put":
        saved = trigger["close"] < float(exit_price)
    else:
        saved = False
    return (True, bool(saved))


def link_brain_predictions(
    *, since: Optional[datetime] = None,
) -> Dict[str, int]:
    """Run the link / resolve / invalidation pass."""
    stats = {
        "scanned": 0,
        "linked": 0,
        "resolved": 0,
        "not_traded": 0,
        "invalidations_hit": 0,
    }
    cutoff = since or (datetime.utcnow() - timedelta(days=30))
    with session_scope() as s:
        pending = s.execute(
            select(BrainPrediction)
            .where(BrainPrediction.outcome == OUTCOME_PENDING)
            .where(BrainPrediction.created_at >= cutoff)
        ).scalars().all()
        stats["scanned"] = len(pending)
        for pred in pending:
            trade: Optional[Trade] = None
            if pred.linked_trade_id is None:
                candidates = s.execute(
                    select(Trade)
                    .where(Trade.ticker == pred.ticker)
                    .where(Trade.timestamp >= pred.created_at)
                    .where(Trade.timestamp <= pred.created_at
                              + timedelta(hours=TRADE_MATCH_HOURS))
                ).scalars().all()
                for c in candidates:
                    if _matches_trade(pred, c):
                        trade = c
                        pred.linked_trade_id = c.id
                        stats["linked"] += 1
                        break
            else:
                trade = s.get(Trade, pred.linked_trade_id)

            inv_hit, inv_saved = _evaluate_invalidation(pred, trade)
            if inv_hit is not None:
                pred.invalidation_hit = inv_hit
                pred.invalidation_saved_capital = inv_saved
                if inv_hit:
                    stats["invalidations_hit"] += 1

            if trade is not None and trade.status in _CLOSED_STATES \
                    and trade.pnl is not None:
                pnl_pct = _pnl_pct_from_trade(trade)
                if pnl_pct is not None:
                    pred.actual_pnl_pct = pnl_pct
                    pred.outcome = _resolve_outcome_from_pnl(pnl_pct)
                    pred.resolved_at = datetime.utcnow()
                    stats["resolved"] += 1
                    # MITS Phase 15.E — score each component of the
                    # original thesis once we have realized pnl.
                    bars = _fetch_bars_after(pred.ticker, pred.created_at)
                    comp = _score_components(pred, trade, bars)
                    for k, v in comp.items():
                        setattr(pred, k, v)
                    continue

            # MITS Phase 15.E — even when the trade hasn't closed yet
            # the regime call can be evaluated off bars alone.
            bars = _fetch_bars_after(pred.ticker, pred.created_at)
            comp = _score_components(pred, trade, bars)
            for k, v in comp.items():
                if v is not None:
                    setattr(pred, k, v)

            if trade is None and (datetime.utcnow() - pred.created_at) \
                    > timedelta(hours=NOT_TRADED_AFTER_HOURS):
                pred.outcome = OUTCOME_NOT_TRADED
                pred.resolved_at = datetime.utcnow()
                stats["not_traded"] += 1
    return stats


__all__ = ["link_brain_predictions", "_score_components"]
