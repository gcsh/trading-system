"""Stage-20a — Market Internals Score.

The shared, deterministic interpretation of "what is the market doing
right now?" that every macro / market / risk agent consumes. Before
Stage-20a, agents independently re-read the macro panel + breadth +
short pressure and each came up with its own framing — five agents
called the same FRED dictionary "risk_off" five different ways.

Now the score is computed ONCE per consensus run, attached to the
context as ``market_internals``, and all agents read from it. Same
inputs in, same conclusion out — disagreement now comes from differing
mandates, not differing interpretations.

The score is intentionally simple and rule-based: each of the 9 source
categories is mapped to a signed score in [-1, +1] (+1 = bullish/risk-on,
-1 = bearish/risk-off, None = no data). A composite + verdict label
roll the categories up so agents can branch on a single field
("internals.verdict == 'risk_off'") rather than re-deriving.

This module is pure: takes a dict, returns a ``MarketInternalsScore``.
No I/O, no DB, no Anthropic. Cheap to call.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.agents.contract import SOURCE_CATEGORIES


# The MarketInternalsScore covers MARKET-level categories only;
# ``portfolio_state`` is OUR-book evidence that doesn't belong in a
# shared market view. The score's category set is therefore
# SOURCE_CATEGORIES minus portfolio_state.
MARKET_INTERNAL_CATEGORIES = tuple(
    c for c in SOURCE_CATEGORIES if c != "portfolio_state"
)


# Verdict labels.
VERDICT_RISK_ON = "risk_on"
VERDICT_RISK_OFF = "risk_off"
VERDICT_MIXED = "mixed"
VERDICT_UNKNOWN = "unknown"


@dataclass
class MarketInternalsScore:
    """Shared market view. Every category is signed [-1, +1] or None."""

    macro_liquidity: Optional[float] = None
    credit: Optional[float] = None
    breadth: Optional[float] = None
    positioning: Optional[float] = None
    volatility: Optional[float] = None
    fundamentals: Optional[float] = None
    insider_flow: Optional[float] = None
    price_structure: Optional[float] = None
    microstructure_flow: Optional[float] = None

    composite: float = 0.0                 # weighted mean of non-null categories
    verdict: str = VERDICT_UNKNOWN
    sources_available: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def category_score(self, name: str) -> Optional[float]:
        """Look up a category by name from ``SOURCE_CATEGORIES``."""
        return getattr(self, name, None)


# ── helpers ─────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _macro_value(macro: Dict[str, Any], key: str) -> Optional[float]:
    d = macro.get(key) if isinstance(macro, dict) else None
    if isinstance(d, dict) and d.get("value") is not None:
        try:
            return float(d["value"])
        except (TypeError, ValueError):
            return None
    return None


def _macro_change(macro: Dict[str, Any], key: str) -> Optional[float]:
    d = macro.get(key) if isinstance(macro, dict) else None
    if isinstance(d, dict) and d.get("change_30d_pct") is not None:
        try:
            return float(d["change_30d_pct"])
        except (TypeError, ValueError):
            return None
    return None


# ── per-category scorers ────────────────────────────────────────────────


def _score_macro_liquidity(macro: Dict[str, Any]) -> tuple[Optional[float], List[str]]:
    """NFCI + financial conditions. NFCI < 0 = loose (risk-on),
    NFCI > 0 = tight (risk-off). Mapped linearly into [-1, +1]."""
    notes: List[str] = []
    nfci = _macro_value(macro, "NFCI")
    if nfci is None:
        return None, notes
    # NFCI typical range ~[-1.5, +2.5]. Scale so |0.6| ~= |0.5| score.
    score = _clamp(-nfci / 1.2)
    if nfci < -0.3:
        notes.append(f"NFCI {nfci:+.2f} (loose conditions)")
    elif nfci > 0.3:
        notes.append(f"NFCI {nfci:+.2f} (tight conditions)")
    else:
        notes.append(f"NFCI {nfci:+.2f} (neutral)")
    return score, notes


def _score_credit(macro: Dict[str, Any]) -> tuple[Optional[float], List[str]]:
    """High-yield OAS. Low spread = risk-on, widening = risk-off."""
    notes: List[str] = []
    hy = _macro_value(macro, "BAMLH0A0HYM2")
    hy_chg = _macro_change(macro, "BAMLH0A0HYM2")
    if hy is None:
        return None, notes
    # 3% = tight (risk-on), 5.5% = stress (risk-off), >7% = panic.
    if hy <= 3.0:
        base = 0.8
    elif hy <= 4.0:
        base = 0.4
    elif hy <= 5.5:
        base = 0.0
    elif hy <= 7.0:
        base = -0.5
    else:
        base = -0.9
    if hy_chg is not None:
        # Widening 15% in 30d shifts score by -0.3.
        base -= _clamp(hy_chg * 2.0, -0.4, 0.4)
    notes.append(f"HY OAS {hy:.1f}%" +
                  (f" ({hy_chg:+.0%} 30d)" if hy_chg is not None else ""))
    return _clamp(base), notes


def _score_breadth(breadth: Dict[str, Any]) -> tuple[Optional[float], List[str]]:
    """Breadth verdict from bot.breadth.regime_health."""
    notes: List[str] = []
    if not isinstance(breadth, dict) or not breadth.get("verdict"):
        return None, notes
    verdict = (breadth.get("verdict") or "").lower()
    pct50 = breadth.get("pct_above_50dma")
    mapping = {
        "healthy_advance": 0.8,
        "pullback_in_bull": 0.3,
        "mixed": 0.0,
        "narrow_rally_fragile": -0.2,
        "broken": -0.7,
    }
    score = mapping.get(verdict)
    if score is None:
        return None, notes
    note = f"breadth {verdict}"
    if pct50 is not None:
        note += f" ({float(pct50):.0%} > 50dma)"
    notes.append(note)
    return score, notes


def _score_positioning(
    cot: Dict[str, Any], features: Dict[str, Any]
) -> tuple[Optional[float], List[str]]:
    """COT noncommercial net position on ES + dealer regime hint."""
    notes: List[str] = []
    raw: Optional[float] = None
    es = (cot or {}).get("ES") or {}
    nc = es.get("noncommercial_net")
    oi = es.get("open_interest") or 0
    if nc is not None and oi:
        try:
            ratio = float(nc) / float(oi)
            # ratio typically in [-0.3, +0.3] — scale.
            raw = _clamp(ratio * 3.0)
            notes.append(f"ES noncomm net {ratio:+.1%} of OI")
        except (TypeError, ValueError, ZeroDivisionError):
            raw = None

    dealer = (features.get("dealer_regime") or "").lower() if features else ""
    if dealer == "long_gamma":
        raw = (raw + 0.3) / 2 if raw is not None else 0.3
        notes.append("dealers long gamma")
    elif dealer == "short_gamma":
        raw = (raw - 0.3) / 2 if raw is not None else -0.3
        notes.append("dealers short gamma")
    if raw is None:
        return None, notes
    return _clamp(raw), notes


def _score_volatility(
    snapshot: Dict[str, Any], features: Dict[str, Any]
) -> tuple[Optional[float], List[str]]:
    """VIX level — low = risk-on, high = risk-off."""
    notes: List[str] = []
    vix = features.get("vix") if features else None
    if vix is None and snapshot:
        vix = snapshot.get("vix")
    if vix is None:
        return None, notes
    try:
        v = float(vix)
    except (TypeError, ValueError):
        return None, notes
    if v <= 0:
        return None, notes
    # VIX 14 → +0.6, 20 → 0, 28 → -0.6
    score = _clamp((20.0 - v) / 10.0)
    notes.append(f"VIX {v:.1f}")
    return score, notes


def _score_fundamentals(ei: Dict[str, Any]) -> tuple[Optional[float], List[str]]:
    """Earnings call intelligence: guidance + margin + tone."""
    notes: List[str] = []
    if not isinstance(ei, dict) or not ei:
        return None, notes
    parts = 0
    score = 0.0
    gc = (ei.get("guidance_change") or "").lower()
    if gc:
        parts += 1
        if gc == "improved":
            score += 0.8
        elif gc == "reduced":
            score -= 0.8
        elif gc == "withdrawn":
            score -= 0.9
    mt = (ei.get("margin_trajectory") or "").lower()
    if mt:
        parts += 1
        if mt == "expanding":
            score += 0.5
        elif mt == "contracting":
            score -= 0.5
    tone = (ei.get("management_tone") or "").lower()
    if tone:
        parts += 1
        if tone == "confident":
            score += 0.4
        elif tone == "cautious":
            score -= 0.4
    if parts == 0:
        return None, notes
    score = score / parts
    notes.append(
        f"earnings: guidance {gc or 'n/a'}, margins {mt or 'n/a'}, tone {tone or 'n/a'}"
    )
    return _clamp(score), notes


def _score_insider_flow(
    insider: Dict[str, Any]
) -> tuple[Optional[float], List[str]]:
    """Form-4 burst feature. We don't know buy/sell without XML parsing,
    so the signal is "activity heat" — high count is informational
    rather than directional. Surface as a small magnitude score so it
    doesn't dominate the composite."""
    notes: List[str] = []
    if not isinstance(insider, dict) or not insider:
        return None, notes
    n = insider.get("form4_count")
    if n is None:
        return None, notes
    try:
        cnt = int(n)
    except (TypeError, ValueError):
        return None, notes
    if cnt <= 0:
        return None, notes
    # Heat-only: shows up in the panel but with low magnitude.
    score = -_clamp(cnt / 10.0, 0.0, 0.4)        # heavy insider activity = caution
    notes.append(f"insider activity: {cnt} Form 4 / 30d")
    return score, notes


