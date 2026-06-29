"""Deterministic no-LLM fast-path thesis composer.

Runs in every `/analysis/{ticker}` request before the optional deep
Claude path. Free, side-effect-free, and uses the same authoritative
direction map as the detectors so the surfaced action matches what
the live engine would route.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from backend.bot.analysis._actions import (
    build_suggested_action,
    is_bearish_pattern,
    is_bullish_pattern,
)


_FALLBACK_INVALIDATION = [
    "Position closes the day below the breakdown level",
    "Volume dries up below the 20-bar median",
    "Regime flips to choppy or counter-trend",
]


@dataclass
class FastComposerResult:
    """One pattern's deterministic thesis card.

    rank ∈ [0, 1] mirrors `eod_analysis._rank_score` normalised by /8.0
    (matches log1p(N)·posterior for N ≈ 3000).
    uncertainty ∈ [0, 1] combines Wilson CI width and sample thinness.
    """

    pattern: str
    action: str
    direction: str
    rank: float
    uncertainty: float
    headline: str
    thesis_paragraph: str
    suggested_action: Optional[Dict[str, Any]]
    invalidation: List[str]
    rationale_tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern,
            "action": self.action,
            "direction": self.direction,
            "rank": round(float(self.rank), 4),
            "uncertainty": round(float(self.uncertainty), 4),
            "headline": self.headline,
            "thesis_paragraph": self.thesis_paragraph,
            "suggested_action": self.suggested_action,
            "invalidation": list(self.invalidation),
            "rationale_tags": list(self.rationale_tags),
        }


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _ci_width_from_knowledge(k: Dict[str, Any]) -> float:
    """Extract CI width from a knowledge dict. Looks at the explicit
    ci_width field first, then confidence_band tuple, then the lower
    /upper pair. Returns 0.0 when none are available — that means we
    don't know the CI so the uncertainty falls back to sample size."""
    v = k.get("ci_width")
    try:
        if v is not None:
            return float(v)
    except Exception:
        pass
    band = k.get("confidence_band")
    if isinstance(band, (list, tuple)) and len(band) == 2:
        lo, hi = band
        try:
            if lo is not None and hi is not None:
                return float(hi) - float(lo)
        except Exception:
            pass
    lo = k.get("confidence_lower")
    hi = k.get("confidence_upper")
    try:
        if lo is not None and hi is not None:
            return float(hi) - float(lo)
    except Exception:
        pass
    return 0.0


def _compute_rank(posterior: float, sample_size: int) -> float:
    """rank = posterior * log(1 + N) / 8.0, mirrors eod_analysis._rank_score.

    The /8.0 normaliser keeps the range in roughly [0, 1]: at N=3000
    posterior=0.65 → 0.65 * log1p(3000) / 8 ≈ 0.65 * 8.01 / 8 ≈ 0.651.
    Caps at 1.0.
    """
    import math
    p = _clip(float(posterior or 0.0))
    n = max(0, int(sample_size or 0))
    return _clip(p * math.log1p(n) / 8.0)


def _compute_uncertainty(ci_width: float, sample_size: int) -> float:
    """uncertainty = clip(ci_width × 1.5 + (1 − sample_strength), 0, 1).

    sample_strength = min(1, N / 200).
    """
    sample_strength = min(1.0, max(0, int(sample_size or 0)) / 200.0)
    return _clip(float(ci_width or 0.0) * 1.5 + (1.0 - sample_strength))


def _direction_for(pattern: str) -> str:
    if is_bullish_pattern(pattern):
        return "long_call"
    if is_bearish_pattern(pattern):
        return "long_put"
    return "neutral"


def _action_for(pattern: str, posterior: float) -> str:
    if posterior < float(TUNABLES.fast_composer_min_posterior):
        return "SKIP"
    if is_bullish_pattern(pattern):
        return "BUY_CALL"
    if is_bearish_pattern(pattern):
        return "BUY_PUT"
    return "SKIP"


def _headline(
    pattern: str, ticker: str, regime: str, posterior: float, n: int,
) -> str:
    return (
        f"{pattern.replace('_', ' ').title()} on {ticker} in {regime} "
        f"regime — {posterior*100:.0f}% historical win rate (N={n})."
    )


