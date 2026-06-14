"""MITS Phase 3 — per-stock analysis page backend.

Endpoint:
  GET /analysis/{ticker}?window=today|5d|all

Returns bars + detector hits + per-pattern knowledge cells + an
AI-composed thesis paragraph + (when posterior is strong) a suggested
options setup. The thesis is composed via ONE Claude call per
(ticker, window) and cached in-process for 15 minutes so navigating
back and forth on the analysis page doesn't burn AI budget.

Schema (response body):
    {
      "ticker": "NVDA",
      "window": "today",
      "bars": [{"t": ISO, "open": .., "high": .., ...}, ...],
      "observations": [
          {"timestamp": ISO, "pattern": "bull_flag", "family":
           "candlesticks", "regime": "trending_up",
           "vol_state": "normal"}, ...
      ],
      "knowledge": {
          "<pattern>": {
              "sample_size": 347,
              "posterior_win_rate": 0.71,
              "win_rate": 0.69,
              "confidence_band": [0.62, 0.78],
              "avg_return_pct": 2.4,
              "avg_hold_minutes": 84,
              "similar_outcomes": [...]
          }
      },
      "theses": {
          "<pattern>": {
              "headline": "Bull Flag on NVDA in trending_up regime — 71% historical win rate (N=347)",
              "thesis_paragraph": "<AI text>",
              "suggested_action": {...} | null,
              "invalidation": [...],
          }
      },
      "summary": "<2-sentence overall summary>"
    }

Caching: a single Claude call composes the per-pattern theses + the
overall summary. Cache key: (ticker, window). Cache TTL: 15 min.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from sqlalchemy import desc, func, select

from backend.bot.analysis import compose_hybrid
from backend.bot.analysis._actions import (
    SUGGESTED_ACTION_MIN_POSTERIOR as _ACTION_MIN_POSTERIOR,
    SUGGESTED_ACTION_MIN_SAMPLES as _ACTION_MIN_SAMPLES,
    build_suggested_action,
    is_bearish_pattern,
    is_bullish_pattern,
    resolve_suggested_strike,
)
from backend.bot.data.bars import bars_to_dataframe, fetch_bars as _shared_fetch_bars
from backend.bot.detectors import (
    DETECTOR_REGISTRY, detect_all, disabled_patterns,
)
from backend.bot.ranker import _grade_for, build_grade_explainer_for_cohort
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.brain_prediction import BrainPrediction
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analysis", tags=["analysis"])


# Tunable thresholds for "suggested action" gating. Defined in
# ``backend.bot.analysis._actions`` so the fast + deep + EOD composers
# share a single source of truth.
SUGGESTED_ACTION_MIN_POSTERIOR = _ACTION_MIN_POSTERIOR
SUGGESTED_ACTION_MIN_SAMPLES = _ACTION_MIN_SAMPLES


# Thesis cache (process-local). Key: (ticker, window). Value:
# {"theses": {...}, "summary": "..."} + expires_at.
_THESIS_CACHE_TTL_SEC = 15 * 60  # 15 minutes
_thesis_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
_thesis_cache_lock = threading.Lock()


def _cache_get(key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _thesis_cache_lock:
        entry = _thesis_cache.get(key)
        if entry is None:
            return None
        if entry.get("expires_at", 0) < now:
            _thesis_cache.pop(key, None)
            return None
        return entry["value"]


def _cache_put(key: Tuple[str, str], value: Dict[str, Any]) -> None:
    with _thesis_cache_lock:
        _thesis_cache[key] = {
            "value": value,
            "expires_at": time.time() + _THESIS_CACHE_TTL_SEC,
        }


def clear_thesis_cache() -> None:
    """Test hook — drop all cached theses so a fresh test sees a fresh call."""
    with _thesis_cache_lock:
        _thesis_cache.clear()


# ── data fetch helpers ────────────────────────────────────────────────


def _resolve_window(window: str) -> Tuple[str, datetime]:
    """Map a window slug to (interval, since_dt). MITS Phase 4 (P4.3)
    delegates period/lookback to the shared bars helper, so we only
    need to return the interval + a since-cutoff for filtering
    observations.

    Short-range:
      ``today`` — 5m, since the start of today UTC.
      ``5d``   — 15m, since 5 days ago.
      ``all``  — 1h, since 30 days ago.

    Long-range (added 2026-06-14 to fix 3Y/5Y/MAX returning ~6 months):
      ``1m``   — 1d, since 31 days ago.
      ``3m``   — 1d, since 95 days ago.
      ``6m``   — 1d, since 185 days ago.
      ``1y``   — 1d, since 370 days ago.
      ``3y``   — 1d, since 3·366 days ago.
      ``5y``   — 1d, since 5·366 days ago.
      ``max``  — 1d, since 15·366 days ago.
    """
    w = (window or "today").lower()
    long_range = {
        "1m":  31,
        "3m":  95,
        "6m":  185,
        "1y":  370,
        "3y":  3 * 366,
        "5y":  5 * 366,
        "max": 15 * 366,
    }
    if w in long_range:
        return "1d", datetime.utcnow() - timedelta(days=long_range[w])
    if w == "5d":
        return "15m", datetime.utcnow() - timedelta(days=5)
    if w == "all":
        return "1h", datetime.utcnow() - timedelta(days=30)
    today_start = datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return "5m", today_start


def _fetch_bars_with_source(
    ticker: str, window: str, interval: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """Pull OHLCV bars via ThetaData→yfinance fallback. Returns
    ``(bars, source)`` with source in {'thetadata', 'yfinance', 'none'}.
    """
    payload = _shared_fetch_bars(
        ticker, window=window, interval=interval,
    )
    return payload.get("bars") or [], payload.get("source", "none")


_last_bar_source: Dict[Tuple[str, str], str] = {}


def _fetch_bars(ticker: str, period: str, interval: str) -> List[Dict[str, Any]]:
    """Compatibility shim — preserved for tests / external callers that
    still mock the original analysis bar fetchers. Delegates to the
    shared ThetaData→yfinance helper. ``period`` is interpreted as a
    window slug ("today"/"5d"/"all"); legacy yfinance period strings
    map sensibly via :func:`_shared_fetch_bars`. Records the resolved
    source in a module-level dict so the route can surface it without
    re-fetching."""
    window = period if period in {"today", "5d", "all"} else "today"
    bars, source = _fetch_bars_with_source(ticker, window, interval)
    _last_bar_source[(ticker.upper(), window)] = source
    return bars


def _fetch_bars_dataframe(ticker: str, period: str, interval: str):
    """Compatibility shim — returns the DataFrame parallel of
    :func:`_fetch_bars`. Detectors prefer the DataFrame shape."""
    bars = _fetch_bars(ticker, period, interval)
    return bars_to_dataframe(bars) if bars else None


def _detector_family(pattern: str) -> str:
    det = DETECTOR_REGISTRY.get(pattern)
    return getattr(det, "family", "uncategorized") if det else "uncategorized"


def _run_detectors_in_window(
    ticker: str, df, since_dt: Optional[datetime]
) -> List[Dict[str, Any]]:
    """Run every enabled detector on the bar window and return a list of
    annotation dicts. Empty list when the corpus is cold / no fires.
    """
    if df is None or len(df) < 5:
        return []
    try:
        obs = detect_all(ticker, df)
    except Exception:
        logger.debug("detect_all failed", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for o in obs:
        ts = o.timestamp
        if since_dt is not None and ts is not None:
            try:
                if ts < since_dt:
                    continue
            except Exception:
                pass
        out.append({
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "pattern": o.pattern,
            "family": _detector_family(o.pattern),
            "regime": o.regime,
            "vol_state": o.vol_state,
            "time_bucket": o.time_bucket,
            "type": "detector_hit",
        })
    return out


def _knowledge_for_patterns(
    ticker: str, patterns: List[str], *, horizon: str = "1d",
    similar_limit: int = 20,
) -> Dict[str, Dict[str, Any]]:
    """For each pattern, return the most-populated cohort cell + recent
    matching observations with outcomes (the "similar trades" panel
    backing data).
    """
    if not patterns:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    disabled = disabled_patterns()
    # Phase 12.2 — use the hierarchical fallback so the per-stock thesis
    # surfaces a pooled posterior when the local (ticker, pattern) cell
    # is thin from the direction-aware split.
    try:
        from backend.bot.corpus.knowledge_graph import (
            get_posterior_with_fallback,
        )
    except Exception:
        get_posterior_with_fallback = None  # type: ignore
    try:
        with session_scope() as s:
            for pat in patterns:
                if pat in disabled:
                    continue
                cell = s.execute(
                    select(KnowledgeGraphCell)
                    .where(KnowledgeGraphCell.ticker == ticker)
                    .where(KnowledgeGraphCell.pattern == pat)
                    .order_by(desc(KnowledgeGraphCell.sample_size))
                ).scalars().first()
                if cell is None:
                    # No local cell at all — try the global parent.
                    if get_posterior_with_fallback is None:
                        continue
                    entry = get_posterior_with_fallback(
                        ticker=ticker, pattern=pat,
                        regime="unknown", vol_state="normal",
                        horizon=horizon, sample_split="combined",
                    )
                    if entry is None:
                        continue
                    out[pat] = {
                        "sample_size": int(entry.get("n") or 0),
                        "posterior_win_rate": entry.get("posterior"),
                        "win_rate": entry.get("win_rate"),
                        "confidence_band": [None, None],
                        "avg_return_pct": entry.get("avg_return_pct"),
                        "avg_hold_minutes": None,
                        "regime": entry.get("regime"),
                        "vol_state": entry.get("vol_state"),
                        "horizon": horizon,
                        "cohort_source": entry.get("source"),
                        "similar_outcomes": [],
                    }
                    continue
                # Pull last 20 outcomes for the pattern (any regime).
                obs_rows = s.execute(
                    select(MarketObservation)
                    .where(MarketObservation.ticker == ticker)
                    .where(MarketObservation.pattern == pat)
                    .order_by(desc(MarketObservation.timestamp))
                    .limit(int(similar_limit) * 2)
                ).scalars().all()
                obs_ids = [r.id for r in obs_rows]
                outcomes_by_id: Dict[int, MarketOutcome] = {}
                if obs_ids:
                    outcomes = s.execute(
                        select(MarketOutcome)
                        .where(MarketOutcome.observation_id.in_(obs_ids))
                        .where(MarketOutcome.horizon == horizon)
                    ).scalars().all()
                    for oc in outcomes:
                        outcomes_by_id.setdefault(oc.observation_id, oc)
                similar: List[Dict[str, Any]] = []
                for r in obs_rows:
                    oc = outcomes_by_id.get(r.id)
                    if oc is None:
                        continue
                    similar.append({
                        "observation_id": r.id,
                        "timestamp": (
                            r.timestamp.isoformat()
                            if r.timestamp else None
                        ),
                        "regime": r.regime,
                        "vol_state": r.vol_state,
                        "horizon": oc.horizon,
                        "return_pct": oc.return_pct,
                        "was_winner": bool(oc.was_winner)
                            if oc.was_winner is not None else None,
                    })
                    if len(similar) >= int(similar_limit):
                        break
                # Phase 12.2 — promote thin local cells using the
                # hierarchical fallback so the per-stock thesis isn't
                # rendered with N=5 posteriors.
                entry_n = int(cell.sample_size or 0)
                cohort_source = "cell"
                post_eff = cell.posterior_win_rate
                wr_eff = cell.win_rate
                ret_eff = cell.avg_return_pct
                if (entry_n < 30 and get_posterior_with_fallback is not None):
                    try:
                        promo = get_posterior_with_fallback(
                            ticker=ticker, pattern=pat,
                            regime=(cell.regime or "unknown"),
                            vol_state=(cell.vol_state or "normal"),
                            horizon=horizon,
                            sample_split="combined",
                        )
                    except Exception:
                        promo = None
                    if promo is not None and int(promo.get("n") or 0) > entry_n:
                        entry_n = int(promo.get("n") or 0)
                        post_eff = promo.get("posterior")
                        wr_eff = promo.get("win_rate")
                        ret_eff = promo.get("avg_return_pct")
                        cohort_source = promo.get("source") or "pooled"
                out[pat] = {
                    "sample_size": entry_n,
                    "posterior_win_rate": post_eff,
                    "win_rate": wr_eff,
                    "confidence_band": [
                        cell.confidence_lower, cell.confidence_upper,
                    ],
                    "avg_return_pct": ret_eff,
                    "avg_hold_minutes": cell.avg_hold_minutes,
                    "regime": cell.regime,
                    "vol_state": cell.vol_state,
                    "horizon": cell.horizon,
                    "cohort_source": cohort_source,
                    "similar_outcomes": similar,
                }
    except Exception:
        logger.debug("knowledge_for_patterns failed", exc_info=True)
    return out


# ── thesis composer (single Claude call per (ticker, window)) ─────────


def _format_pattern_block(pat: str, k: Dict[str, Any]) -> str:
    n = int(k.get("sample_size") or 0)
    post = k.get("posterior_win_rate")
    wr = k.get("win_rate")
    avg_ret = k.get("avg_return_pct")
    avg_hold = k.get("avg_hold_minutes")
    lo, hi = (k.get("confidence_band") or [None, None])
    parts = [f"pattern={pat}", f"N={n}"]
    if post is not None:
        parts.append(f"posterior={post*100:.0f}%")
    if wr is not None:
        parts.append(f"frequentist_wr={wr*100:.0f}%")
    if avg_ret is not None:
        parts.append(f"avg_move={avg_ret*100:+.1f}%")
    if avg_hold is not None:
        parts.append(f"avg_hold_min={avg_hold:.0f}")
    if lo is not None and hi is not None:
        parts.append(f"CI=[{lo*100:.0f}%, {hi*100:.0f}%]")
    return ", ".join(parts)


_FALLBACK_INVALIDATION = [
    "Position closes the day below the breakdown level",
    "Volume dries up below the 20-bar median",
    "Regime flips to choppy / counter-trend",
]


def _resolve_suggested_strike(
    ticker: str, spot: float, direction: str, dte_target: int,
) -> Tuple[Optional[float], str]:
    """Thin shim over ``backend.bot.analysis._actions.resolve_suggested_strike``
    (Phase 14.A consolidation). Preserved so existing test mocks keep working.
    """
    return resolve_suggested_strike(ticker, spot, direction, dte_target)


def _suggested_action_for(
    pattern: str, k: Dict[str, Any], ticker: str, spot: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Thin shim over the shared ``build_suggested_action`` helper. Phase
    14.A moved the bullish/bearish map to ``backend.bot.detectors.direction``
    so fast + deep + EOD composers consult one source of truth."""
    return build_suggested_action(
        pattern=pattern, knowledge=k, ticker=ticker, spot=spot,
    )


