"""Flowseeker — institutional options flow, through the standard pipeline.

Raw → Clean → Normalize → Validate → Enrich:
- Raw: Unusual Whales API (primary, needs UNUSUAL_WHALES_API_KEY); fallback
  scans the yfinance options chain for unusual volume (volume > N× OI).
- Clean: drop malformed / sub-threshold rows.
- Normalize: standard FlowAlert shape.
- Validate: sane fields.
- Enrich: urgency score + sentiment.

No key / API down → empty flow; existing strategies are unaffected.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from backend.bot.data.pipeline import run_pipeline
from backend.config import SETTINGS, TUNABLES

logger = logging.getLogger(__name__)

_CACHE: Dict[str, tuple] = {}
_DP_CACHE: tuple = (0.0, [])
_UW_BASE = "https://api.unusualwhales.com/api"


@dataclass
class FlowAlert:
    ticker: str
    timestamp: str
    strike: float
    expiry: str
    premium: float
    option_type: str          # call | put
    trade_type: str           # sweep | block | darkpool
    sentiment: str            # bullish | bearish
    size: int
    urgency_score: float      # 0.0 - 1.0
    session: str = "regular"  # pre_market | regular | after_hours

    def to_dict(self) -> dict:
        return asdict(self)

    def alert_id(self) -> str:
        """Stable ID for dedup — same trade always hashes to the same value."""
        raw = f"{self.ticker}|{self.strike}|{self.expiry}|{self.option_type}|{self.premium}|{self.timestamp}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _et(timestamp: str) -> Optional[datetime]:
    """Parse an ISO timestamp into Eastern time (the US options session zone)."""
    try:
        ts = datetime.fromisoformat(timestamp)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return ts.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return ts.astimezone(timezone.utc)


def _session_for(timestamp: str) -> str:
    """Tag a trade pre_market / regular / after_hours from its ET clock time."""
    local = _et(timestamp)
    if local is None:
        return "regular"
    minutes = local.hour * 60 + local.minute
    if minutes < 9 * 60 + 30:
        return "pre_market"
    if minutes >= 16 * 60:
        return "after_hours"
    return "regular"


def _within_window(alert: "FlowAlert", minutes: int, now: Optional[datetime] = None) -> bool:
    """True if the alert is recent enough to count toward conviction (#5)."""
    try:
        ts = datetime.fromisoformat(alert.timestamp)
    except Exception:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - ts) <= timedelta(minutes=minutes)


def _f(v) -> float:
    """NaN-safe float (yfinance returns NaN for empty volume/OI)."""
    try:
        f = float(v)
        return 0.0 if f != f else f
    except Exception:
        return 0.0


def _urgency(premium: float, vol: float, oi: float) -> float:
    voi = vol / max(oi, 1.0)
    score = 0.45 * min(voi / 8.0, 1.0) + 0.55 * min(premium / 250_000.0, 1.0)
    return round(max(0.0, min(1.0, score)), 3)


# ── raw sources ──────────────────────────────────────────────────────────────

def _uw_get(path: str, params: Optional[dict] = None):
    if not SETTINGS.unusual_whales_api_key:
        return None
    try:
        import httpx

        r = httpx.get(f"{_UW_BASE}{path}", params=params or {},
                      headers={"Authorization": f"Bearer {SETTINGS.unusual_whales_api_key}"}, timeout=8.0)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as exc:
        logger.warning("unusual whales %s failed: %s", path, exc)
        return None


def _uw_flow(ticker: Optional[str]) -> Optional[List[dict]]:
    j = _uw_get("/option-trades/flow-alerts", {"ticker": ticker} if ticker else None)
    if not j:
        return None
    items = j.get("data") or j if isinstance(j, list) else j.get("data")
    if not isinstance(items, list):
        return None
    rows = []
    for a in items:
        try:
            rows.append({
                "ticker": (a.get("ticker") or a.get("underlying") or ticker or "").upper(),
                "strike": float(a.get("strike") or 0),
                "expiry": str(a.get("expiry") or a.get("expiration") or ""),
                "premium": float(a.get("premium") or a.get("total_premium") or 0),
                "option_type": (a.get("type") or a.get("option_type") or "call").lower(),
                "trade_type": (a.get("rule_name") or a.get("trade_type") or "sweep").lower(),
                "sentiment": (a.get("sentiment") or ("bullish" if (a.get("type") or "call").lower() == "call" else "bearish")).lower(),
                "size": int(a.get("size") or a.get("volume") or 0),
                "volume": float(a.get("volume") or 0),
                "oi": float(a.get("open_interest") or 0),
            })
        except Exception:
            continue
    return rows


