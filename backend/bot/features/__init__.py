"""Feature Fusion Engine.

Collapses a raw market-data snapshot into one normalized feature vector that the
probability engine, ranker, regime layer and (later) ML models all share. Keeping
the fusion in one place means every downstream consumer sees the same, consistently
scaled inputs.

Pure + NaN-safe. Most features are scaled to roughly [-1, 1] (directional bias) or
[0, 1] (magnitude) so heuristics and models don't have to re-normalize.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _num(snapshot: Dict[str, Any], key: str, default: Optional[float] = None) -> Optional[float]:
    v = snapshot.get(key)
    try:
        f = float(v)
        return default if f != f else f   # NaN-safe
    except (TypeError, ValueError):
        return default


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def build_features(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return the unified, normalized feature vector for one ticker. Never raises."""
    s = snapshot or {}
    price = _num(s, "price", 0.0) or 0.0

    feats: Dict[str, Any] = {}

    # --- momentum / trend ---
    rsi = _num(s, "rsi")
    feats["rsi_14"] = rsi
    feats["rsi_bias"] = _clip((rsi - 50.0) / 30.0) if rsi is not None else 0.0   # >50 bullish

    macd = _num(s, "macd")
    macd_sig = _num(s, "macd_signal")
    if macd is not None and macd_sig is not None:
        feats["macd_hist"] = round(macd - macd_sig, 4)
        feats["macd_bias"] = _clip((macd - macd_sig) / (abs(macd_sig) + 1e-6))
    else:
        feats["macd_hist"] = None
        feats["macd_bias"] = 0.0

    ma50 = _num(s, "ma50", 0.0) or 0.0
    ma200 = _num(s, "ma200", 0.0) or 0.0
    if price > 0 and ma50 > 0 and ma200 > 0:
        feats["trend_bias"] = _clip(((price - ma50) / ma50 + (ma50 - ma200) / ma200) * 5)
    else:
        feats["trend_bias"] = 0.0
    feats["adx"] = _num(s, "adx")

    # --- volume / liquidity ---
    vol = _num(s, "volume", 0.0) or 0.0
    avg = _num(s, "avg_volume", 0.0) or 0.0
    feats["volume_ratio"] = round(vol / avg, 3) if avg > 0 else None
    feats["volume_zscore"] = round(_clip((vol / avg - 1.0), -3, 3), 3) if avg > 0 else 0.0

    # --- volatility ---
    feats["vix"] = _num(s, "vix")
    feats["iv_rank"] = _num(s, "iv_rank")
    feats["atr"] = _num(s, "atr")

    # --- options / dealer structure (GEX) ---
    flip = _num(s, "gamma_flip")
    if price > 0 and flip:
        feats["gex_flip_distance"] = round((price - flip) / price * 100, 2)  # % above(+)/below(-) flip
    else:
        feats["gex_flip_distance"] = None
    feats["dealer_regime"] = s.get("dealer_regime") or "unknown"
    feats["put_call_ratio"] = _num(s, "put_call_ratio")

    # Dealer positioning (pinning probability + hedging pressure) — derived from
    # the same GEX fields already in the snapshot, so this stays loop-cheap.
    try:
        gex_dict = {
            "ok": bool(s.get("call_wall") or s.get("put_wall") or flip),
            "spot_price": price, "dealer_regime": feats["dealer_regime"],
            "gamma_flip": flip, "call_wall": s.get("call_wall"), "put_wall": s.get("put_wall"),
            "net_gex_total": _num(s, "net_gex_total", 0.0), "opex_day": s.get("opex_day"),
        }
        from backend.bot.flowintel import dealer_positioning as _dp

        dp = _dp(gex_dict)
        feats["pinning_probability"] = dp.pinning_probability
        feats["hedging_pressure"] = dp.hedging_pressure
        feats["dominant_wall"] = dp.dominant_wall
    except Exception:
        feats["pinning_probability"] = 0.0
        feats["hedging_pressure"] = "normal"
        feats["dominant_wall"] = "neutral"

    # --- institutional flow ---
    bull = _num(s, "bullish_sweeps", 0.0) or 0.0
    bear = _num(s, "bearish_sweeps", 0.0) or 0.0
    total_sweeps = bull + bear
    feats["flow_bullishness"] = round((bull - bear) / total_sweeps, 3) if total_sweeps else 0.0
    feats["premarket_bullish_sweeps"] = _num(s, "premarket_bullish_sweeps", 0.0)
    feats["darkpool_bias"] = 1.0 if s.get("darkpool_confirms") else 0.0

    # --- news / sentiment / events ---
    feats["news_sentiment"] = _num(s, "news_score", 0.0)
    feats["earnings_days"] = _num(s, "earnings_days")

    # --- composite directional bias: average of the signed components present ---
    components = [
        feats["rsi_bias"], feats["macd_bias"], feats["trend_bias"],
        feats["flow_bullishness"], _clip((feats["news_sentiment"] or 0.0)),
    ]
    feats["composite_bias"] = round(sum(components) / len(components), 3)

    # MITS Phase 14.A — direction-aware cohort CI width.
    # Snapshot must carry ticker + pattern (and optionally regime,
    # vol_state, direction) for the lookup. When direction=LONG and
    # the cohort cell has populated long-side bounds, we use those;
    # mirror for SHORT. Falls back to the overall Wilson CI when
    # direction-specific bounds are NULL (e.g. uniform-direction cell).
    #
    # MITS Phase 14.E — also surface raw CI bounds + sample size + posterior
    # so the grade explainer can quote them in plain English without
    # re-fetching the cohort cell.
    cohort = _cohort_fields(s)
    feats["cohort_ci_width"] = cohort["cohort_ci_width"]
    feats["cohort_ci_lower"] = cohort["cohort_ci_lower"]
    feats["cohort_ci_upper"] = cohort["cohort_ci_upper"]
    feats["cohort_sample_size"] = cohort["cohort_sample_size"]
    feats["cohort_posterior"] = cohort["cohort_posterior"]
    return feats


