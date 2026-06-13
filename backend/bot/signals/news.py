"""Fetch recent news headlines and score them with VADER sentiment."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from backend.config import SETTINGS

_ANALYZER = SentimentIntensityAnalyzer()


@dataclass
class NewsItem:
    title: str
    description: str
    url: str
    published_at: str
    sentiment: float  # -1 .. 1


@dataclass
class NewsSnapshot:
    items: List[NewsItem]
    average_sentiment: float


def _score(text: str) -> float:
    if not text:
        return 0.0
    return float(_ANALYZER.polarity_scores(text)["compound"])


def fetch_news(ticker: str, max_items: int = 5, client=None) -> List[dict]:
    """Fetch raw news articles for a ticker via NewsAPI.

    A ``client`` may be injected by tests; in production we lazily build a
    ``NewsApiClient`` from the configured key.
    """
    if client is None:
        if not SETTINGS.news_api_key:
            return []
        from newsapi import NewsApiClient

        client = NewsApiClient(api_key=SETTINGS.news_api_key)
    response = client.get_everything(
        q=ticker,
        language="en",
        sort_by="publishedAt",
        page_size=max_items,
    )
    return response.get("articles", [])[:max_items]


def score_articles(articles: List[dict]) -> NewsSnapshot:
    """Convert raw articles into scored :class:`NewsItem` objects."""
    items: List[NewsItem] = []
    for article in articles:
        title = article.get("title") or ""
        description = article.get("description") or ""
        sentiment = (_score(title) + _score(description)) / 2 if description else _score(title)
        items.append(
            NewsItem(
                title=title,
                description=description,
                url=article.get("url", ""),
                published_at=article.get("publishedAt", ""),
                sentiment=sentiment,
            )
        )
    average = sum(item.sentiment for item in items) / len(items) if items else 0.0
    return NewsSnapshot(items=items, average_sentiment=average)


def news_snapshot(ticker: str, client=None) -> NewsSnapshot:
    """Convenience wrapper: fetch then score."""
    articles = fetch_news(ticker, client=client)
    return score_articles(articles)