def _yf_unusual(ticker: str) -> List[dict]:
    """Fallback: flag contracts trading at unusual volume vs open interest."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    try:
        exps = list(t.options or [])[:1]   # nearest expiry only — keep the scan fast
    except Exception:
        # yfinance auth/crumb failures throw here; treat as "no flow available".
        return []
    ratio = TUNABLES.flow_volume_oi_ratio
    rows: List[dict] = []
    for exp in exps:
        try:
            chain = t.option_chain(exp)
        except Exception:
            continue
        # yfinance returns None for chain.calls / chain.puts when the
        # provider responds with a degraded payload (common for thin
        # names, after-hours, or transient API issues). Guard explicitly
        # so we don't fall into the broad except-AttributeError handler
        # that spams the warnings buffer once per ticker per cycle.
        for df_, otype in ((chain.calls, "call"), (chain.puts, "put")):
            if df_ is None or getattr(df_, "empty", True):
                continue
            for _, row in df_.iterrows():
                vol = _f(row.get("volume"))
                oi = _f(row.get("openInterest"))
                last = _f(row.get("lastPrice"))
                strike = _f(row.get("strike"))
                if oi <= 0 or vol <= 0 or vol < ratio * oi:
                    continue
                premium = vol * last * 100
                if premium < TUNABLES.flow_min_premium:
                    continue
                rows.append({
                    "ticker": ticker.upper(), "strike": strike, "expiry": exp, "premium": round(premium, 2),
                    "option_type": otype, "trade_type": "sweep" if vol > 5 * oi else "block",
                    "sentiment": "bullish" if otype == "call" else "bearish",
                    "size": int(vol), "volume": vol, "oi": oi,
                })
    return rows


# ── pipeline stages ──────────────────────────────────────────────────────────

def _clean(rows: List[dict]) -> List[dict]:
    return [r for r in rows if r.get("ticker") and r.get("premium", 0) >= TUNABLES.flow_min_premium and r.get("strike", 0) > 0]


def _normalize(rows: List[dict]) -> List[dict]:
    out = []
    for r in rows:
        out.append({
            "ticker": r["ticker"].upper(),
            "timestamp": r.get("timestamp") or _now(),
            "strike": round(float(r["strike"]), 2),
            "expiry": str(r.get("expiry") or ""),
            "premium": round(float(r["premium"]), 2),
            "option_type": r.get("option_type", "call"),
            "trade_type": r.get("trade_type", "sweep"),
            "sentiment": r.get("sentiment", "bullish"),
            "size": int(r.get("size") or 0),
            "volume": float(r.get("volume") or r.get("size") or 0),
            "oi": float(r.get("oi") or 0),
            "urgency_score": r.get("urgency_score"),
        })
    return out


def _validate(rows: List[dict]) -> List[str]:
    issues: List[str] = []
    for r in rows:
        if r["option_type"] not in ("call", "put"):
            issues.append("! bad option_type")
            break
    return issues


def _enrich(rows: List[dict]) -> List[FlowAlert]:
    alerts: List[FlowAlert] = []
    for r in rows:
        score = r.get("urgency_score")
        if score is None:
            score = _urgency(r["premium"], r["volume"], r["oi"])
        alerts.append(FlowAlert(
            ticker=r["ticker"], timestamp=r["timestamp"], strike=r["strike"], expiry=r["expiry"],
            premium=r["premium"], option_type=r["option_type"], trade_type=r["trade_type"],
            sentiment=r["sentiment"], size=r["size"], urgency_score=float(score),
            session=_session_for(r["timestamp"]),
        ))
    alerts.sort(key=lambda a: a.urgency_score, reverse=True)
    return alerts


# ── public API ───────────────────────────────────────────────────────────────

def flow_for(ticker: str) -> List[FlowAlert]:
    """Unusual flow for one ticker (cached). Returns [] when nothing qualifies."""
    ticker = ticker.upper()
    now = time.monotonic()
    hit = _CACHE.get(ticker)
    if hit and (now - hit[0]) < TUNABLES.flow_cache_ttl:
        return hit[1]

    # Never raise: a data-source failure must degrade to empty flow, never a 500
    # (the endpoints + engine snapshot both depend on this contract).
    alerts: List[FlowAlert] = []
    try:
        raw = _uw_flow(ticker)
        if raw is None:
            raw = _yf_unusual(ticker)
        if raw:
            res = run_pipeline(source="flow", fetch=lambda: raw, clean=_clean, normalize=_normalize,
                               validate=_validate, enrich=_enrich)
            alerts = res.data if res.ok else []
    except Exception:
        logger.warning("flow_for(%s) failed; serving empty flow", ticker, exc_info=True)
        alerts = []
    _CACHE[ticker] = (now, alerts)
    return alerts


def live_flow(tickers: List[str], limit: int = 50) -> List[FlowAlert]:
    out: List[FlowAlert] = []
    for tk in tickers:
        out.extend(flow_for(tk))
    out.sort(key=lambda a: a.urgency_score, reverse=True)
    return out[:limit]


def darkpool() -> List[dict]:
    j = _uw_get("/darkpool/recent")
    items = (j or {}).get("data") if isinstance(j, dict) else (j if isinstance(j, list) else None)
    return items or []


def _recent_darkpool() -> List[dict]:
    """Cached darkpool prints for cross-referencing — does not touch darkpool()."""
    global _DP_CACHE
    now = time.monotonic()
    if (now - _DP_CACHE[0]) < TUNABLES.flow_cache_ttl and _DP_CACHE[1]:
        return _DP_CACHE[1]
    try:
        prints = darkpool()
    except Exception:
        prints = []
    _DP_CACHE = (now, prints)
    return prints


def _darkpool_confirms(ticker: str, bullish_sweep_recent: bool) -> bool:
    """True when a >$1M dark-pool print backs a recent bullish sweep (#7)."""
    if not bullish_sweep_recent:
        return False
    tk = ticker.upper()
    for p in _recent_darkpool():
        try:
            sym = (p.get("ticker") or p.get("symbol") or p.get("underlying") or "").upper()
            if sym != tk:
                continue
            notional = float(p.get("premium") or p.get("notional")
                             or p.get("value") or p.get("size") or 0)
            if notional >= TUNABLES.flow_darkpool_min:
                return True
        except Exception:
            continue
    return False


def filter_unseen(alerts: List[FlowAlert]) -> List[FlowAlert]:
    """Return only alerts never pushed before, recording the new ones in SQLite.

    Stops a WebSocket reconnect (or server restart) from replaying old alerts
    (#3). Best-effort: on any DB error the alerts are returned unchanged so the
    live feed keeps working.
    """
    if not alerts:
        return []
    try:
        from backend.db import session_scope
        from backend.models.flow_seen import SeenFlowAlert

        ids = [a.alert_id() for a in alerts]
        fresh: List[FlowAlert] = []
        with session_scope() as session:
            seen = {
                r[0] for r in session.query(SeenFlowAlert.id)
                .filter(SeenFlowAlert.id.in_(ids)).all()
            }
            for a in alerts:
                aid = a.alert_id()
                if aid in seen:
                    continue
                session.add(SeenFlowAlert(id=aid))
                seen.add(aid)
                fresh.append(a)
        return fresh
    except Exception:
        logger.debug("filter_unseen failed", exc_info=True)
        return alerts


def summary(alerts: List[FlowAlert]) -> dict:
    bullish = sum(1 for a in alerts if a.sentiment == "bullish")
    bearish = sum(1 for a in alerts if a.sentiment == "bearish")
    total_premium = round(sum(a.premium for a in alerts), 2)
    high_urgency = sum(1 for a in alerts if a.urgency_score >= TUNABLES.flow_urgency_threshold)
    return {"count": len(alerts), "bullish": bullish, "bearish": bearish,
            "net_sentiment": "bullish" if bullish > bearish else ("bearish" if bearish > bullish else "neutral"),
            "total_premium": total_premium, "high_urgency": high_urgency}


def flow_context(ticker: str) -> dict:
    """Compact flow fields for injection into a strategy's data snapshot.

    Only sweeps inside the conviction window (#5) count toward the boost; the
    pre-market subset (#6) and a >$1M dark-pool confirmation (#7) are surfaced
    so strategies can act on stronger, time-bounded signals.
    """
    alerts = flow_for(ticker)
    if not alerts:
        return {}
    win = TUNABLES.flow_conviction_window_minutes
    recent = [a for a in alerts if _within_window(a, win)]
    bullish_sweeps = sum(1 for a in recent if a.sentiment == "bullish" and a.trade_type == "sweep")
    bearish_sweeps = sum(1 for a in recent if a.sentiment == "bearish" and a.trade_type == "sweep")
    premarket_bullish_sweeps = sum(
        1 for a in recent
        if a.session == "pre_market" and a.sentiment == "bullish" and a.trade_type == "sweep"
    )
    return {
        "bullish_sweeps": bullish_sweeps,
        "bearish_sweeps": bearish_sweeps,
        "premarket_bullish_sweeps": premarket_bullish_sweeps,
        "darkpool_confirms": _darkpool_confirms(ticker, bullish_sweeps >= 1),
        "flow_count": len(alerts),
    }