def _thesis(
    pattern: str, ticker: str, knowledge: Dict[str, Any],
    ci_width: float, uncertainty: float,
) -> str:
    n = int(knowledge.get("sample_size") or 0)
    post = float(knowledge.get("posterior_win_rate") or 0.0)
    avg_ret = float(knowledge.get("avg_return_pct") or 0.0)
    avg_hold = knowledge.get("avg_hold_minutes")
    hold_txt = f" over {avg_hold:.0f} minutes" if avg_hold else ""
    ci_clause = ""
    if ci_width > 0:
        ci_clause = f" CI width {ci_width:.2f}."
    uncertainty_clause = ""
    if uncertainty >= 0.6:
        uncertainty_clause = " High uncertainty — treat as low conviction."
    elif uncertainty <= 0.3:
        uncertainty_clause = " Low uncertainty — high conviction read."
    return (
        f"The {pattern} signal fired on {ticker}. Historical cohort "
        f"matches show a posterior win rate of {post*100:.0f}% over "
        f"N={n} prior occurrences. Average move on winners was "
        f"{avg_ret*100:+.1f}%{hold_txt}.{ci_clause}{uncertainty_clause}"
    )


def _rationale_tags(
    pattern: str, posterior: float, n: int,
    ci_width: float, regime: Optional[str], vol_state: Optional[str],
) -> List[str]:
    tags: List[str] = []
    if posterior >= 0.65:
        tags.append("strong_posterior")
    elif posterior < float(TUNABLES.fast_composer_min_posterior):
        tags.append("weak_posterior")
    if n >= 100:
        tags.append("high_sample")
    elif n < 30:
        tags.append("thin_sample")
    if ci_width >= float(TUNABLES.cohort_ci_width_warn_threshold):
        tags.append("wide_ci")
    if regime and regime not in ("unknown", None):
        tags.append(f"regime_{regime}")
    if vol_state and vol_state not in ("normal", None):
        tags.append(f"vol_{vol_state}")
    return tags


def fast_compose_one(
    *,
    ticker: str,
    pattern: str,
    cohort: Dict[str, Any],
    spot: Optional[float],
    regime: Optional[str] = None,
    vol_state: Optional[str] = None,
    features: Optional[Dict[str, Any]] = None,
) -> FastComposerResult:
    """Compose ONE deterministic pattern thesis. Pure + side-effect-free."""
    posterior = float(cohort.get("posterior_win_rate") or 0.0)
    n = int(cohort.get("sample_size") or 0)
    ci_width = _ci_width_from_knowledge(cohort)
    rank = _compute_rank(posterior, n)
    uncertainty = _compute_uncertainty(ci_width, n)
    action = _action_for(pattern, posterior)
    direction = _direction_for(pattern)
    eff_regime = regime or cohort.get("regime") or "unknown"
    eff_vol = vol_state or cohort.get("vol_state") or "normal"
    headline = _headline(pattern, ticker, eff_regime, posterior, n)
    thesis_paragraph = _thesis(
        pattern, ticker, cohort, ci_width, uncertainty,
    )
    if action == "SKIP":
        suggested = None
    else:
        suggested = build_suggested_action(
            pattern=pattern, knowledge=cohort, ticker=ticker, spot=spot,
        )
    tags = _rationale_tags(
        pattern, posterior, n, ci_width, eff_regime, eff_vol,
    )
    return FastComposerResult(
        pattern=pattern,
        action=action,
        direction=direction,
        rank=rank,
        uncertainty=uncertainty,
        headline=headline,
        thesis_paragraph=thesis_paragraph,
        suggested_action=suggested,
        invalidation=list(_FALLBACK_INVALIDATION),
        rationale_tags=tags,
    )


def fast_compose_all(
    *,
    ticker: str,
    knowledge: Dict[str, Dict[str, Any]],
    spot: Optional[float],
    features: Optional[Dict[str, Any]] = None,
) -> Dict[str, FastComposerResult]:
    """Compose fast theses for every pattern in the knowledge dict."""
    out: Dict[str, FastComposerResult] = {}
    for pat, cohort in (knowledge or {}).items():
        if not isinstance(cohort, dict):
            continue
        out[pat] = fast_compose_one(
            ticker=ticker, pattern=pat, cohort=cohort, spot=spot,
            regime=cohort.get("regime"),
            vol_state=cohort.get("vol_state"),
            features=features,
        )
    return out


__all__ = [
    "FastComposerResult",
    "fast_compose_one",
    "fast_compose_all",
]
