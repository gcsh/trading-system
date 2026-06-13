"""Stage-11.6 Scenario Engine — portfolio-level macro stress.

Take the current open positions, apply a hypothetical macro shock vector
(SPY ±%, VIX ±Δ, rates ±bps, optional per-sector overrides), and project
per-position and portfolio-level P&L impact.

Sensitivities are heuristic and transparent — the output is "what would
happen under a simple linear approximation", not a multi-factor risk
model. The point is to make the trade-off visible during decisioning,
not to replace a Greeks engine.

Heuristics per instrument:

  • **Stock long**:  ΔP&L ≈ market_value × beta × spy_pct
                     + market_value × sector_override (if any)
                     + market_value × rate_sensitivity × rates_bps / 10000
                     (negative rate_sensitivity for tech-heavy sectors)

  • **Long call** :  ΔP&L ≈ market_value × call_delta × underlying_pct
                     + market_value × vega_factor × vix_delta
                     where call_delta ≈ 0.55 (ATM-ish heuristic)
                           vega_factor ≈ 0.02 (~2% of premium per VIX pt)

  • **Long put**  :  ΔP&L ≈ market_value × put_delta × underlying_pct
                     + market_value × vega_factor × vix_delta
                     where put_delta ≈ -0.45

  • **Complex / spreads**: net market_value × 0 (we don't have the legs'
    Greeks; mark net delta from `meta` when present, else zero).

All five **PRESETS** are named in the dict at module-bottom so the
endpoint / UI can offer them as one-click stress buttons.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.portfolio_intel import beta_of, sector_of

logger = logging.getLogger(__name__)


# ── data types ───────────────────────────────────────────────────────────


@dataclass
class Shock:
    spy_pct: float = 0.0                                          # decimal (e.g. -0.02)
    vix_delta: float = 0.0                                        # points
    rates_bps: float = 0.0                                        # basis points
    sector_pcts: Dict[str, float] = field(default_factory=dict)   # extra per-sector %
    label: str = ""                                               # human label

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PositionImpact:
    ticker: str
    instrument: str               # stock | option | complex
    side: str                     # LONG | SHORT
    market_value: float
    base_unrealized_pnl: float
    pnl_delta: float
    new_unrealized_pnl: float
    pnl_delta_pct: float          # vs base value
    breakdown: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioResult:
    shock: Dict[str, Any]
    total_market_value: float
    total_base_pnl: float
    total_pnl_delta: float
    new_total_pnl: float
    portfolio_pct_change: float                        # vs market_value
    impacts: List[PositionImpact] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["impacts"] = [i.to_dict() if hasattr(i, "to_dict") else i for i in self.impacts]
        return d


# ── sensitivity heuristics ───────────────────────────────────────────────


# Per-sector rate sensitivity. Negative means the sector loses when rates
# rise — long-duration tech/growth is hurt by rising rates; financials
# tend to benefit.
_RATE_SENSITIVITY = {
    "Tech": -0.0015,
    "Semis": -0.0020,
    "Growth": -0.0018,
    "Crypto-proxy": -0.0030,
    "Financials": +0.0012,
    "Energy": +0.0005,
    "Defensive": -0.0005,
    "Utilities": -0.0010,
    "Real Estate": -0.0025,
    "REITs": -0.0025,
    "Industrials": -0.0008,
    "Healthcare": -0.0005,
}


# Option-side Greeks defaults — used when no explicit per-position Greeks
# are supplied in the position's ``meta`` dict.
_CALL_DELTA = 0.55
_PUT_DELTA = -0.45
_VEGA_FACTOR = 0.02       # ~2% of premium per VIX pt change


def _rate_sensitivity_for(ticker: str) -> float:
    return _RATE_SENSITIVITY.get(sector_of(ticker), 0.0)


def _stock_impact(pos: Dict[str, Any], shock: Shock) -> PositionImpact:
    ticker = pos.get("ticker", "")
    mv = float(pos.get("market_value") or 0.0)
    base = float(pos.get("unrealized_pnl") or 0.0)
    beta = beta_of(ticker)
    sector = sector_of(ticker)

    spy_contrib = mv * beta * shock.spy_pct
    sector_contrib = mv * float(shock.sector_pcts.get(sector, 0.0))
    rate_contrib = mv * _rate_sensitivity_for(ticker) * shock.rates_bps
    # Stocks have ~zero vega; small indirect VIX drag captured via beta.
    delta = spy_contrib + sector_contrib + rate_contrib

    return PositionImpact(
        ticker=ticker, instrument="stock",
        side="LONG" if float(pos.get("quantity") or 0) > 0 else "SHORT",
        market_value=round(mv, 2),
        base_unrealized_pnl=round(base, 2),
        pnl_delta=round(delta, 2),
        new_unrealized_pnl=round(base + delta, 2),
        pnl_delta_pct=round((delta / mv) if mv else 0.0, 4),
        breakdown={
            "spy": round(spy_contrib, 2),
            "sector": round(sector_contrib, 2),
            "rates": round(rate_contrib, 2),
        },
    )


def _option_impact(pos: Dict[str, Any], shock: Shock) -> PositionImpact:
    ticker = pos.get("ticker", "")
    mv = abs(float(pos.get("market_value") or 0.0))
    base = float(pos.get("unrealized_pnl") or 0.0)
    side = (pos.get("side") or "LONG").upper()
    opt_type = (pos.get("option_type") or "call").lower()
    beta = beta_of(ticker)
    underlying_pct = beta * shock.spy_pct
    sign = 1.0 if side == "LONG" else -1.0
    meta = pos.get("meta") or {}

    # Stage-15 — pull real Black-Scholes Greeks when the position has
    # enough info (strike + expiration + underlying + IV). Falls through
    # to the heuristic defaults when any input is missing or degenerate.
    delta_default = _CALL_DELTA if opt_type == "call" else _PUT_DELTA
    delta_used = delta_default
    vega_per_vol_pt_used = _VEGA_FACTOR     # heuristic: fraction of premium per VIX pt
    greeks_source = "heuristic"
    try:
        from backend.bot.greeks import greeks_from_position
        g = greeks_from_position(pos)
        if g.delta != 0 or g.vega != 0:
            # Real Greeks: ``vega`` is $ per 1 vol point per *contract*.
            # Per-contract math: contracts × 100 (multiplier) × per-share Δ × ΔS,
            # and contracts × vega × ΔIV. We translate to "$ change as fraction
            # of market_value" so the existing shock arithmetic still works:
            #   delta_pnl = MV × Δ × ΔS / S
            #   vega_pnl  = (vega / per_contract_price) × MV × ΔIV
            # When we don't have the per-contract price, vega_per_vol_pt
            # defaults to the heuristic 0.02 — a graceful blend.
            delta_used = float(g.delta)
            # Compute "vega as % of premium per vol point" when possible.
            contracts = int(abs(pos.get("contracts")
                                  or pos.get("quantity") or 0)) or 1
            per_contract_premium = mv / max(1, contracts) / 100.0
            if per_contract_premium > 0 and g.vega:
                vega_per_vol_pt_used = float(g.vega) / per_contract_premium
                # Clamp to sane band so a bad IV input can't blow up the model.
                vega_per_vol_pt_used = max(0.0, min(0.50, vega_per_vol_pt_used))
            greeks_source = "computed"
    except Exception:
        # Greeks module unavailable / scipy missing — silently use heuristic.
        pass

    # Per-position meta overrides win over everything (operator forced value).
    if isinstance(meta, dict):
        if "delta" in meta:
            try:
                delta_used = float(meta["delta"])
                greeks_source = "meta_override"
            except Exception:
                pass
        if "vega_per_vol_point" in meta:
            try:
                vega_per_vol_pt_used = float(meta["vega_per_vol_point"])
                greeks_source = "meta_override"
            except Exception:
                pass

    delta_contrib = mv * delta_used * underlying_pct * sign
    vega_contrib = mv * vega_per_vol_pt_used * shock.vix_delta * sign
    rate_contrib = 0.0
    total = delta_contrib + vega_contrib + rate_contrib

    return PositionImpact(
        ticker=ticker, instrument="option",
        side=side,
        market_value=round(mv, 2),
        base_unrealized_pnl=round(base, 2),
        pnl_delta=round(total, 2),
        new_unrealized_pnl=round(base + total, 2),
        pnl_delta_pct=round((total / mv) if mv else 0.0, 4),
        breakdown={
            "delta": round(delta_contrib, 2),
            "vega": round(vega_contrib, 2),
            "rates": round(rate_contrib, 2),
            "delta_used": round(delta_used, 4),
            "vega_per_vol_point_used": round(vega_per_vol_pt_used, 4),
            "greeks_source": greeks_source,
        },
    )


def _complex_impact(pos: Dict[str, Any], shock: Shock) -> PositionImpact:
    ticker = pos.get("ticker", "")
    mv = abs(float(pos.get("market_value") or 0.0))
    base = float(pos.get("unrealized_pnl") or 0.0)
    # Net delta from the meta blob if provided, else assume zero (defined-risk
    # spread). VIX exposure mirrors the option vega heuristic, dampened.
    meta = pos.get("meta") or {}
    net_delta = 0.0
    if isinstance(meta, dict):
        try:
            net_delta = float(meta.get("net_delta", 0.0))
        except Exception:
            net_delta = 0.0
    underlying_pct = beta_of(ticker) * shock.spy_pct
    delta_contrib = mv * net_delta * underlying_pct
    vega_contrib = mv * (_VEGA_FACTOR * 0.5) * shock.vix_delta
    total = delta_contrib + vega_contrib
    return PositionImpact(
        ticker=ticker, instrument="complex",
        side=(pos.get("side") or "LONG").upper(),
        market_value=round(mv, 2),
        base_unrealized_pnl=round(base, 2),
        pnl_delta=round(total, 2),
        new_unrealized_pnl=round(base + total, 2),
        pnl_delta_pct=round((total / mv) if mv else 0.0, 4),
        breakdown={
            "delta": round(delta_contrib, 2),
            "vega": round(vega_contrib, 2),
        },
    )


def _impact_for(pos: Dict[str, Any], shock: Shock) -> PositionImpact:
    kind = (pos.get("kind") or pos.get("instrument") or "stock").lower()
    if kind == "stock":
        return _stock_impact(pos, shock)
    if kind == "option":
        return _option_impact(pos, shock)
    return _complex_impact(pos, shock)


# ── public API ───────────────────────────────────────────────────────────


def run_scenario(positions: List[Dict[str, Any]], shock: Shock) -> ScenarioResult:
    """Project ``shock`` onto each open position and aggregate."""
    impacts = [_impact_for(p, shock) for p in (positions or [])]
    total_mv = round(sum(abs(i.market_value) for i in impacts), 2)
    total_base = round(sum(i.base_unrealized_pnl for i in impacts), 2)
    total_delta = round(sum(i.pnl_delta for i in impacts), 2)
    portfolio_pct = round((total_delta / total_mv) if total_mv else 0.0, 4)

    worst = min(impacts, key=lambda i: i.pnl_delta, default=None)
    best = max(impacts, key=lambda i: i.pnl_delta, default=None)
    by_instr: Dict[str, float] = {}
    for i in impacts:
        by_instr[i.instrument] = round(
            by_instr.get(i.instrument, 0.0) + i.pnl_delta, 2)

    summary = {
        "positions": len(impacts),
        "by_instrument": by_instr,
        "worst": worst.to_dict() if worst else None,
        "best": best.to_dict() if best else None,
    }
    return ScenarioResult(
        shock=shock.to_dict(),
        total_market_value=total_mv,
        total_base_pnl=total_base,
        total_pnl_delta=total_delta,
        new_total_pnl=round(total_base + total_delta, 2),
        portfolio_pct_change=portfolio_pct,
        impacts=impacts, summary=summary,
    )


def fetch_live_positions() -> List[Dict[str, Any]]:
    """Best-effort: pull positions from the running engine's executor.
    Returns an empty list if the engine / executor isn't reachable."""
    try:
        from backend.main import app  # late import avoids cycles at module load
        engine = getattr(app.state, "engine", None)
        if engine is None or not hasattr(engine.executor, "positions"):
            return []
        return engine.executor.positions() or []
    except Exception:
        logger.debug("fetch_live_positions failed", exc_info=True)
        return []


# Five canonical stress scenarios. Keep the dict tight — UI uses the keys
# as button names, so add new entries judiciously.
PRESETS: Dict[str, Shock] = {
    "mild_risk_off": Shock(spy_pct=-0.01, vix_delta=3, label="Mild risk-off (SPY −1%, VIX +3)"),
    "severe_risk_off": Shock(spy_pct=-0.05, vix_delta=15, label="Severe risk-off (SPY −5%, VIX +15)"),
    "risk_on": Shock(spy_pct=0.02, vix_delta=-2, label="Risk-on (SPY +2%, VIX −2)"),
    "rates_shock": Shock(rates_bps=50, label="Rates shock (+50 bps)"),
    "vix_spike": Shock(vix_delta=20, label="VIX spike (+20)"),
    "flash_crash": Shock(spy_pct=-0.08, vix_delta=25,
                            label="Flash crash (SPY −8%, VIX +25)"),
}


def preset_list() -> List[Dict[str, Any]]:
    """Surface PRESETS for the endpoint. Order matters for the UI."""
    return [{"name": name, **shock.to_dict()} for name, shock in PRESETS.items()]
