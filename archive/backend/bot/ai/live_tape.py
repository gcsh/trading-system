"""MITS Phase 7.4 — Live tape analyzer + Claude context assembly.

Composes the compact JSON blob the Opportunity Brain prompt injects on
every Claude call. Stays under 3 KB so prompt-cache hits remain cheap.

The shape is deliberately FLAT and string-keyed so the Claude prompt
can JSON.dumps it directly and the model has no trouble parsing it.
Missing inputs collapse to ``null`` rather than raising — the Brain is
trained to reason about absent data ("breadth unavailable, lean on
flow + sector dispersion instead").
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# Full 11-sector basket — operators expect every major US sector ETF
# represented in the rotation panel.
SECTOR_ETFS = (
    "XLK", "XLF", "XLE", "XLY", "XLU",
    "XLV", "XLP", "XLI", "XLB", "XLRE", "XLC",
)


def _safe_float(value: Any, *, ndigits: int = 4) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _spy_tick_samples(market_data: Any, *, max_samples: int) -> List[Dict[str, Any]]:
    """Best-effort SPY 5-min sampled ticks.

    The shape produced is a list of ``{"t": iso, "p": price, "spread_bps": ...}``
    dicts, capped at ``max_samples`` so the JSON stays compact.
    """
    out: List[Dict[str, Any]] = []
    if market_data is None or not hasattr(market_data, "snapshot"):
        return out
    try:
        snap = market_data.snapshot("SPY").data
        bars = (snap.get("intraday_bars")
                or snap.get("intraday_history")
                or snap.get("recent_bars")
                or [])
        if not isinstance(bars, list):
            return out
        # Downsample to fit budget.
        if len(bars) > max_samples:
            stride = max(1, len(bars) // max_samples)
            bars = bars[::stride][:max_samples]
        for b in bars:
            ts = b.get("timestamp") or b.get("t")
            price = b.get("close") or b.get("price") or b.get("c")
            bid = b.get("bid")
            ask = b.get("ask")
            spread_bps: Optional[float] = None
            if bid and ask and float(price or 0) > 0:
                try:
                    spread_bps = round(
                        (float(ask) - float(bid)) / float(price) * 10_000, 2)
                except Exception:
                    spread_bps = None
            out.append({
                "t": str(ts) if ts else None,
                "p": _safe_float(price, ndigits=4),
                "spread_bps": spread_bps,
            })
    except Exception:
        logger.debug("live_tape: spy ticks failed", exc_info=True)
    return out


def _sector_rotation(market_data: Any) -> Dict[str, Optional[float]]:
    """30-minute return per sector ETF."""
    out: Dict[str, Optional[float]] = {}
    if market_data is None or not hasattr(market_data, "snapshot"):
        return {sym: None for sym in SECTOR_ETFS}
    for sym in SECTOR_ETFS:
        try:
            snap = market_data.snapshot(sym).data
            pct = (snap.get("intraday_30m_pct")
                   or snap.get("intraday_pct_change")
                   or snap.get("change_pct"))
            out[sym] = _safe_float(pct, ndigits=3)
        except Exception:
            out[sym] = None
    return out


def _vix_curve(market_data: Any) -> Dict[str, Optional[float]]:
    """Spot VIX plus contango / backwardation pct vs the front-month VX
    contracts when available. yfinance doesn't surface VX1!/VX2 cleanly,
    so we report what we have and Claude reasons over absence."""
    out: Dict[str, Optional[float]] = {
        "vix_spot": None, "vx1": None, "vx2": None,
        "contango_pct": None, "curve_slope": None,
    }
    if market_data is None or not hasattr(market_data, "snapshot"):
        return out
    try:
        snap = market_data.snapshot("SPY").data
        out["vix_spot"] = _safe_float(snap.get("vix"), ndigits=2)
        out["vx1"] = _safe_float(snap.get("vx1"), ndigits=2)
        out["vx2"] = _safe_float(snap.get("vx2"), ndigits=2)
        out["contango_pct"] = _safe_float(
            snap.get("vix_contango_pct"), ndigits=3
        )
        out["curve_slope"] = _safe_float(
            snap.get("vix_curve_slope"), ndigits=3
        )
    except Exception:
        pass
    return out


def _unusual_flow(top_n: int) -> List[Dict[str, Any]]:
    """Top-N highest-conviction flow alerts within the last 15 min.

    Reads via the FlowSeen / flowintel persistence layer when present;
    returns ``[]`` when no flow data is wired (tests / cold start).
    """
    try:
        from backend.bot.flowintel import recent_unusual_flow  # type: ignore
    except Exception:
        try:
            # Fallback: read directly from flow_seen rows.
            from backend.db import session_scope
            from sqlalchemy import desc
            from backend.models.flow_seen import FlowSeen
            since = datetime.utcnow() - timedelta(minutes=15)
            out: List[Dict[str, Any]] = []
            with session_scope() as s:
                rows = (s.query(FlowSeen)
                        .filter(FlowSeen.seen_at >= since)
                        .order_by(desc(FlowSeen.seen_at))
                        .limit(top_n * 4)
                        .all())
                for r in rows:
                    out.append({
                        "ticker": getattr(r, "ticker", None),
                        "kind": getattr(r, "kind", "flow"),
                        "premium": _safe_float(
                            getattr(r, "premium", None), ndigits=0),
                        "at": (getattr(r, "seen_at", None)
                               .isoformat() if getattr(r, "seen_at", None)
                               else None),
                    })
            # Sort by premium desc, then take top_n.
            out.sort(key=lambda d: d.get("premium") or 0.0, reverse=True)
            return out[:top_n]
        except Exception:
            return []
    try:
        rows = recent_unusual_flow(window_minutes=15, limit=top_n) or []
        return rows[:top_n]
    except Exception:
        return []


def _dealer_gex_flip(market_data: Any) -> Optional[float]:
    """Dealer gamma flip point from HeatSeeker. Returns None when no
    GEX cache is warm yet."""
    try:
        from backend.bot.heatseeker import gex_snapshot  # type: ignore
    except Exception:
        try:
            snap = market_data.snapshot("SPY").data if market_data else {}
            return _safe_float(snap.get("gamma_flip"), ndigits=2)
        except Exception:
            return None
    try:
        gex = gex_snapshot("SPY") or {}
        return _safe_float(gex.get("gamma_flip") or gex.get("flip_point"),
                              ndigits=2)
    except Exception:
        return None


def _breadth_and_pcr(market_data: Any) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {"breadth": None, "put_call": None}
    # Breadth via the cached snapshot.
    try:
        from backend.bot.breadth import latest as _breadth_latest
        row = _breadth_latest()
        if row is not None and row.advancers is not None and row.decliners is not None:
            total = float(row.advancers) + float(row.decliners)
            if total > 0:
                out["breadth"] = round(float(row.advancers) / total, 3)
    except Exception:
        pass
    if market_data is not None and hasattr(market_data, "snapshot"):
        try:
            snap = market_data.snapshot("SPY").data
            pcr = snap.get("put_call_ratio") or snap.get("pcc_ratio")
            out["put_call"] = _safe_float(pcr, ndigits=3)
        except Exception:
            pass
    return out


def _watchlist_top(market_data: Any, *, top_n: int) -> List[Dict[str, Any]]:
    """Top-N watchlist tickers' current spot + intraday range."""
    out: List[Dict[str, Any]] = []
    if market_data is None or not hasattr(market_data, "snapshot"):
        return out
    try:
        from backend.db import session_scope
        from backend.models.watchlist import WatchlistItem
        with session_scope() as s:
            items = [w.ticker.upper() for w in s.query(WatchlistItem).all()
                     if w.ticker]
    except Exception:
        items = []
    # Truncate so we don't burn quote budget when watchlist is huge.
    items = items[:top_n]
    for tk in items:
        try:
            snap = market_data.snapshot(tk).data
            out.append({
                "ticker": tk,
                "price": _safe_float(snap.get("price"), ndigits=2),
                "day_high": _safe_float(snap.get("day_high"), ndigits=2),
                "day_low": _safe_float(snap.get("day_low"), ndigits=2),
                "change_pct": _safe_float(
                    snap.get("change_pct"), ndigits=3),
            })
        except Exception:
            continue
    return out


