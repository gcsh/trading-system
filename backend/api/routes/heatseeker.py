"""Heatseeker (GEX gamma-exposure) endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Query

from backend.bot.signals.gex import gex, gex_by_expiry, is_opex_day, is_opex_week, regime_history
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.config import load_config

router = APIRouter(prefix="/heatseeker", tags=["heatseeker"])


def _bucket_label_for_dte(dte: int) -> str:
    """Map a DTE integer to the Phase 19 heatmap-matrix label.

    Buckets (per Phase 19 GEX Dashboard spec):
        0DTE  : dte == 0
        1W    : 1  <= dte <= 7
        2W    : 8  <= dte <= 14
        3W    : 15 <= dte <= 21
        1M    : 22 <= dte <= 35
        >1M   : dte >  35
    """
    d = int(dte)
    if d <= 0:
        return "0DTE"
    if d <= 7:
        return "1W"
    if d <= 14:
        return "2W"
    if d <= 21:
        return "3W"
    if d <= 35:
        return "1M"
    return ">1M"


def _resolve_expiration(label: Optional[str]) -> Optional[int]:
    """Map UI dropdown labels (``0d``, ``1d``, … ``60d``, ``all``) to
    the ``max_dte`` integer passed into the GEX pipeline.

    ``all`` returns ``None`` to preserve the legacy "front 45 days"
    behaviour. Unknown labels also yield ``None`` so old clients keep
    working.
    """
    if not label:
        return None
    key = label.strip().lower()
    if key in {"all", "any", ""}:
        return None
    for tag, dte in TUNABLES.gex_expiration_buckets:
        if tag == key:
            # `all` returns None above; the table maps it to 45 only for
            # documentation, but the public contract is "all = unfiltered".
            return None if tag == "all" else int(dte)
    # Accept raw integers (e.g. "21") too.
    try:
        return max(0, int(key.rstrip("d")))
    except Exception:
        return None


def _default_tickers() -> List[str]:
    try:
        with session_scope() as session:
            return (load_config(session).get("tickers") or ["SPY"])
    except Exception:
        return ["SPY"]


@router.get("/regime")
async def regime(
    symbol: str = "SPY",
    expiration: str = Query("all", description=(
        "DTE bucket: 0d / 1d / 5d / 7d / 14d / 30d / 60d / all. Default 'all' "
        "keeps the legacy front-45-day window."
    )),
) -> dict:
    """Dealer-gamma regime summary for one symbol (default SPY)."""
    max_dte = _resolve_expiration(expiration)
    g = gex(symbol.upper(), max_dte=max_dte)
    return {"ticker": g.ticker, "spot_price": g.spot_price, "dealer_regime": g.dealer_regime,
            "gamma_flip": g.gamma_flip, "call_wall": g.call_wall, "put_wall": g.put_wall,
            "prev_call_wall": g.prev_call_wall, "prev_gamma_flip": g.prev_gamma_flip,
            "flip_direction": g.flip_direction, "stale": g.stale,
            "opex_day": is_opex_day(), "opex_week": is_opex_week(),
            "source": g.source, "ok": g.ok}


@router.get("/regime/history")
async def regime_history_route(
    symbol: str = "SPY",
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """Stored 15-min GEX regime snapshots for a symbol, oldest→newest (#8)."""
    sym = symbol.upper()
    return {"ticker": sym, "history": regime_history(sym, limit)}


@router.get("/batch")
async def batch(symbols: str = Query("", description="comma-separated; default = configured tickers")) -> List[dict]:
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or _default_tickers()
    out = []
    for s in syms[:12]:
        g = gex(s)
        out.append({"ticker": g.ticker, "spot_price": g.spot_price, "call_wall": g.call_wall,
                    "put_wall": g.put_wall, "gamma_flip": g.gamma_flip, "dealer_regime": g.dealer_regime,
                    "source": g.source, "ok": g.ok})
    return out


@router.get("/expirations")
async def list_expirations() -> dict:
    """Operator-facing list of DTE buckets for the Heatseeker dropdown
    (MITS Phase 9.3). The first label that resolves to ``None``
    ("all") preserves the legacy unfiltered behaviour."""
    return {
        "buckets": [{"label": tag, "max_dte": (None if tag == "all" else int(dte))}
                      for tag, dte in TUNABLES.gex_expiration_buckets],
        "default": "all",
    }


@router.get("/{ticker}")
async def heatseeker(
    ticker: str,
    expiration: str = Query("all", description=(
        "DTE bucket: 0d / 1d / 5d / 7d / 14d / 30d / 60d / all. Default 'all' "
        "preserves the legacy front-45-day aggregation."
    )),
) -> dict:
    """Full GEX result for a ticker (spot, walls, flip, regime, per-strike GEX).

    ``expiration`` is the MITS-P9.3 DTE bucket: ``0d``, ``1d``, ``5d``,
    ``7d``, ``14d``, ``30d``, ``60d``, ``all`` (default).
    """
    max_dte = _resolve_expiration(expiration)
    try:
        payload = gex(ticker.upper(), max_dte=max_dte).to_dict()
    except Exception as exc:
        # MITS-P9.5 — degrade gracefully when an upstream vendor (yfinance
        # Invalid Crumb, ThetaData terminal down, etc.) raises. UI shows
        # ok=False + note instead of an opaque 500.
        payload = {
            "ticker": ticker.upper(),
            "ok": False,
            "source": "none",
            "note": f"upstream data unavailable: {type(exc).__name__}",
            "spot_price": 0.0,
            "gex_by_strike": [],
        }
    payload["expiration"] = expiration
    payload["max_dte"] = max_dte
    return payload


@router.get("/{ticker}/history")
async def heatseeker_history(ticker: str, limit: int = Query(60, ge=1, le=1000)) -> dict:
    """Path-style alias matching ``/heatseeker/{ticker}/history`` —
    consumed by the Long Gamma regime ribbon (Item #14)."""
    sym = ticker.upper()
    return {"ticker": sym, "items": regime_history(sym, limit)}


@router.get("/{ticker}/by-expiry")
async def heatseeker_by_expiry(
    ticker: str,
    max_expiries: int = Query(6, ge=1, le=12),
) -> dict:
    """Per-expiry GEX breakdown (Item #14 third panel) — stacked-bars
    decomposition of gamma exposure across near-term expirations."""
    return gex_by_expiry(ticker.upper(), max_expiries=max_expiries)


@router.get("/multi/{ticker}")
async def heatseeker_multi(
    ticker: str,
    max_expiries: int = Query(12, ge=1, le=20),
) -> dict:
    """Phase 19 — multi-expiration GEX matrix for the heatmap dashboard.

    Returns one row per near-term expiration plus a per-strike GEX
    breakdown (call / put / net) suitable for a rows=expirations,
    cols=strikes heatmap. Each expiration is tagged with a human
    bucket label (``0DTE`` / ``1W`` / ``2W`` / ``3W`` / ``1M`` /
    ``>1M``) so the UI can group/stack by bucket without recomputing
    the DTE math.

    Reuses ``gex_by_expiry`` (already cached internally by the GEX
    pipeline) — never re-fetches the chain. Failures degrade gracefully
    to ``expirations=[]`` so the UI shows an empty-matrix banner instead
    of a 500.
    """
    sym = ticker.upper()
    try:
        raw = gex_by_expiry(sym, max_expiries=max_expiries)
    except Exception as exc:
        return {
            "ticker": sym,
            "spot_price": 0.0,
            "expirations": [],
            "note": f"upstream data unavailable: {type(exc).__name__}",
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    spot_price = float(raw.get("spot") or 0.0)
    expirations_out: List[dict] = []
    for exp in (raw.get("expiries") or []):
        try:
            dte = int(exp.get("dte") or 0)
        except (TypeError, ValueError):
            dte = 0
        totals = exp.get("totals") or {}
        try:
            call_total = float(totals.get("call_gex") or 0.0)
            put_total = float(totals.get("put_gex") or 0.0)
            net_total = float(totals.get("net_gex") or 0.0)
        except (TypeError, ValueError):
            call_total = put_total = net_total = 0.0
        strikes = exp.get("strikes") or []
        gex_by_strike: List[dict] = []
        for s in strikes:
            try:
                gex_by_strike.append({
                    "strike": float(s.get("strike") or 0.0),
                    "call_gex": float(s.get("call_gex") or 0.0),
                    "put_gex": float(s.get("put_gex") or 0.0),
                    "net_gex": float(s.get("net_gex") or 0.0),
                })
            except (TypeError, ValueError):
                continue
        expirations_out.append({
            "expiry": exp.get("expiry"),
            "dte": dte,
            "label": _bucket_label_for_dte(dte),
            "call_gex_total": round(call_total, 2),
            "put_gex_total": round(put_total, 2),
            "net_gex_total": round(net_total, 2),
            "gex_by_strike": gex_by_strike,
        })
    return {
        "ticker": sym,
        "spot_price": round(spot_price, 2),
        "expirations": expirations_out,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