_EMPTY_COHORT: Dict[str, Optional[float]] = {
    "cohort_ci_width": None,
    "cohort_ci_lower": None,
    "cohort_ci_upper": None,
    "cohort_sample_size": None,
    "cohort_posterior": None,
}


def _cohort_fields(snapshot: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Pull the direction-aware Wilson CI bounds, width, sample size and
    posterior for the (ticker, pattern, regime, vol_state) cohort.

    Returns a dict with all five keys; values are None when the snapshot
    lacks the lookup keys or the cohort isn't found. Best-effort — never
    raises. When ``direction=LONG`` and the cohort cell has long-side
    bounds populated, those bounds are used; mirror for SHORT. Falls
    back to the overall Wilson CI when direction-specific bounds are
    NULL (e.g. a uniform-direction cell).
    """
    out = dict(_EMPTY_COHORT)
    pattern = snapshot.get("pattern")
    ticker = snapshot.get("ticker")
    if not pattern or not ticker:
        return out
    direction = (snapshot.get("direction") or "").upper()
    regime = snapshot.get("regime") or snapshot.get("market_regime") or "unknown"
    vol_state = snapshot.get("vol_state") or "normal"
    horizon = snapshot.get("horizon") or "5d"
    try:
        from backend.bot.corpus.knowledge_graph import (
            get_posterior_with_fallback,
        )
    except Exception:
        return out
    try:
        entry = get_posterior_with_fallback(
            ticker=str(ticker).upper(), pattern=str(pattern),
            regime=str(regime), vol_state=str(vol_state),
            horizon=str(horizon), sample_split="combined",
        )
    except Exception:
        return out
    if not entry:
        return out

    # Sample size + posterior are direction-agnostic — surface whatever the
    # entry exposes.
    n = entry.get("n")
    if n is not None:
        try:
            out["cohort_sample_size"] = int(n)
        except Exception:
            pass
    post = entry.get("posterior")
    if post is None:
        post = entry.get("posterior_win_rate")
    if post is not None:
        try:
            out["cohort_posterior"] = round(float(post), 4)
        except Exception:
            pass

    lo: Optional[float] = None
    hi: Optional[float] = None
    if direction == "LONG":
        lo = entry.get("confidence_lower_long")
        hi = entry.get("confidence_upper_long")
    elif direction == "SHORT":
        lo = entry.get("confidence_lower_short")
        hi = entry.get("confidence_upper_short")
    if lo is None or hi is None:
        lo = entry.get("confidence_lower")
        hi = entry.get("confidence_upper")

    if lo is not None and hi is not None:
        try:
            lo_f = float(lo)
            hi_f = float(hi)
            out["cohort_ci_lower"] = round(lo_f, 4)
            out["cohort_ci_upper"] = round(hi_f, 4)
            out["cohort_ci_width"] = round(hi_f - lo_f, 4)
            return out
        except Exception:
            pass

    # No direction-specific or overall bounds — fall back to the entry's
    # pre-computed width if any.
    width = entry.get("ci_width")
    if width is not None:
        try:
            out["cohort_ci_width"] = round(float(width), 4)
        except Exception:
            pass
    return out
