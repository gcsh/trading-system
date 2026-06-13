"""Heatseeker — dealer gamma exposure (GEX), through the standard pipeline.

Raw → Clean → Normalize → Validate → Enrich:
- Raw: FlashAlpha free API (primary); fallback to an options chain with greeks
  — Cboe delayed options (real gamma + OI, no key) or yfinance OI + a
  Black-Scholes gamma computed from implied vol.
- Clean: drop junk strikes, keep the nearest expiry.
- Normalize: aggregate per-strike call/put OI + gamma.
- Validate: enough strikes / open interest.
- Enrich: GEX per strike, call/put walls, gamma flip, dealer regime.

If nothing is available the result is ``ok=False`` and the bot's existing
strategies are unaffected.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from backend.bot.data.pipeline import run_pipeline
from backend.config import SETTINGS, TUNABLES

logger = logging.getLogger(__name__)

_CACHE: Dict[str, tuple] = {}
# OCC option symbol: ROOT + YYMMDD + C/P + strike(8 digits, /1000)
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ── OPEX / session helpers ─────────────────────────────────────────────────────

def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7   # Mon=0 … Fri=4
    return first + timedelta(days=offset + 14)


def is_opex_day(d: Optional[date] = None) -> bool:
    """True on options-expiration days. Every Friday carries weekly expiries and
    the 3rd Friday is monthly OPEX — dealers re-hedge heavily into expiry, so the
    bot sizes down on these days (see ``TUNABLES.opex_size_factor``)."""
    d = d or date.today()
    return d.weekday() == 4


def is_opex_week(d: Optional[date] = None) -> bool:
    """True during the Mon–Fri week containing the monthly 3rd-Friday OPEX."""
    d = d or date.today()
    tf = _third_friday(d.year, d.month)
    monday = tf - timedelta(days=tf.weekday())
    return monday <= d <= tf


def _session_start(now: Optional[datetime] = None) -> datetime:
    """09:30 ET open of the current or most-recent regular session, as UTC."""
    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(et)
    open_t = local.replace(hour=9, minute=30, second=0, microsecond=0)
    if local < open_t:
        open_t = open_t - timedelta(days=1)
    return open_t.astimezone(timezone.utc)


def _is_stale(timestamp: str, now: Optional[datetime] = None) -> bool:
    """True if a snapshot's timestamp predates the current session's open (#1)."""
    try:
        ts = datetime.fromisoformat(timestamp)
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < _session_start(now)


def _apply_deltas(out: "GEXResult", prev: Optional["GEXResult"]) -> None:
    """Record wall/flip shift vs. the previously cached snapshot (#4)."""
    if prev is None:
        return
    out.prev_call_wall = prev.call_wall
    out.prev_gamma_flip = prev.gamma_flip
    if out.gamma_flip is not None and prev.gamma_flip is not None:
        if out.gamma_flip > prev.gamma_flip:
            out.flip_direction = "up"
        elif out.gamma_flip < prev.gamma_flip:
            out.flip_direction = "down"
        else:
            out.flip_direction = "flat"


@dataclass
class GEXResult:
    ticker: str
    timestamp: str
    spot_price: float
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gamma_flip: Optional[float] = None
    dealer_regime: str = "unknown"     # long_gamma | short_gamma | unknown
    gex_by_strike: List[dict] = field(default_factory=list)
    source: str = "none"
    ok: bool = False
    note: str = ""
    stale: bool = False                # timestamp predates the current session
    prev_call_wall: Optional[float] = None
    prev_gamma_flip: Optional[float] = None
    flip_direction: Optional[str] = None   # up | down | flat vs. previous snapshot
    # Aggregate summary-card figures (Σ across all strikes).
    net_gex_total: float = 0.0
    call_gex_total: float = 0.0
    put_gex_total: float = 0.0
    total_call_oi: int = 0
    total_put_oi: int = 0
    total_oi: int = 0
    atm_iv: float = 0.0
    expected_move: Optional[float] = None      # ±$ 1-day move from ATM IV
    expected_move_pct: Optional[float] = None

    # Item #6 Tier A — institutional gamma feature suite.
    max_gamma_strike: Optional[float] = None    # peak |GEX| strike
    max_gamma_value: float = 0.0                # |GEX| there
    vol_trigger: Optional[float] = None         # SpotGamma "below = destabilizing"
    distance_to_flip: Optional[float] = None    # spot − flip; sign-carrying
    dealer_flow: str = "neutral"                # stabilizing | amplifying | neutral
    dealer_flow_intensity: float = 0.0          # |net_gex_total| as a regime strength
    pin_risk_strike: Optional[float] = None     # highest-OI strike near money
    pin_risk_distance: Optional[float] = None   # spot − pin
    pin_risk_dte_weighted: Optional[float] = None  # higher when close + near-expiry

    # Item #6 Tier B — second-order Greeks aggregates.
    total_vanna: Optional[float] = None         # Σ vanna × oi (call−put)
    total_charm: Optional[float] = None         # Σ charm × oi (call−put)

    # Item #6 Tier C — 0DTE separation.
    zero_dte_net_gex: Optional[float] = None
    zero_dte_share: Optional[float] = None      # 0DTE / total |GEX|

    def to_dict(self) -> dict:
        return asdict(self)


# ── spot price ───────────────────────────────────────────────────────────────

def _spot(ticker: str) -> float:
    """Hierarchical spot lookup — ThetaData → Alpaca → yfinance.

    MITS-P9.5 (2026-06-08): rewired through ``quote_source.get_quote`` so a
    yfinance "Invalid Crumb" / fundamentals 404 doesn't 500 the whole
    /heatseeker route. Returns 0.0 only when EVERY upstream source fails.
    """
    try:
        from backend.bot.data.quote_source import get_quote
        q = get_quote(ticker)
        if q is not None and q.price > 0:
            return float(q.price)
    except Exception:
        logger.debug("quote_source.get_quote raised for %s", ticker, exc_info=True)
    return 0.0


# ── raw chain sources (each returns [{type,strike,oi,gamma,expiry}] or None) ──

def _cboe_chain(ticker: str) -> Optional[List[dict]]:
    from curl_cffi import requests as creq

    r = creq.get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker.upper()}.json",
                 impersonate="chrome", timeout=15)
    if r.status_code != 200:
        return None
    opts = (r.json().get("data") or {}).get("options") or []
    rows: List[dict] = []
    for o in opts:
        m = _OCC_RE.match(o.get("option", ""))
        if not m:
            continue
        _, ymd, cp, strike8 = m.groups()
        try:
            rows.append({
                "type": cp,
                "strike": int(strike8) / 1000.0,
                "oi": float(o.get("open_interest") or 0),
                "gamma": float(o.get("gamma") or 0),
                "iv": float(o.get("iv") or 0),
                "expiry": f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}",
            })
        except Exception:
            continue
    return rows or None


