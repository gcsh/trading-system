"""MITS Phase 11.C — Finnhub-sourced US company news article cache.

One row per (ticker, article_id) — Finnhub's ``id`` field. We dedupe on
that natural key. Sentiment fields are populated at fetch time by
:mod:`backend.bot.data.sentiment` (FinBERT primary, VADER fallback).

External-cache-shaped — survives a paper reset (re-fetching 5y × 40
tickers worth of news on every reset would cost hours and burn the 60
req/min Finnhub free-tier budget).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, Float, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        # Finnhub recycles article IDs across tickers when a piece names
        # multiple companies. Uniqueness key is (ticker, article_id) so
        # we get one row per (ticker, article_id) — same article landing
        # in both AAPL and MSFT pulls produces two rows, one per
        # association. That's what the feature layer wants.
        UniqueConstraint("ticker", "article_id",
                         name="uq_news_articles_ticker_article"),
        Index("ix_news_articles_ticker_published", "ticker", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Finnhub's article id (large int — stored as string to avoid
    # SQLite int-width surprises on giant numeric IDs from some vendors).
    article_id: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    headline: Mapped[str] = mapped_column(String)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # UTC datetime. Finnhub returns epoch seconds; we coerce on ingest.
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # FinBERT classification — "positive" / "neutral" / "negative".
    # NULL when sentiment scoring failed (offline / model missing /
    # truncated text). Default NULL so the column can be added safely
    # to an existing table via _auto_migrate.
    sentiment_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Confidence of the predicted label, in [0, 1].
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Source-of-record tag — "finbert" or "vader" so the feature layer
    # can weigh down low-quality fallback rows.
    sentiment_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "article_id": self.article_id,
            "ticker": self.ticker,
            "headline": self.headline,
            "summary": self.summary,
            "source": self.source,
            "published_at": (self.published_at.isoformat()
                              if self.published_at else None),
            "url": self.url,
            "category": self.category,
            "sentiment_label": self.sentiment_label,
            "sentiment_score": self.sentiment_score,
            "sentiment_model": self.sentiment_model,
            "ingested_at": (self.ingested_at.isoformat()
                             if self.ingested_at else None),
        }