def _score_price_structure(
    snapshot: Dict[str, Any], features: Dict[str, Any]
) -> tuple[Optional[float], List[str]]:
    """Trend + bias from regime/features."""
    notes: List[str] = []
    trend = ""
    trend_bias = features.get("trend_bias") if features else None
    if snapshot and snapshot.get("spy_trend"):
        trend = (snapshot.get("spy_trend") or "").lower()
    if not trend and trend_bias is None:
        return None, notes
    score = 0.0
    parts = 0
    if trend in ("bullish", "bearish", "choppy"):
        parts += 1
        if trend == "bullish":
            score += 0.6
        elif trend == "bearish":
            score -= 0.6
    if trend_bias is not None:
        try:
            tb = float(trend_bias)
            parts += 1
            score += _clamp(tb)
        except (TypeError, ValueError):
            pass
    if parts == 0:
        return None, notes
    notes.append(
        f"price: {trend or 'n/a'}" +
        (f" bias={float(trend_bias):+.2f}" if trend_bias is not None else "")
    )
    return _clamp(score / parts), notes


def _score_microstructure(
    features: Dict[str, Any], short_pressure: Dict[str, Any]
) -> tuple[Optional[float], List[str]]:
    """Flow + volume + short pressure."""
    notes: List[str] = []
    if not features and not short_pressure:
        return None, notes
    score = 0.0
    parts = 0
    flow_b = (features or {}).get("flow_bullishness")
    if flow_b is not None:
        try:
            fb = float(flow_b)
            parts += 1
            score += _clamp(fb)
            notes.append(f"flow {fb:+.2f}")
        except (TypeError, ValueError):
            pass
    vr = (features or {}).get("volume_ratio")
    if vr is not None:
        try:
            v = float(vr)
            parts += 1
            # 1.0 = average; >1.5 modestly bullish, <0.5 thin
            if v >= 1.5:
                score += 0.2
            elif v <= 0.5:
                score -= 0.3
            notes.append(f"vol {v:.1f}× avg")
        except (TypeError, ValueError):
            pass
    sp = short_pressure or {}
    sp_level = (sp.get("level") or "").lower()
    sp_trend = (sp.get("trend") or "").lower()
    if sp_level or sp_trend:
        parts += 1
        if sp_level == "high" and sp_trend == "rising":
            score += 0.3                              # squeeze fuel
            notes.append("short pressure high/rising → squeeze fuel")
        elif sp_level == "high":
            score -= 0.1
            notes.append(f"short interest {sp_level}")
    if parts == 0:
        return None, notes
    return _clamp(score / parts), notes