def _bs_gamma(spot: float, strike: float, t_years: float, sigma: float, rate: float) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (spot * sigma * math.sqrt(t_years))


def _yf_chain(ticker: str, spot: float) -> Optional[List[dict]]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    exps = list(t.options or [])
    if not exps:
        return None
    expiry = exps[0]
    chain = t.option_chain(expiry)
    dte = max(1, (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days)
    t_years = dte / 365.0
    rows: List[dict] = []
    for df_, cp in ((chain.calls, "C"), (chain.puts, "P")):
        for _, row in df_.iterrows():
            try:
                iv = float(row.get("impliedVolatility") or 0)
                strike = float(row.get("strike") or 0)
                oi = float(row.get("openInterest") or 0)
            except Exception:
                continue
            rows.append({"type": cp, "strike": strike, "oi": oi, "iv": iv,
                         "gamma": _bs_gamma(spot, strike, t_years, iv, TUNABLES.risk_free_rate),
                         "expiry": expiry})
    return rows or None


def _flashalpha(ticker: str, spot: float) -> Optional[GEXResult]:
    """Primary source. Returns a ready GEXResult only if the response clearly
    matches; otherwise None so we fall back to the computed chain."""
    try:
        import httpx

        headers = {}
        if SETTINGS.flashalpha_api_key:
            headers["Authorization"] = f"Bearer {SETTINGS.flashalpha_api_key}"
        r = httpx.get(f"https://api.flashalpha.com/v1/exposure/gex/{ticker.upper()}", headers=headers, timeout=8.0)
        if r.status_code != 200:
            return None
        j = r.json()
        rows = j.get("gex_by_strike") or j.get("strikes")
        if not isinstance(rows, list) or not rows:
            return None
        return GEXResult(
            ticker=ticker, timestamp=datetime.now(timezone.utc).isoformat(),
            spot_price=float(j.get("spot_price") or spot),
            call_wall=j.get("call_wall"), put_wall=j.get("put_wall"), gamma_flip=j.get("gamma_flip"),
            dealer_regime=j.get("dealer_regime") or "unknown",
            gex_by_strike=rows, source="flashalpha", ok=True, note="flashalpha",
        )
    except Exception:
        return None


# ── pipeline stages ──────────────────────────────────────────────────────────

def _clean(rows: List[dict], max_dte: int = 45) -> List[dict]:
    """Drop junk rows, then keep only contracts inside the requested
    DTE bucket. ``max_dte`` defaults to 45 (legacy "front-month" window
    that the operator sees as `expiration=all`). Configurable from
    P9.3's expiration dropdown.
    """
    out = [r for r in rows if r.get("strike", 0) > 0 and r.get("oi", 0) >= 0 and r.get("gamma", 0) >= 0]
    cutoff = date.today() + timedelta(days=max(0, int(max_dte)))
    dated = []
    for r in out:
        try:
            if datetime.strptime(r["expiry"], "%Y-%m-%d").date() <= cutoff:
                dated.append(r)
        except Exception:
            # Rows without expiry come from non-dated chain sources;
            # keep them so the `all` bucket isn't empty when expiries
            # are unavailable.
            if max_dte >= 45:
                dated.append(r)
    return dated or out


def _normalize(rows: List[dict]) -> List[dict]:
    # Aggregate Σ(OI × gamma) per strike across the expiry window — summing each
    # contract's exposure (not max gamma) so no single 0DTE strike dominates.
    by: Dict[float, dict] = {}
    today_iso = date.today().isoformat()
    for r in rows:
        s = round(r["strike"], 2)
        d = by.setdefault(s, {"strike": s, "call_oig": 0.0, "put_oig": 0.0,
                              "call_oi": 0.0, "put_oi": 0.0, "iv": 0.0,
                              "dte": None, "expiry": None,
                              "has_zero_dte": False})
        if r["type"] == "C":
            d["call_oig"] += r["oi"] * r["gamma"]
            d["call_oi"] += r.get("oi", 0)
        else:
            d["put_oig"] += r["oi"] * r["gamma"]
            d["put_oi"] += r.get("oi", 0)
        iv = r.get("iv") or 0.0
        if iv > d["iv"]:
            d["iv"] = iv
        # Track earliest expiry / minimum DTE per strike — needed by the
        # Tier A pin-risk and Tier B vanna/charm aggregates.
        expiry = r.get("expiry")
        if expiry:
            try:
                this_dte = max(0, (datetime.strptime(expiry, "%Y-%m-%d").date()
                                        - date.today()).days)
            except Exception:
                this_dte = None
            if this_dte is not None:
                if d["dte"] is None or this_dte < d["dte"]:
                    d["dte"] = this_dte
                    d["expiry"] = expiry
                if expiry == today_iso:
                    d["has_zero_dte"] = True
    return sorted(by.values(), key=lambda d: d["strike"])


def _validate(strikes: List[dict]) -> List[str]:
    issues: List[str] = []
    if len(strikes) < 3:
        issues.append("! fewer than 3 strikes")
    if not any((s["call_oig"] + s["put_oig"]) > 0 for s in strikes):
        issues.append("! no gamma exposure")
    return issues


def _make_enrich(spot: float):
    def enrich(strikes: List[dict]) -> dict:
        s2 = spot * spot
        rows = []
        for s in strikes:
            cg = s["call_oig"] * 100 * s2 * 0.01
            pg = -(s["put_oig"] * 100 * s2 * 0.01)  # dealer-short puts = negative
            coi = int(round(s.get("call_oi", 0)))
            poi = int(round(s.get("put_oi", 0)))
            rows.append({"strike": s["strike"], "call_gex": round(cg, 2), "put_gex": round(pg, 2),
                         "net_gex": round(cg + pg, 2), "call_oi": coi, "put_oi": poi,
                         "total_oi": coi + poi,
                         "expiry": s.get("expiry"), "dte": s.get("dte"),
                         "has_zero_dte": bool(s.get("has_zero_dte"))})
        # Walls + flip are only meaningful near the money; ignore deep-OTM noise.
        band = [r for r in rows if 0.7 * spot <= r["strike"] <= 1.3 * spot] or rows
        call_wall = max(band, key=lambda r: r["call_gex"])["strike"] if band else None
        put_wall = min(band, key=lambda r: r["put_gex"])["strike"] if band else None
        # Regime from the sign of total near-money net GEX (positive = dealers
        # net long gamma = dampening; negative = short gamma = amplifying).
        total_net = sum(r["net_gex"] for r in band)
        regime = "long_gamma" if total_net >= 0 else "short_gamma"
        # Gamma flip = the strike where cumulative net GEX crosses negative→
        # positive; else the strike where cumulative is closest to zero.
        flip, cum, best = None, 0.0, (float("inf"), None)
        for r in band:
            prev = cum
            cum += r["net_gex"]
            if prev < 0 <= cum and flip is None:
                flip = r["strike"]
            if abs(cum) < best[0]:
                best = (abs(cum), r["strike"])
        if flip is None:
            flip = best[1]
        # Aggregate summary figures (the top stat-cards).
        call_gex_total = round(sum(r["call_gex"] for r in rows), 2)
        put_gex_total = round(sum(r["put_gex"] for r in rows), 2)
        net_gex_total = round(call_gex_total + put_gex_total, 2)
        total_call_oi = sum(r["call_oi"] for r in rows)
        total_put_oi = sum(r["put_oi"] for r in rows)
        # 1-day expected move from the nearest-strike (ATM) implied vol.
        atm = min(strikes, key=lambda s: abs(s["strike"] - spot)) if strikes else None
        atm_iv = round(float(atm.get("iv", 0.0)), 4) if atm else 0.0
        em = round(spot * atm_iv * math.sqrt(1.0 / 252.0), 2) if atm_iv > 0 else None
        em_pct = round(atm_iv * math.sqrt(1.0 / 252.0) * 100, 2) if atm_iv > 0 else None

        # ── Item #6 Tier A ─────────────────────────────────────────────
        max_gamma_strike: Optional[float] = None
        max_gamma_value: float = 0.0
        if band:
            peak = max(band, key=lambda r: abs(r["net_gex"]))
            max_gamma_strike = peak["strike"]
            max_gamma_value = round(abs(peak["net_gex"]), 2)
        distance_to_flip: Optional[float] = (
            round(spot - flip, 2) if (flip is not None and spot > 0) else None
        )
        # SpotGamma's "Vol Trigger": the gamma-flip level. Spot below it
        # implies dealer hedging is destabilizing; above it, stabilizing.
        vol_trigger: Optional[float] = flip
        # Dealer hedging flow direction. Long-gamma regimes dampen moves
        # (dealers buy dips / sell rips). Short-gamma regimes amplify.
        if regime == "long_gamma":
            dealer_flow = "stabilizing"
        elif regime == "short_gamma":
            dealer_flow = "amplifying"
        else:
            dealer_flow = "neutral"
        dealer_flow_intensity = round(abs(net_gex_total), 2)
        # Pin risk — highest total-OI strike inside the near-money band,
        # distance from spot, weighted by 1 / (nearest-DTE so 0DTE pins
        # punch hardest). The chain may carry expiries; default to dte=7
        # when none present.
        pin = max(band, key=lambda r: r["total_oi"]) if band else None
        pin_risk_strike = pin["strike"] if pin else None
        pin_risk_distance = (
            round(spot - pin["strike"], 2) if (pin and spot > 0) else None
        )
        # nearest DTE — pull from any strike's first expiry if present.
        try:
            dte_min = min(
                (s.get("dte") for s in strikes if s.get("dte")),
                default=7,
            )
            dte_min = max(1, int(dte_min))
        except Exception:
            dte_min = 7
        if pin_risk_distance is not None:
            denom = max(0.1, abs(pin_risk_distance))
            pin_risk_dte_weighted = round((1.0 / denom) * (7.0 / dte_min), 4)
        else:
            pin_risk_dte_weighted = None

        # ── Item #6 Tier B ─────────────────────────────────────────────
        # Vanna ≈ ∂Gamma/∂σ = pdf(d1) · d2 / σ (sign convention by call/put).
        # Charm ≈ ∂Delta/∂t — for calls: -pdf(d1) · (r + d1·σ²/2) / (σ·√T) (approx).
        # Aggregate over the chain weighted by OI; sign netted as "calls −
        # puts". This is informative as a single scalar (institutional
        # convention).
        total_vanna = 0.0
        total_charm = 0.0
        any_vc = False
        for s in strikes:
            iv = float(s.get("iv") or 0.0)
            K = float(s.get("strike") or 0.0)
            T = max(1, int(s.get("dte") or dte_min)) / 365.0
            coi = int(s.get("call_oi") or 0)
            poi = int(s.get("put_oi") or 0)
            if iv <= 0 or K <= 0 or T <= 0 or spot <= 0:
                continue
            r_rate = float(getattr(TUNABLES, "risk_free_rate", 0.045))
            sigma_sqrt_t = iv * math.sqrt(T)
            d1 = (math.log(spot / K) + (r_rate + 0.5 * iv * iv) * T) / sigma_sqrt_t
            d2 = d1 - sigma_sqrt_t
            pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
            vanna_per = -pdf_d1 * d2 / iv  # per-contract approx
            # Charm — same sign for calls, opposite for puts (we net OI).
            charm_per = -pdf_d1 * (
                2.0 * r_rate * T - d2 * sigma_sqrt_t
            ) / (2.0 * T * sigma_sqrt_t)
            total_vanna += vanna_per * (coi - poi) * 100
            total_charm += charm_per * (coi - poi) * 100
            any_vc = True
        total_vanna = round(total_vanna, 4) if any_vc else None
        total_charm = round(total_charm, 4) if any_vc else None

        # ── Item #6 Tier C ─────────────────────────────────────────────
        # 0DTE share: normalize() flags strikes whose chain contained a
        # contract expiring today.
        zero_dte_net_gex: Optional[float] = None
        zero_dte_share: Optional[float] = None
        zero_strikes = [s for s in strikes if s.get("has_zero_dte")]
        if zero_strikes:
            z_call = sum(s["call_oig"] for s in zero_strikes) * 100 * (spot * spot) * 0.01
            z_put = -sum(s["put_oig"] for s in zero_strikes) * 100 * (spot * spot) * 0.01
            zero_dte_net_gex = round(z_call + z_put, 2)
            total_abs = abs(call_gex_total) + abs(put_gex_total)
            if total_abs > 0:
                zero_dte_share = round(abs(zero_dte_net_gex) / total_abs, 4)

        return {"gex_by_strike": rows, "call_wall": call_wall, "put_wall": put_wall,
                "gamma_flip": flip, "dealer_regime": regime,
                "net_gex_total": net_gex_total, "call_gex_total": call_gex_total,
                "put_gex_total": put_gex_total, "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi, "total_oi": total_call_oi + total_put_oi,
                "atm_iv": atm_iv, "expected_move": em, "expected_move_pct": em_pct,
                # Tier A
                "max_gamma_strike": max_gamma_strike,
                "max_gamma_value": max_gamma_value,
                "vol_trigger": vol_trigger,
                "distance_to_flip": distance_to_flip,
                "dealer_flow": dealer_flow,
                "dealer_flow_intensity": dealer_flow_intensity,
                "pin_risk_strike": pin_risk_strike,
                "pin_risk_distance": pin_risk_distance,
                "pin_risk_dte_weighted": pin_risk_dte_weighted,
                # Tier B
                "total_vanna": total_vanna,
                "total_charm": total_charm,
                # Tier C
                "zero_dte_net_gex": zero_dte_net_gex,
                "zero_dte_share": zero_dte_share}
    return enrich


# ── public API ───────────────────────────────────────────────────────────────

def gex(ticker: str, *, max_dte: Optional[int] = None) -> GEXResult:
    """Cached GEX for a ticker. Never raises.

    ``max_dte`` (MITS Phase 9.3) restricts the chain to expirations
    within N days of today before aggregation. Default ``None`` means
    use the legacy 45-day front-month window. The cache is keyed on
    ``(ticker, max_dte)`` so the operator can switch between buckets
    in the UI without invalidating other buckets.
    """
    ticker = ticker.upper()
    cache_key = (ticker, int(max_dte) if max_dte is not None else None)
    now = time.monotonic()
    hit = _CACHE.get(cache_key)
    if hit and (now - hit[0]) < TUNABLES.gex_cache_ttl:
        return hit[1]
    prev = hit[1] if hit else _CACHE.get((ticker, None), (0, None))[1]

    spot = _spot(ticker)
    fa = _flashalpha(ticker, spot)
    if fa is not None and max_dte is None:
        # FlashAlpha returns its own pre-aggregated GEX; we only use it
        # for the default bucket since we can't honour ``max_dte``.
        _apply_deltas(fa, prev)
        fa.stale = _is_stale(fa.timestamp)
        _CACHE[cache_key] = (now, fa)
        return fa

    chain, source = None, "none"
    try:
        c = _cboe_chain(ticker)
        if c:
            chain, source = c, "cboe"
    except Exception:
        pass
    if chain is None:
        try:
            y = _yf_chain(ticker, spot)
            if y:
                chain, source = y, "yfinance"
        except Exception:
            pass

    clean_dte = int(max_dte) if max_dte is not None else 45
    def _clean_dte(rows: List[dict]) -> List[dict]:
        return _clean(rows, max_dte=clean_dte)
    res = run_pipeline(
        source=f"gex:{source}", fetch=lambda: chain,
        clean=_clean_dte, normalize=_normalize, validate=_validate, enrich=_make_enrich(spot),
    )
    ts = datetime.now(timezone.utc).isoformat()
    if res.ok and spot > 0:
        e = res.data
        out = GEXResult(ticker, ts, round(spot, 2), e["call_wall"], e["put_wall"], e["gamma_flip"],
                        e["dealer_regime"], e["gex_by_strike"], source=source, ok=True, note="computed from chain")
        out.net_gex_total = e["net_gex_total"]
        out.call_gex_total = e["call_gex_total"]
        out.put_gex_total = e["put_gex_total"]
        out.total_call_oi = e["total_call_oi"]
        out.total_put_oi = e["total_put_oi"]
        out.total_oi = e["total_oi"]
        out.atm_iv = e["atm_iv"]
        out.expected_move = e["expected_move"]
        out.expected_move_pct = e["expected_move_pct"]
        # Item #6 Tier A
        out.max_gamma_strike = e.get("max_gamma_strike")
        out.max_gamma_value = e.get("max_gamma_value", 0.0)
        out.vol_trigger = e.get("vol_trigger")
        out.distance_to_flip = e.get("distance_to_flip")
        out.dealer_flow = e.get("dealer_flow", "neutral")
        out.dealer_flow_intensity = e.get("dealer_flow_intensity", 0.0)
        out.pin_risk_strike = e.get("pin_risk_strike")
        out.pin_risk_distance = e.get("pin_risk_distance")
        out.pin_risk_dte_weighted = e.get("pin_risk_dte_weighted")
        # Item #6 Tier B
        out.total_vanna = e.get("total_vanna")
        out.total_charm = e.get("total_charm")
        # Item #6 Tier C
        out.zero_dte_net_gex = e.get("zero_dte_net_gex")
        out.zero_dte_share = e.get("zero_dte_share")
    else:
        note = f"{res.stage}: {'; '.join(res.issues) or 'unavailable'}"
        out = GEXResult(ticker, ts, round(spot, 2), source=source, ok=False, note=note)
    _apply_deltas(out, prev)
    out.stale = _is_stale(out.timestamp)
    _CACHE[cache_key] = (now, out)
    # Mirror the default bucket into the legacy single-ticker key so
    # existing callers (engine, market snapshot) keep working.
    if max_dte is None:
        _CACHE[ticker] = (now, out)
    return out


def gex_by_expiry(ticker: str, *, max_expiries: int = 6) -> dict:
    """Per-expiry GEX breakdown — Item #14 third panel.

    Returns ``{ticker, spot, expiries: [{expiry, dte, strikes: [{strike,
    call_gex, put_gex, net_gex}], totals: {call_gex, put_gex, net_gex}}]}``.

    Fetches up to ``max_expiries`` near-term expirations (yfinance) so the
    UI can render stacked bars colored by expiry bucket. Falls back to a
    single-expiry response if the chain source is Cboe-only.
    """
    ticker = ticker.upper()
    spot = _spot(ticker)
    if spot <= 0:
        return {"ticker": ticker, "spot": 0.0, "expiries": []}
    expiries_out: List[dict] = []
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        exps_raw = list(t.options or [])
    except Exception:
        exps_raw = []
    cutoff = date.today() + timedelta(days=45)
    chosen: List[str] = []
    for e in exps_raw:
        try:
            if datetime.strptime(e, "%Y-%m-%d").date() <= cutoff:
                chosen.append(e)
        except Exception:
            continue
        if len(chosen) >= max_expiries:
            break

    r_rate = float(getattr(TUNABLES, "risk_free_rate", 0.045))
    for expiry in chosen:
        try:
            dte = max(1, (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days)
            t_years = dte / 365.0
            chain = yf.Ticker(ticker).option_chain(expiry)
        except Exception:
            continue
        if chain is None or chain.calls is None or chain.puts is None:
            continue
        per_strike: Dict[float, dict] = {}
        for df_, cp in ((chain.calls, "C"), (chain.puts, "P")):
            if df_ is None or getattr(df_, "empty", True):
                continue
            for _, row in df_.iterrows():
                try:
                    iv = float(row.get("impliedVolatility") or 0)
                    K = float(row.get("strike") or 0)
                    oi = float(row.get("openInterest") or 0)
                except Exception:
                    continue
                if K <= 0 or iv <= 0:
                    continue
                gamma = _bs_gamma(spot, K, t_years, iv, r_rate)
                d = per_strike.setdefault(K, {"strike": K, "call_oig": 0.0,
                                                     "put_oig": 0.0, "call_oi": 0.0,
                                                     "put_oi": 0.0})
                if cp == "C":
                    d["call_oig"] += oi * gamma
                    d["call_oi"] += oi
                else:
                    d["put_oig"] += oi * gamma
                    d["put_oi"] += oi
        # Convert to GEX dollar exposure per strike (same formula as enrich).
        s2 = spot * spot
        strikes_out = []
        for s_dict in sorted(per_strike.values(), key=lambda d: d["strike"]):
            cg = s_dict["call_oig"] * 100 * s2 * 0.01
            pg = -(s_dict["put_oig"] * 100 * s2 * 0.01)
            strikes_out.append({
                "strike": round(s_dict["strike"], 2),
                "call_gex": round(cg, 2),
                "put_gex": round(pg, 2),
                "net_gex": round(cg + pg, 2),
            })
        totals = {
            "call_gex": round(sum(s["call_gex"] for s in strikes_out), 2),
            "put_gex": round(sum(s["put_gex"] for s in strikes_out), 2),
            "net_gex": round(sum(s["net_gex"] for s in strikes_out), 2),
        }
        expiries_out.append({
            "expiry": expiry, "dte": dte,
            "strikes": strikes_out, "totals": totals,
        })
    return {"ticker": ticker, "spot": round(spot, 2), "expiries": expiries_out}


def gex_context(ticker: str) -> dict:
    """Compact GEX fields for injection into a strategy's data snapshot.

    Includes Item #6 Tier A regime metrics so strategies can gate on
    "dealer flow stabilizing here, but vol_trigger only 0.8% below — be
    careful around that level."
    """
    g = gex(ticker)
    if not g.ok:
        return {}
    return {
        "dealer_regime": g.dealer_regime, "gamma_flip": g.gamma_flip,
        "call_wall": g.call_wall, "put_wall": g.put_wall,
        "opex_day": is_opex_day(), "opex_week": is_opex_week(),
        # Tier A — institutional gamma intuition
        "max_gamma_strike": g.max_gamma_strike,
        "vol_trigger": g.vol_trigger,
        "distance_to_flip": g.distance_to_flip,
        "dealer_flow": g.dealer_flow,
        "dealer_flow_intensity": g.dealer_flow_intensity,
        "pin_risk_strike": g.pin_risk_strike,
        "pin_risk_distance": g.pin_risk_distance,
        "pin_risk_dte_weighted": g.pin_risk_dte_weighted,
        # Tier B
        "total_vanna": g.total_vanna,
        "total_charm": g.total_charm,
        # Tier C
        "zero_dte_share": g.zero_dte_share,
        "zero_dte_net_gex": g.zero_dte_net_gex,
        # Regime ribbon source
        "net_gex_total": g.net_gex_total,
    }


# ── regime history (#8) ────────────────────────────────────────────────────────

def store_regime_snapshot(ticker: str) -> Optional[dict]:
    """Persist a GEX regime snapshot to ``gex_regime_history``. Never raises."""
    try:
        g = gex(ticker)
        if not g.ok:
            return None
        from backend.db import session_scope
        from backend.models.gex_history import GexRegimeHistory

        with session_scope() as session:
            row = GexRegimeHistory(
                ticker=g.ticker, spot_price=g.spot_price, call_wall=g.call_wall,
                put_wall=g.put_wall, gamma_flip=g.gamma_flip, dealer_regime=g.dealer_regime,
            )
            session.add(row)
            session.flush()
            return row.to_dict()
    except Exception:
        logger.debug("store_regime_snapshot failed for %s", ticker, exc_info=True)
        return None


def regime_history(ticker: str, limit: int = 200) -> List[dict]:
    """Recent stored GEX regime snapshots for a ticker, oldest→newest."""
    try:
        from backend.db import session_scope
        from backend.models.gex_history import GexRegimeHistory

        with session_scope() as session:
            rows = (
                session.query(GexRegimeHistory)
                .filter(GexRegimeHistory.ticker == ticker.upper())
                .order_by(GexRegimeHistory.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [r.to_dict() for r in reversed(rows)]
    except Exception:
        return []
