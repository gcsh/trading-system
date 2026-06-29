"""Stage-12.B4 Composite Data Quality Score.

Every signal carries a 0-100 quality score derived from:

  • Per-source success (was the price feed live? did the options chain load?
    did news return?)  — from ``MarketSnapshot.source_errors``
  • Feed health staleness (minutes since the last good fetch per feed)
    — from ``bot/monitoring``
  • Completeness (how many of the canonical features are non-null?)

When quality drops below a threshold the engine dampens confidence and/or
abstains entirely — most trading failures come from bad data, not bad
models, so a low-quality decision is *worse than no decision*.

Pure / deterministic. No DB writes. Read-only over MarketSnapshot and
FeedHealth.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# Canonical feeds we expect snapshots to consume. Missing any of these
# drops the per-feed score; extras don't affect it.
_EXPECTED_FEEDS = ("price", "fundamentals", "news", "options", "flow")


# Minimum non-null coverage among these features for "complete" snapshot.
_CORE_FEATURES = (
    "price", "rsi", "macd", "ma50", "ma200", "vix", "iv_rank",
    "volume", "avg_volume",
)


@dataclass
class QualityScore:
    composite: int                       # 0-100
    feed_scores: Dict[str, int] = field(default_factory=dict)   # per-feed 0-100
    completeness: int = 0                # % of core features present
    source_errors: List[str] = field(default_factory=list)
    stale_feeds: List[str] = field(default_factory=list)
    band: str = "good"                   # excellent | good | degraded | poor
    confidence_multiplier: float = 1.0   # what to multiply signal conf by
    should_abstain: bool = False         # hard veto

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _band_for(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "degraded"
    return "poor"


def _confidence_multiplier_for(score: int) -> float:
    """Map a quality score to a confidence dampener.

    100 → 1.00; 85 → 1.00; 70 → 0.90; 50 → 0.75; 0 → 0.50.
    Linear within each band so the gradient is stable.
    """
    if score >= 85:
        return 1.0
    if score >= 70:
        return round(0.90 + (score - 70) / 15 * 0.10, 2)
    if score >= 50:
        return round(0.75 + (score - 50) / 20 * 0.15, 2)
    return round(0.50 + score / 50 * 0.25, 2)


def _feed_score_from_errors(feed: str, errors: Sequence[str]) -> int:
    """100 if the feed had no recorded errors, otherwise scale down by
    the number of errors mentioning this feed name (~25 pts per error)."""
    hits = sum(1 for e in errors if feed.lower() in e.lower())
    if hits == 0:
        return 100
    return max(0, 100 - hits * 25)


def _feed_score_from_staleness(feed_health: Optional[Dict[str, Any]],
                                  feed: str,
                                  *,
                                  fresh_minutes: float = 5.0,
                                  dead_minutes: float = 60.0) -> Optional[int]:
    """Translate "minutes since last success" into 0-100.

    Returns ``None`` when no health info is available for the feed.
    """
    if not feed_health or feed not in feed_health:
        return None
    info = feed_health[feed]
    if not isinstance(info, dict):
        return None
    minutes = info.get("minutes_since_last_success")
    if minutes is None:
        return None
    try:
        m = float(minutes)
    except Exception:
        return None
    if m <= fresh_minutes:
        return 100
    if m >= dead_minutes:
        return 0
    # Linear between fresh and dead.
    span = dead_minutes - fresh_minutes
    return int(max(0, min(100, 100 - (m - fresh_minutes) / span * 100)))


def _completeness(snapshot: Dict[str, Any]) -> int:
    if not snapshot:
        return 0
    have = sum(1 for k in _CORE_FEATURES
                  if snapshot.get(k) is not None and snapshot.get(k) != 0)
    return int(round(100 * have / len(_CORE_FEATURES)))


def score_data_quality(*,
                          snapshot: Optional[Dict[str, Any]] = None,
                          source_errors: Optional[Sequence[str]] = None,
                          feed_health: Optional[Dict[str, Any]] = None,
                          abstain_below: int = 40,
                          ) -> QualityScore:
    """Compute the composite quality score for a single decision.

    Composite formula (transparent):
       0.55 × mean(per-feed scores)  +  0.45 × completeness

    Decision rules:
       composite < ``abstain_below`` → ``should_abstain = True``
       composite < 85                 → ``confidence_multiplier`` < 1.0
    """
    snapshot = snapshot or {}
    source_errors = list(source_errors or [])
    feed_health = feed_health or {}

    feed_scores: Dict[str, int] = {}
    stale: List[str] = []
    for feed in _EXPECTED_FEEDS:
        err_score = _feed_score_from_errors(feed, source_errors)
        health_score = _feed_score_from_staleness(feed_health, feed)
        if health_score is None:
            score = err_score
        else:
            score = min(err_score, health_score)
        feed_scores[feed] = score
        if score < 50:
            stale.append(feed)

    avg_feed = (sum(feed_scores.values()) / len(feed_scores)) if feed_scores else 0
    completeness = _completeness(snapshot)
    composite = int(round(0.55 * avg_feed + 0.45 * completeness))
    band = _band_for(composite)
    mult = _confidence_multiplier_for(composite)
    return QualityScore(
        composite=composite,
        feed_scores=feed_scores,
        completeness=completeness,
        source_errors=source_errors[:10],
        stale_feeds=stale,
        band=band,
        confidence_multiplier=mult,
        should_abstain=composite < abstain_below,
    )