# ── composite ───────────────────────────────────────────────────────────


# Category weights into the composite. Macro/breadth/credit dominate
# (they describe the regime); fundamentals/insider/microstructure are
# ticker-local hints and get smaller weight in the composite.
_COMPOSITE_WEIGHTS: Dict[str, float] = {
    "macro_liquidity": 1.0,
    "credit": 1.0,
    "breadth": 1.0,
    "positioning": 0.7,
    "volatility": 0.9,
    "fundamentals": 0.6,
    "insider_flow": 0.3,
    "price_structure": 0.8,
    "microstructure_flow": 0.6,
}


def _verdict_from(composite: float, n_sources: int) -> str:
    if n_sources < 2:
        return VERDICT_UNKNOWN
    if composite >= 0.30:
        return VERDICT_RISK_ON
    if composite <= -0.30:
        return VERDICT_RISK_OFF
    return VERDICT_MIXED


def compute_market_internals(
    *,
    macro: Optional[Dict[str, Any]] = None,
    breadth: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    features: Optional[Dict[str, Any]] = None,
    cot: Optional[Dict[str, Any]] = None,
    earnings_intel: Optional[Dict[str, Any]] = None,
    insider: Optional[Dict[str, Any]] = None,
    short_pressure: Optional[Dict[str, Any]] = None,
) -> MarketInternalsScore:
    """Compute the shared market view. Pure deterministic.

    Pass whichever inputs are available; missing inputs leave the
    corresponding category as ``None``. The composite + verdict are
    derived from the non-null categories only — passing in zero
    sources yields ``unknown``.
    """
    macro = macro or {}
    breadth = breadth or {}
    snapshot = snapshot or {}
    features = features or {}
    cot = cot or {}
    earnings_intel = earnings_intel or {}
    insider = insider or {}
    short_pressure = short_pressure or {}

    scorers: Dict[str, tuple[Optional[float], List[str]]] = {
        "macro_liquidity": _score_macro_liquidity(macro),
        "credit": _score_credit(macro),
        "breadth": _score_breadth(breadth),
        "positioning": _score_positioning(cot, features),
        "volatility": _score_volatility(snapshot, features),
        "fundamentals": _score_fundamentals(earnings_intel),
        "insider_flow": _score_insider_flow(insider),
        "price_structure": _score_price_structure(snapshot, features),
        "microstructure_flow": _score_microstructure(features, short_pressure),
    }

    # Cross-check we covered every market-level category.
    assert set(scorers.keys()) == set(MARKET_INTERNAL_CATEGORIES), (
        "MarketInternalsScore is out of sync with MARKET_INTERNAL_CATEGORIES; "
        f"missing={set(MARKET_INTERNAL_CATEGORIES) - set(scorers.keys())} "
        f"extra={set(scorers.keys()) - set(MARKET_INTERNAL_CATEGORIES)}"
    )

    notes: List[str] = []
    kwargs: Dict[str, Optional[float]] = {}
    weighted_total = 0.0
    weight_sum = 0.0
    n_sources = 0
    for cat, (score, cat_notes) in scorers.items():
        kwargs[cat] = score
        if score is None:
            continue
        n_sources += 1
        w = _COMPOSITE_WEIGHTS.get(cat, 1.0)
        weighted_total += score * w
        weight_sum += w
        notes.extend(cat_notes)
    composite = (weighted_total / weight_sum) if weight_sum else 0.0
    composite = round(_clamp(composite), 3)

    return MarketInternalsScore(
        **kwargs,
        composite=composite,
        verdict=_verdict_from(composite, n_sources),
        sources_available=n_sources,
        notes=notes,
    )