def assemble_live_context(regime_state: str,
                            *, market_data: Any = None) -> Dict[str, Any]:
    """Build the compact JSON blob the Opportunity Brain prompt injects.

    Caller passes the current ``regime_state`` so the blob includes it
    inline — the prompt template uses the same string for the
    "regime says X" framing. ``market_data`` is the existing
    ``MarketDataAdapter`` from the engine; tests pass a mock or None
    to exercise the fallback branches.
    """
    spy_samples_n = int(TUNABLES.live_tape_spy_tick_samples)
    flow_n = int(TUNABLES.live_tape_unusual_flow_topn)
    watch_n = int(TUNABLES.live_tape_watchlist_topn)

    spy_ticks = _spy_tick_samples(market_data, max_samples=spy_samples_n)
    sectors = _sector_rotation(market_data)
    vix = _vix_curve(market_data)
    flow = _unusual_flow(flow_n)
    gex_flip = _dealer_gex_flip(market_data)
    breadth_pcr = _breadth_and_pcr(market_data)
    watch = _watchlist_top(market_data, top_n=watch_n)

    return {
        "regime_state": regime_state,
        "as_of": datetime.utcnow().isoformat(),
        "spy_ticks_5min": spy_ticks,
        "sector_30m_returns": sectors,
        "vix_curve": vix,
        "unusual_flow": flow,
        "dealer_gex_flip": gex_flip,
        "breadth": breadth_pcr.get("breadth"),
        "put_call_ratio": breadth_pcr.get("put_call"),
        "watchlist_top": watch,
    }


__all__ = ["SECTOR_ETFS", "assemble_live_context"]
