"""MITS Phase 9.1 + Phase 10 + Phase 10.1 — Theory Studio API.

Endpoints:

  * ``GET  /theories``                          — registry list.
  * ``GET  /theories/{theory}/{ticker}``        — bars + auto-annotation.
  * ``GET  /theories/multi/{ticker}``           — bars + N theory annotations.
  * ``GET  /theories/quote/{ticker}``           — MITS-P10.1 live tick
                                                  (price + ts + source,
                                                  500ms cache, 1s-poll
                                                  friendly).
  * ``POST /theories/{theory}/{ticker}/save``   — persist an operator edit.
  * ``DELETE /theories/{theory}/{ticker}/saved`` — revert to auto.

23 theory modules ship under ``backend/bot/theories`` (Phase-9 baseline
of 5 + Phase-10 extension of 18). Bars come from the unified
``fetch_bars`` helper in ``backend/bot/data/bars`` (ThetaData →
yfinance fallback).

MITS-P10 fixes vs Phase 9.6:

  * ``window=max`` now returns ≥1000 daily bars (was capped at ~126).
  * Bar-count contract per window is documented in ``WINDOW_MAP``.
  * New ``/theories/multi/{ticker}`` endpoint accepts a comma-separated
    ``theories=`` list and returns a single payload with ``bars`` once
    plus an ``annotations`` dict keyed by theory name. Frontend
    multi-select picker calls this.
  * ``live=true`` query param surfaces a ``live`` flag in the payload —
    the frontend uses this to schedule a 30s / 5min refresh ticker.
    The backend itself does not change cache semantics for the live
    case; the polling loop just re-issues the request.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from backend.bot.data.bars import fetch_bars
from backend.bot.theories import THEORIES
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.saved_theory_annotation import SavedTheoryAnnotation


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/theories", tags=["theories"])


# ── Bars helper: map UI window → fetch_bars(window, interval) ─────────

#
# MITS-P10 contract (bars per window, daily interval):
#
#     1m  =   21 bars  (30 calendar days; ~21 trading days)
#     3m  =   63 bars  (90 calendar days; ~63 trading days)
#     6m  =  126 bars  (180 calendar days)
#     1y  =  252 bars  (365 calendar days)
#     2y  =  504 bars  (730 calendar days)
#     5y  = 1260 bars  (1825 calendar days)
#     max = 1260+ bars (3650 calendar days; up to 2520 trading days)
#
# The previous Phase-9 mapping used lookback_days but capped 5y at
# 1825 and "max" at 3650 — `fetch_bars` honoured the lookback range
# correctly. The bug operator was hitting (window=max returning ~6 mo
# of bars) was actually the *interval* default: when the route passed
# ``window="all"`` to fetch_bars without an explicit interval, the
# preset table mapped "all" to "1h" with a default 30-day lookback —
# the explicit ``lookback_days=cfg["lookback_days"]`` ARGUMENT was
# being honoured, but only after the preset's interval resolution
# decided we wanted 1h bars. That meant a 1825-day lookback at 1h ≈
# 11 000+ bars on paper but the ThetaData v3 endpoint only ships EOD
# bars when ``interval=1d`` — we end up with whatever 1h ThetaData
# returns (typically a 30-day truncation). The fix below explicitly
# passes the resolved daily interval.
#

WINDOW_MAP: Dict[str, Dict[str, Any]] = {
    # UI label  → (lookback_days, interval, expected_min_bars,
    #               aggregate_to). ``aggregate_to`` is the resample bucket
    #               MITS-P10.1 collapses daily bars into so the front-end
    #               doesn't have to render 2 500 candles on the ``max``
    #               window. Buckets:
    #                 ``"D"``  — daily (no resample).
    #                 ``"W"``  — weekly (last close per ISO week).
    #                 ``"M"``  — monthly (last close per calendar month).
    "1m":  {"lookback_days":   30, "interval": "1d", "min_bars":   15, "aggregate_to": "D"},
    "3m":  {"lookback_days":   90, "interval": "1d", "min_bars":   55, "aggregate_to": "D"},
    "6m":  {"lookback_days":  180, "interval": "1d", "min_bars":  110, "aggregate_to": "D"},
    "1y":  {"lookback_days":  365, "interval": "1d", "min_bars":  220, "aggregate_to": "D"},
    "2y":  {"lookback_days":  730, "interval": "1d", "min_bars":  100, "aggregate_to": "W"},
    "5y":  {"lookback_days": 1825, "interval": "1d", "min_bars":  240, "aggregate_to": "W"},
    "max": {"lookback_days": 3650, "interval": "1d", "min_bars":  100, "aggregate_to": "M"},
}


def _bucket_key(ts_iso: str, mode: str) -> str:
    """Resample bucket key for a bar timestamp. ``mode`` ∈ {D, W, M}."""
    if mode == "D":
        return ts_iso[:10]
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except Exception:
        return ts_iso[:10]
    if mode == "W":
        iso = dt.isocalendar()
        # iso = (year, week, weekday) — bucket = "YYYY-Www".
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    if mode == "M":
        return f"{dt.year:04d}-{dt.month:02d}"
    return ts_iso[:10]


def _aggregate_bars(bars: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """Resample a list of daily bars into weekly / monthly buckets.

    The bucket's bar uses: ``open`` = first bar's open in the bucket,
    ``high`` = max of highs, ``low`` = min of lows, ``close`` = last
    bar's close, ``volume`` = sum, ``t`` = first bar's ISO timestamp.

    Returns ``bars`` unchanged when ``mode == "D"``.
    """
    if mode == "D" or not bars:
        return bars
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for b in bars:
        ts = str(b.get("t") or b.get("timestamp") or "")
        if not ts:
            continue
        key = _bucket_key(ts, mode)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(b)
    out: List[Dict[str, Any]] = []
    for key in order:
        rows = buckets[key]
        if not rows:
            continue
        first = rows[0]; last = rows[-1]
        try:
            high = max(float(r.get("high") or 0) for r in rows)
            low_candidates = [float(r.get("low") or 0) for r in rows
                              if (r.get("low") or 0) > 0]
            low = min(low_candidates) if low_candidates else 0.0
            vol = sum(float(r.get("volume") or 0) for r in rows)
            out.append({
                "t": first.get("t") or first.get("timestamp"),
                "open": float(first.get("open") or 0),
                "high": high,
                "low": low,
                "close": float(last.get("close") or 0),
                "volume": vol,
            })
        except Exception:
            continue
    return out


def _fetch_window(ticker: str, window: str):
    cfg = WINDOW_MAP.get(window) or WINDOW_MAP["1y"]
    payload = fetch_bars(
        ticker,
        window="all",
        interval=cfg["interval"],            # FIXED — was relying on "all" default = 1h.
        lookback_days=cfg["lookback_days"],
    )
    # MITS-P10.1 — aggregate daily bars into weekly/monthly buckets for
    # multi-year windows so the front-end doesn't get 2 500 candles +
    # the theories re-run on the visible-resolution series, keeping math
    # consistent with what the operator sees.
    raw_bars = payload.get("bars") or []
    raw_count = len(raw_bars)
    agg_mode = cfg.get("aggregate_to") or "D"
    if agg_mode != "D" and raw_bars:
        try:
            payload["bars"] = _aggregate_bars(raw_bars, agg_mode)
            payload["interval"] = ("1wk" if agg_mode == "W" else "1mo")
        except Exception:
            logger.exception("bar-aggregation failed; falling back to raw daily")
    payload["requested_window"] = window
    payload["min_bars_expected"] = cfg["min_bars"]
    payload["raw_bar_count"] = raw_count
    payload["aggregated_to"] = agg_mode
    return payload


# ── Annotation cache ─────────────────────────────────────────────────


_CACHE: Dict[str, Any] = {}


def _cache_key(theory: str, ticker: str, window: str,
                  params: Dict[str, Any]) -> str:
    # Stable serialisation: sorted keys.
    return f"{theory}|{ticker.upper()}|{window}|{json.dumps(params, sort_keys=True, default=str)}"


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.monotonic() - ts) > TUNABLES.theory_cache_ttl:
        return None
    return payload


def _cache_put(key: str, payload: Dict[str, Any]) -> None:
    _CACHE[key] = (time.monotonic(), payload)
    if len(_CACHE) > 256:
        # Drop oldest 25%.
        items = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
        for k, _ in items[:64]:
            _CACHE.pop(k, None)


# ── routes ────────────────────────────────────────────────────────────


@router.get("")
def list_theories() -> Dict[str, Any]:
    return {
        "theories": [
            {"name": name, "label": label}
            for name, (_fn, label) in THEORIES.items()
        ],
        "windows": list(WINDOW_MAP.keys()),
        "zigzag_pct_default": TUNABLES.theory_zigzag_pct,
        "count": len(THEORIES),
    }


def _params_from_query(request: Request) -> Dict[str, Any]:
    """Pull ``params`` from the query string. Supports either a single
    URL-encoded JSON blob (``?params=%7B...%7D``) or individual scalar
    keys for the common knobs.
    """
    qp = dict(request.query_params)
    qp.pop("window", None)
    qp.pop("live", None)
    qp.pop("theories", None)
    out: Dict[str, Any] = {}
    raw = qp.pop("params", None)
    if raw:
        try:
            out.update(json.loads(raw))
        except Exception:
            pass
    # MITS-P10.3.4 — density param: simple | normal | detailed. Forwarded
    # into theory params (so theories can self-filter) AND used by the
    # route as a fallback post-filter on lines that carry meta.priority.
    if "density" in qp:
        dval = str(qp["density"]).lower()
        if dval in ("simple", "normal", "detailed"):
            out["density"] = dval
    # Coerce well-known scalars.
    for key in ("zigzag_pct", "unit_lookback", "lookback",
                  "pivot_index", "anchor_a_index", "anchor_b_index",
                  "tenkan", "kijun", "senkou_b", "displacement",
                  "min_confidence", "period", "mult",
                  "ema_period", "atr_period", "fast", "slow", "signal",
                  "k_period", "d_period", "rsi_period", "exit_period",
                  "bins", "value_area_pct", "range_lookback",
                  "atr_mult", "bos_lookback", "max_obs", "max_fvgs",
                  "min_score", "squeeze_pct", "min_gap_pct"):
        if key in qp:
            try:
                if key in {"zigzag_pct", "min_confidence", "mult",
                            "value_area_pct", "atr_mult",
                            "min_score", "squeeze_pct", "min_gap_pct"}:
                    out[key] = float(qp[key])
                else:
                    out[key] = int(qp[key])
            except Exception:
                continue
    for key in ("show_retracements", "show_time_cycles", "show_fan",
                  "show_extensions"):
        if key in qp:
            out[key] = qp[key].lower() in {"1", "true", "yes", "on"}
    for key in ("pivot_ts", "pivot_type", "anchor_a_ts", "anchor_b_ts"):
        if key in qp:
            out[key] = qp[key]
    if "periods" in qp:
        out["periods"] = [p.strip() for p in qp["periods"].split(",") if p.strip()]
    return out


# MITS-P10.3.4 — universal density post-filter.
#
# Theories that have not been refactored to consume ``density`` natively
# (everything except pivots + murrey) still benefit from the operator's
# density choice via priority metadata attached to each Line.
#
#   simple   →  drop lines with meta.priority > 1
#   normal   →  drop lines with meta.priority > 2
#   detailed →  no filter
#
# Theories without priority metadata default to priority=2 (i.e. they
# survive normal+detailed but get knocked out by simple). The pivots
# and murrey_math modules already self-filter on density, so the post-
# filter is a NO-OP on lines they emit (they emit only "kept" ones).
DEFAULT_LINE_PRIORITY = 2


def _line_priority(ln: Dict[str, Any]) -> int:
    meta = ln.get("meta") or {}
    pri = meta.get("priority")
    try:
        return int(pri) if pri is not None else DEFAULT_LINE_PRIORITY
    except Exception:
        return DEFAULT_LINE_PRIORITY


def _apply_density_filter(annotation: Dict[str, Any], density: str) -> Dict[str, Any]:
    """Strip lines whose priority exceeds the density level.

    ``density`` ∈ {simple, normal, detailed}. Returns the same dict
    (mutated) so callers can chain.
    """
    if not annotation:
        return annotation
    if density not in ("simple", "normal", "detailed"):
        return annotation
    if density == "detailed":
        return annotation
    threshold = 1 if density == "simple" else 2
    lines = annotation.get("lines") or []
    kept = [ln for ln in lines if _line_priority(ln) <= threshold]
    annotation["lines"] = kept
    return annotation


def _live_payload_tag(live: bool) -> Dict[str, Any]:
    """The frontend uses these fields to decide its poll interval."""
    return {
        "live": bool(live),
        "server_ts": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/multi/{ticker}")
def analyse_multi(
    ticker: str,
    request: Request,
    theories: str = Query(..., description="Comma-separated theory names"),
    window: str = Query("1y"),
    live: bool = Query(False),
) -> Dict[str, Any]:
    """Multi-theory analysis. Returns ``{bars, annotations: {theory:
    annotation_dict, …}}`` so the frontend can render multiple theory
    overlays from a single bars payload.

    MITS-P10 — used by the Theory Studio multi-select chip UI.
    """
    if window not in WINDOW_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown window: {window}")
    names = [n.strip() for n in (theories or "").split(",") if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="theories= must be a "
                                                    "comma-separated list.")
    unknown = [n for n in names if n not in THEORIES]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown theories: {', '.join(unknown)}",
        )

    bar_payload = _fetch_window(ticker, window)
    bars = bar_payload.get("bars") or []
    params = _params_from_query(request)

    density = str(params.get("density", "normal")).lower()
    annotations: Dict[str, Any] = {}
    failures: Dict[str, str] = {}
    for name in names:
        fn, label = THEORIES[name]
        # Cache hit per (theory, ticker, window, params) — same key as
        # the single-theory endpoint, so a multi call warms / shares.
        key = _cache_key(name, ticker, window, params)
        cached = _cache_get(key)
        if cached is not None:
            annotations[name] = cached.get("annotation")
            continue
        try:
            ann = fn(bars, params=params)
            ann_dict = ann.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("theory %s failed for %s (multi)", name, ticker)
            failures[name] = str(exc)
            continue
        _apply_density_filter(ann_dict, density)
        annotations[name] = ann_dict
        single_payload = {
            "theory": name, "label": label,
            "ticker": ticker.upper(), "window": window,
            "bars": bars, "annotation": ann_dict,
            "bar_source": bar_payload.get("source"),
            "bar_interval": bar_payload.get("interval"),
            "saved": _load_saved(name, ticker, window),
        }
        _cache_put(key, single_payload)

    return {
        "ticker": ticker.upper(),
        "window": window,
        "bars": bars,
        "bar_source": bar_payload.get("source"),
        "bar_interval": bar_payload.get("interval"),
        "bar_count": len(bars),
        "min_bars_expected": bar_payload.get("min_bars_expected"),
        "theories": names,
        "annotations": annotations,
        "failures": failures,
        **_live_payload_tag(live),
    }


@router.get("/{theory}/{ticker}")
def analyse_ticker(
    theory: str,
    ticker: str,
    request: Request,
    window: str = Query("1y"),
    live: bool = Query(False),
) -> Dict[str, Any]:
    if theory not in THEORIES:
        raise HTTPException(status_code=404, detail=f"Unknown theory: {theory}")
    if window not in WINDOW_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown window: {window}")
    fn, label = THEORIES[theory]
    params = _params_from_query(request)

    key = _cache_key(theory, ticker, window, params)
    cached = _cache_get(key)
    if cached is not None:
        # Add the live tag fresh (it changes per request even on cache hit).
        out = dict(cached)
        out.update(_live_payload_tag(live))
        return out

    bar_payload = _fetch_window(ticker, window)
    bars = bar_payload.get("bars") or []
    try:
        ann = fn(bars, params=params)
    except Exception as exc:
        logger.exception("theory %s failed for %s", theory, ticker)
        raise HTTPException(status_code=500, detail=str(exc))
    ann_dict = ann.to_dict()
    density = str(params.get("density", "normal")).lower()
    _apply_density_filter(ann_dict, density)
    payload = {
        "theory": theory,
        "label": label,
        "ticker": ticker.upper(),
        "window": window,
        "bars": bars,
        "bar_source": bar_payload.get("source"),
        "bar_interval": bar_payload.get("interval"),
        "bar_count": len(bars),
        "min_bars_expected": bar_payload.get("min_bars_expected"),
        "annotation": ann_dict,
        "saved": _load_saved(theory, ticker, window),
    }
    _cache_put(key, payload)
    out = dict(payload)
    out.update(_live_payload_tag(live))
    return out


def _load_saved(theory: str, ticker: str, window: str) -> Optional[Dict[str, Any]]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(SavedTheoryAnnotation).where(
                    SavedTheoryAnnotation.theory == theory,
                    SavedTheoryAnnotation.ticker == ticker.upper(),
                    SavedTheoryAnnotation.window == window,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return row.to_dict()
    except Exception:
        logger.debug("_load_saved failed", exc_info=True)
        return None


@router.post("/{theory}/{ticker}/save")
async def save_annotation(
    theory: str,
    ticker: str,
    request: Request,
    window: str = Query("1y"),
) -> Dict[str, Any]:
    if theory not in THEORIES:
        raise HTTPException(status_code=404, detail=f"Unknown theory: {theory}")
    body = await request.json()
    annotation = body.get("annotation") if isinstance(body, dict) else None
    if not isinstance(annotation, dict):
        raise HTTPException(
            status_code=422,
            detail="Body must be {annotation: {...}} matching TheoryAnnotation schema.",
        )
    annotation_json = json.dumps(annotation, default=str)
    with session_scope() as session:
        row = session.execute(
            select(SavedTheoryAnnotation).where(
                SavedTheoryAnnotation.theory == theory,
                SavedTheoryAnnotation.ticker == ticker.upper(),
                SavedTheoryAnnotation.window == window,
            )
        ).scalar_one_or_none()
        if row is None:
            row = SavedTheoryAnnotation(
                theory=theory,
                ticker=ticker.upper(),
                window=window,
                annotation_json=annotation_json,
                created_by=str(body.get("created_by") or "operator"),
            )
            session.add(row)
        else:
            row.annotation_json = annotation_json
            row.created_by = str(body.get("created_by") or row.created_by or "operator")
        session.flush()
        out = row.to_dict()
    # Invalidate the cache for this (theory, ticker, window) since the
    # operator-edited annotation supersedes the auto one.
    keys = [k for k in _CACHE.keys()
            if k.startswith(f"{theory}|{ticker.upper()}|{window}|")]
    for k in keys:
        _CACHE.pop(k, None)
    return {"ok": True, "saved": out}


@router.delete("/{theory}/{ticker}/saved")
def delete_saved(
    theory: str,
    ticker: str,
    window: str = Query("1y"),
) -> Dict[str, Any]:
    if theory not in THEORIES:
        raise HTTPException(status_code=404, detail=f"Unknown theory: {theory}")
    with session_scope() as session:
        row = session.execute(
            select(SavedTheoryAnnotation).where(
                SavedTheoryAnnotation.theory == theory,
                SavedTheoryAnnotation.ticker == ticker.upper(),
                SavedTheoryAnnotation.window == window,
            )
        ).scalar_one_or_none()
        if row is None:
            return {"ok": True, "removed": False}
        session.delete(row)
    keys = [k for k in _CACHE.keys()
            if k.startswith(f"{theory}|{ticker.upper()}|{window}|")]
    for k in keys:
        _CACHE.pop(k, None)
    return {"ok": True, "removed": True}
