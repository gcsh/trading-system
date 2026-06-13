"""Aggregate sentiment from multiple sources into a single -1..1 score."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_ANALYZER = SentimentIntensityAnalyzer()


@dataclass
class SentimentSnapshot:
    score: float  # -1 .. 1
    components: dict


def score_text(text: str) -> float:
    if not text:
        return 0.0
    return float(_ANALYZER.polarity_scores(text)["compound"])


def aggregate(news_score: float, social_texts: Iterable[str] | None = None) -> SentimentSnapshot:
    """Combine news sentiment with optional social sentiment texts.

    The resulting score is a simple equal-weighted average. Missing sources are
    ignored so the bot can run even with no social feeds wired up.
    """
    components: dict = {"news": float(news_score)}
    scores = [float(news_score)]
    if social_texts:
        social_scores = [score_text(t) for t in social_texts]
        if social_scores:
            social_avg = sum(social_scores) / len(social_scores)
            components["social"] = social_avg
            scores.append(social_avg)
    final = sum(scores) / len(scores) if scores else 0.0
    final = max(-1.0, min(1.0, final))
    return SentimentSnapshot(score=final, components=components)