def _compose_via_claude(
    ticker: str, window: str, knowledge: Dict[str, Dict[str, Any]],
    observations: List[Dict[str, Any]], bars: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Phase 14.A — thin shim over ``deep_compose`` that returns the
    legacy ``{"theses": {...}, "summary": "..."}`` shape so existing
    callers + tests keep working.
    """
    from backend.bot.analysis.deep_composer import (
        deep_compose, deep_compose_to_legacy_dict,
    )
    output = deep_compose(
        ticker=ticker, window=window, knowledge=knowledge,
        observations=observations, bars=bars,
    )
    if output is None:
        return None
    spot: Optional[float] = None
    if bars:
        try:
            spot = float(bars[-1].get("close"))
        except Exception:
            spot = None
    return deep_compose_to_legacy_dict(
        output=output, knowledge=knowledge, ticker=ticker, spot=spot,
    )


# ── MITS Phase 11.I — insider + 13F per-ticker enrichment ────────────


def _insider_classify_code(code: str) -> str:
    """Form 4 transaction code classification.

    Codes:
      P = open-market or private purchase  -> 'buy'
      S = open-market or private sale       -> 'sell'
      A = grant/award                       -> 'grant' (not a buy signal)
      M = exercise of derivative            -> 'exercise'
      F = tax-withhold                      -> 'tax'
      G = gift                              -> 'gift'
    Anything else falls into 'other'.
    """
    c = (code or "").strip().upper()
    if c == "P":
        return "buy"
    if c == "S":
        return "sell"
    if c == "A":
        return "grant"
    if c == "M":
        return "exercise"
    if c == "F":
        return "tax"
    if c == "G":
        return "gift"
    return "other"


@router.get("/{ticker}/insider")
async def insider_activity(
    ticker: str,
    days: int = Query(90, ge=1, le=730),
    top_n: int = Query(5, ge=1, le=50),
) -> Dict[str, Any]:
    """MITS Phase 11.I — Form 4 insider activity for ``ticker``.

    Returns net buy/sell counts in the last ``days`` calendar days, the
    top-N transactions by dollar value, and a cluster signal flag (3+
    distinct insiders buying in the last 30 days).
    """
    from datetime import date as _date, timedelta as _td
    from backend.models.insider_trade import InsiderTrade

    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    days = int(days)
    cutoff = _date.today() - timedelta(days=days)
    cluster_cutoff = _date.today() - timedelta(days=30)

    try:
        with session_scope() as s:
            rows = s.execute(
                select(InsiderTrade)
                .where(InsiderTrade.ticker == ticker)
                .where(InsiderTrade.transaction_date >= cutoff)
                .order_by(desc(InsiderTrade.transaction_date))
            ).scalars().all()
            payload = [r.to_dict() for r in rows]
    except Exception as exc:
        logger.exception("insider activity query failed for %s", ticker)
        raise HTTPException(status_code=500, detail=str(exc))

    buys, sells = 0, 0
    buy_value, sell_value = 0.0, 0.0
    cluster_buyers = set()
    for r in payload:
        kind = _insider_classify_code(r.get("transaction_code") or "")
        val = float(r.get("total_value") or 0.0)
        if kind == "buy":
            buys += 1
            buy_value += val
            try:
                txn_dt = (datetime.fromisoformat(r["transaction_date"]).date()
                            if r.get("transaction_date") else None)
                if txn_dt and txn_dt >= cluster_cutoff:
                    cluster_buyers.add(r.get("insider_name") or "")
            except Exception:
                pass
        elif kind == "sell":
            sells += 1
            sell_value += val

    # Top transactions by absolute value.
    def _val(row: Dict[str, Any]) -> float:
        try:
            return abs(float(row.get("total_value") or 0.0))
        except Exception:
            return 0.0
    top_transactions = sorted(payload, key=_val, reverse=True)[:int(top_n)]

    cluster_flag = len(cluster_buyers) >= 3
    return {
        "ticker": ticker,
        "lookback_days": days,
        "row_count": len(payload),
        "buys_count": buys,
        "sells_count": sells,
        "net_count": buys - sells,
        "buy_value_usd": round(buy_value, 2),
        "sell_value_usd": round(sell_value, 2),
        "net_value_usd": round(buy_value - sell_value, 2),
        "cluster_buy_30d": cluster_flag,
        "cluster_distinct_buyers_30d": len(cluster_buyers),
        "top_transactions": top_transactions,
    }


@router.get("/{ticker}/13f")
async def fund_holdings_for_ticker(
    ticker: str,
    top_n: int = Query(5, ge=1, le=50),
) -> Dict[str, Any]:
    """MITS Phase 11.I — 13F fund-holdings snapshot for ``ticker``.

    Returns the top-N funds holding ``ticker`` at the latest available
    13F reporting quarter, with QoQ change_from_prior_qtr. Also returns
    an aggregate "smart-money flow" momentum number (sum of
    share-count change across the top-25 funds) so the UI can render a
    + / − chip without a second roundtrip.
    """
    from backend.models.fund_holding import FundHolding

    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    try:
        with session_scope() as s:
            latest_quarter = s.execute(
                select(FundHolding.quarter_end_date)
                .where(FundHolding.ticker == ticker)
                .order_by(desc(FundHolding.quarter_end_date))
                .limit(1)
            ).scalar()
            if latest_quarter is None:
                return {
                    "ticker": ticker,
                    "latest_quarter": None,
                    "fund_count": 0,
                    "top_funds": [],
                    "smart_money_flow_shares": 0.0,
                    "smart_money_flow_pct": 0.0,
                    "smart_money_direction": "flat",
                }
            top_rows = s.execute(
                select(FundHolding)
                .where(FundHolding.ticker == ticker)
                .where(FundHolding.quarter_end_date == latest_quarter)
                .order_by(desc(FundHolding.value_usd))
                .limit(int(top_n))
            ).scalars().all()
            top_funds = [r.to_dict() for r in top_rows]

            # Smart-money flow: sum of change_from_prior_qtr (positive = adds)
            # across the top-25 funds at the latest quarter. NULL = first
            # quarter on file, treated as 0.
            flow_rows = s.execute(
                select(FundHolding.shares,
                       FundHolding.change_from_prior_qtr)
                .where(FundHolding.ticker == ticker)
                .where(FundHolding.quarter_end_date == latest_quarter)
                .order_by(desc(FundHolding.value_usd))
                .limit(25)
            ).all()
            total_shares = sum(float(r[0] or 0.0) for r in flow_rows)
            total_change = sum(float(r[1] or 0.0) for r in flow_rows)
            flow_pct = (total_change / total_shares) if total_shares else 0.0
            direction = ("added" if total_change > 0
                          else ("trimmed" if total_change < 0 else "flat"))
            fund_count = s.execute(
                select(func.count(FundHolding.id))
                .where(FundHolding.ticker == ticker)
                .where(FundHolding.quarter_end_date == latest_quarter)
            ).scalar() or 0
    except Exception as exc:
        logger.exception("13F query failed for %s", ticker)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ticker": ticker,
        "latest_quarter": (latest_quarter.isoformat()
                            if latest_quarter else None),
        "fund_count": int(fund_count),
        "top_funds": top_funds,
        "smart_money_flow_shares": round(total_change, 0),
        "smart_money_flow_pct": round(flow_pct, 4),
        "smart_money_direction": direction,
    }


# ── Phase 14.D — cross-window disagreement + BrainPrediction persist ──


_PARALLEL_WINDOW: Dict[str, str] = {"today": "5d", "5d": "today"}


def _cross_window_check(
    ticker: str, window: str, theses: Dict[str, Dict[str, Any]],
) -> Tuple[bool, str]:
    """Compare today/5d chosen theses against the parallel window's
    cached payload. Returns ``(window_disagreement, reconciler_note)``.

    No-op (False, "") when window=='all', no parallel cache exists, or
    no pattern fires in both windows with opposing actions.
    """
    parallel = _PARALLEL_WINDOW.get(window)
    if not parallel:
        return False, ""
    other = _cache_get((ticker, parallel))
    if not other:
        return False, ""
    other_chosen = other.get("chosen") or {}
    for pat, payload in theses.items():
        my_action = (payload.get("suggested_action") or {})
        if not isinstance(my_action, dict):
            continue
        my_dir = my_action.get("action")
        if not my_dir:
            continue
        other_payload = other_chosen.get(pat) or {}
        other_action = other_payload.get("suggested_action") or {}
        if not isinstance(other_action, dict):
            continue
        other_dir = other_action.get("action")
        if not other_dir or other_dir == my_dir:
            continue
        note = (
            f"{window}-window says {my_dir} on {pat} while "
            f"{parallel}-window says {other_dir} — short-term momentum "
            f"likely diverging from swing trend; size down or wait "
            f"for confirmation."
        )
        return True, note
    return False, ""


def _persist_brain_predictions(
    *, surface: str, ticker: str, window: Optional[str],
    theses: Dict[str, Dict[str, Any]],
    knowledge: Dict[str, Dict[str, Any]],
    regime_vector: Optional[Dict[str, Any]] = None,
    confidence_breakdown: Optional[Dict[str, Any]] = None,
    top_strategy: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort write of one BrainPrediction row per pattern with a
    non-null suggested_action. Wrapped in try/except so a write failure
    never blocks the response.

    MITS Phase 15.E — also stamps decision-time JSON snapshots of the
    regime vector, council confidence breakdown, and top strategy so the
    nightly linker can attribute outcomes to each component.
    """
    regime_blob = json.dumps(regime_vector) if regime_vector else None
    breakdown_blob = (
        json.dumps(confidence_breakdown) if confidence_breakdown else None
    )
    strategy_blob = json.dumps(top_strategy) if top_strategy else None
    try:
        with session_scope() as s:
            for pat, payload in theses.items():
                if not isinstance(payload, dict):
                    continue
                action = payload.get("suggested_action")
                if not isinstance(action, dict) or not action.get("action"):
                    continue
                k = knowledge.get(pat) or {}
                inv = payload.get("invalidation") or []
                s.add(BrainPrediction(
                    surface=surface,
                    ticker=ticker,
                    window=window,
                    pattern=pat,
                    suggested_action=action.get("action"),
                    suggested_direction=action.get("direction"),
                    suggested_strike=action.get("strike"),
                    suggested_dte=action.get("dte"),
                    posterior_at_decision=k.get("posterior_win_rate"),
                    sample_size_at_decision=k.get("sample_size"),
                    confidence_self_assessment=payload.get(
                        "confidence_self_assessment"),
                    invalidation_json=json.dumps(list(inv)),
                    thesis_paragraph=payload.get("thesis_paragraph"),
                    regime_at_decision=regime_blob,
                    confidence_breakdown_at_decision=breakdown_blob,
                    top_strategy_at_decision=strategy_blob,
                ))
    except Exception:
        logger.debug("brain prediction persist failed for %s/%s",
                       ticker, window, exc_info=True)


# ── route ─────────────────────────────────────────────────────────────


def _grade_explainer_for_thesis(
    pattern: str, knowledge: Optional[Dict[str, Any]],
) -> str:
    """MITS Phase 14.E — compose the per-pattern grade explainer for the
    /analysis response.

    The /analysis route doesn't run the full features → probability →
    ranker pipeline (that's the engine path); it scores patterns directly
    off cohort cells. So we reconstruct the grade by treating the cohort
    posterior as the composite score — that's also what the EOD bias
    ranker uses for cohort-driven grading. CI bounds and regime come
    straight from the surfaced knowledge dict; pin probability is not
    available without an options-chain pull and is reported as 0.
    """
    if not knowledge:
        return ""
    post = knowledge.get("posterior_win_rate")
    n = knowledge.get("sample_size") or 0
    if post is None or not n:
        return ""
    try:
        post_f = float(post)
        n_int = int(n)
    except (TypeError, ValueError):
        return ""
    band = knowledge.get("confidence_band") or [None, None]
    ci_lo, ci_hi = band[0], band[1]
    regime_label = (knowledge.get("regime") or "unknown")
    if is_bullish_pattern(pattern):
        direction = "LONG"
    elif is_bearish_pattern(pattern):
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    # Cohort-driven grade: use the posterior as the composite proxy.
    grade = _grade_for(post_f)
    return build_grade_explainer_for_cohort(
        posterior=post_f, sample_size=n_int,
        ci_lower=ci_lo, ci_upper=ci_hi,
        regime_label=str(regime_label),
        pinning_probability=0.0,
        grade=grade, score=post_f,
        direction=direction,
    )


def _features_for_snapshot(ticker: str, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Best-effort snapshot → features dict for the hybrid composer.

    The hybrid composer doesn't need this on the critical path (the
    deep path lives off the cohort knowledge dict), but passing
    features lets fast_compose_one surface vol/regime tags when the
    cohort lookup didn't carry them. Pure best-effort; failure
    degrades gracefully to None features.
    """
    if not bars:
        return {}
    try:
        from backend.bot.features import build_features
        spot = float(bars[-1].get("close") or 0.0)
        snapshot = {"price": spot}
        return build_features(snapshot) or {}
    except Exception:
        return {}


def _build_strategy_matrix_for_analysis(
    *, ticker: str, bars: List[Dict[str, Any]],
    observations: List[Dict[str, Any]],
):
    """MITS Phase 15.C — compose the StrategyMatrix for the analysis
    route. Gated by ``TUNABLES.strategy_matrix_enabled`` upstream; this
    helper is only called when the flag is on."""
    from backend.bot.analysis.strategy_matrix import build_strategy_matrix
    from backend.bot.corpus.analog_retrieval import retrieve_analogs
    from backend.bot.features import build_features
    from backend.bot.regime.vector import build_regime_vector
    spot = float(bars[-1].get("close")) if bars else 0.0
    snapshot: Dict[str, Any] = {"price": spot}
    snapshot["features"] = build_features(snapshot) or {}
    rv = build_regime_vector(ticker=ticker, snapshot=snapshot)
    top_pat = observations[-1]["pattern"] if observations else "na"
    analogs = retrieve_analogs(
        ticker=ticker, regime_vector=rv, pattern=top_pat,
        horizon="5d", k=50, sector_fallback=True,
    )
    iv_rank = snapshot.get("features", {}).get("iv_rank")
    iv_regime_label: Optional[str] = None
    current_iv: Optional[float] = None
    try:
        from backend.bot.iv_regime import classify_ticker
        report = classify_ticker(ticker)
        iv_regime_label = report.regime
        current_iv = report.current_iv
    except Exception:
        pass
    iv_state = {
        "iv_rank": iv_rank, "iv_regime": iv_regime_label,
        "current_iv": current_iv,
    }
    return build_strategy_matrix(
        ticker=ticker, regime_vector=rv, pattern_hits=observations,
        analogs=analogs, iv_state=iv_state,
    )


@router.get("/{ticker}")
async def analyze_ticker(
    ticker: str,
    background_tasks: BackgroundTasks,
    window: str = Query(
        "today",
        pattern="^(today|5d|1m|3m|6m|1y|3y|5y|max|all)$",
        description=(
            "Time range slug. Short: today/5d/all (legacy intraday). "
            "Long-range (daily bars): 1m/3m/6m/1y/3y/5y/max."
        ),
    ),
    interval: Optional[str] = Query(
        None,
        pattern="^(1m|5m|15m|30m|1h|1d|1w)?$",
        description=(
            "Optional candle granularity override. When unset, the "
            "window's default interval is used (today=5m, 5d=15m, "
            "long-range=1d, all=1h)."
        ),
    ),
) -> Dict[str, Any]:
    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")

    resolved_interval, since_dt = _resolve_window(window)
    # Honour an explicit interval override so a long-range window can
    # be requested at a finer granularity (e.g. window=1y interval=1h
    # for a year-long hourly chart).
    if interval:
        resolved_interval = interval
    bars = _fetch_bars(ticker, window, resolved_interval)
    df = _fetch_bars_dataframe(ticker, window, resolved_interval)
    bar_source = _last_bar_source.get(
        (ticker.upper(), window), "none" if not bars else "unknown",
    )

    observations = _run_detectors_in_window(ticker, df, since_dt)
    patterns_today = sorted({o["pattern"] for o in observations})
    knowledge = _knowledge_for_patterns(ticker, patterns_today)

    cache_key = (ticker, window)
    cached = _cache_get(cache_key)
    sm = None
    if cached is None:
        features = _features_for_snapshot(ticker, bars)
        if getattr(TUNABLES, "strategy_matrix_enabled", False):
            sm = _build_strategy_matrix_for_analysis(
                ticker=ticker, bars=bars, observations=observations,
            )
        ensemble = compose_hybrid(
            ticker=ticker, window=window, knowledge=knowledge,
            observations=observations, bars=bars,
            features=features,
            deep_top_n=int(TUNABLES.deep_composer_top_n),
            strategy_matrix=sm,
        )
        cached = ensemble.to_dict()
        _cache_put(cache_key, cached)

    chosen = cached.get("chosen") or {}
    theses = {pat: {
        "headline": payload.get("headline") or "",
        "thesis_paragraph": payload.get("thesis_paragraph") or "",
        "suggested_action": payload.get("suggested_action"),
        "invalidation": payload.get("invalidation")
            or list(_FALLBACK_INVALIDATION),
        "confidence_self_assessment": payload.get(
            "confidence_self_assessment"),
        # MITS Phase 14.E — operator-readable grade explainer.
        "grade_explainer": _grade_explainer_for_thesis(pat, knowledge.get(pat)),
        # MITS Phase 15.C — top StrategyMatrix candidate when the flag is on.
        "top_strategy": payload.get("top_strategy"),
    } for pat, payload in chosen.items()}

    # MITS Phase 15.A — consolidated regime view. Computed up here so
    # 15.E can stamp it on the BrainPrediction rows below.
    regime_vector_dict: Optional[Dict[str, Any]] = None
    try:
        from backend.bot.features import build_features
        from backend.bot.regime.vector import build_regime_vector
        spot = float(bars[-1].get("close")) if bars else 0.0
        snapshot: Dict[str, Any] = {"price": spot}
        snapshot["features"] = build_features(snapshot)
        rv = build_regime_vector(ticker=ticker, snapshot=snapshot)
        regime_vector_dict = rv.to_dict()
    except Exception:
        logger.warning("regime_vector build failed for %s", ticker, exc_info=True)

    # Prefer the freshly built StrategyMatrix when we just composed the
    # ensemble; on a cache hit, fall back to the top_strategy embedded
    # in any chosen thesis payload (compose_hybrid stamps it there).
    top_strategy_dict: Optional[Dict[str, Any]] = None
    if sm is not None and sm.top_strategy is not None:
        top_strategy_dict = sm.top_strategy.to_dict()
    else:
        for payload in chosen.values():
            ts = payload.get("top_strategy")
            if isinstance(ts, dict) and ts:
                top_strategy_dict = ts
                break

    background_tasks.add_task(
        _persist_brain_predictions,
        surface="analysis", ticker=ticker, window=window,
        theses=theses, knowledge=knowledge,
        regime_vector=regime_vector_dict,
        top_strategy=top_strategy_dict,
    )
    window_disagreement, reconciler_note = _cross_window_check(
        ticker, window, theses,
    )

    return {
        "ticker": ticker,
        "window": window,
        "bars": bars,
        "bar_source": bar_source,
        "observations": observations,
        "knowledge": knowledge,
        "theses": theses,
        "summary": cached.get("summary") or "",
        "fast_thesis": cached.get("fast") or {},
        "uncertainty_signal": cached.get("uncertainty_signal") or {},
        "window_disagreement": window_disagreement,
        "reconciler_note": reconciler_note,
        "regime_vector": regime_vector_dict,
    }
