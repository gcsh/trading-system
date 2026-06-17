"""MITS Phase 11.C — Finnhub company-news ingest + sentiment scoring.

Finnhub free tier:
    - 60 req/min hard cap
    - US-stock /company-news goes back to ~2015
    - Endpoint: GET /company-news?symbol=...&from=YYYY-MM-DD&to=YYYY-MM-DD
    - Response: array of {id, headline, summary, source, datetime (epoch),
                          url, image, category, related}

We pull in calendar windows (default 60 days) to stay under the
per-response size cap Finnhub silently applies. Idempotent via
``UniqueConstraint(ticker, article_id)``.

Two write paths:
  1. SQLite ``news_articles`` table — typed silver-layer rows, indexed
     by (ticker, published_at) so the feature layer can do efficient
     time-window lookups.
  2. Bronze parquet via ``backend.bot.data.lake.write_bronze`` — raw
     payload preserved for replay / re-embedding.

Sentiment is scored at ingest time (FinBERT primary, VADER fallback)
because:
  - The model is finance-tuned — re-scoring during replay risks
    drift if the model changes.
  - Score-at-ingest amortizes the model load across the full backfill
    instead of per-cycle on every Brain prompt.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, insert as sa_insert
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sentiment import score_headline_summary
from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import SETTINGS, TUNABLES
from backend.db import session_scope
from backend.models.news_article import NewsArticle

logger = logging.getLogger(__name__)


FINNHUB_BASE = "https://finnhub.io/api/v1"


# ── public shape ──────────────────────────────────────────────────────


@dataclass
class NewsItem:
    article_id: str
    ticker: str
    headline: str
    summary: Optional[str]
    source: Optional[str]
    published_at: datetime
    url: Optional[str]
    category: Optional[str]
    raw: Dict[str, Any]


# ── rate limit (per-process token bucket, 60 req/min) ─────────────────


class _FinnhubBucket:
    """Token bucket sized for Finnhub's 60 req/min free-tier ceiling.

    We deliberately under-throttle relative to the published 60/min: the
    default ``finnhub_rate_per_minute`` TUNABLE is 55 so a slow clock or
    a burst-window edge doesn't trip a 429. Configurable so the operator
    can dial back to 30/min if Finnhub starts complaining.
    """

    def __init__(self, per_minute: float) -> None:
        self.rate = max(1.0, float(per_minute)) / 60.0
        self.capacity = max(1.0, float(per_minute))
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.last_refill) * self.rate,
                )
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = max(0.05, (1.0 - self.tokens) / self.rate)
            time.sleep(min(wait, 1.0))


_BUCKET: Optional[_FinnhubBucket] = None
_BUCKET_LOCK = threading.Lock()


def _bucket() -> _FinnhubBucket:
    global _BUCKET
    if _BUCKET is not None:
        return _BUCKET
    with _BUCKET_LOCK:
        if _BUCKET is None:
            per_min = float(
                getattr(TUNABLES, "finnhub_news_rate_per_minute", 55.0))
            _BUCKET = _FinnhubBucket(per_min)
        return _BUCKET


# ── HTTP ──────────────────────────────────────────────────────────────


def _api_key() -> str:
    """Read the API key in this order:
       1. ``FINNHUB_API_KEY`` env (set by deploy /etc/profile or .env)
       2. ``SETTINGS.finnhub_api_key`` (parsed once at process start)
    """
    return (os.environ.get("FINNHUB_API_KEY") or
            getattr(SETTINGS, "finnhub_api_key", "") or "").strip()


def _http_get(path: str, params: Dict[str, Any]) -> Tuple[int, str]:
    """``requests.get`` against Finnhub. Returns ``(status, body)``;
    raises on transport errors so the orchestrator can retry."""
    import requests
    key = _api_key()
    if not key:
        raise RuntimeError(
            "finnhub: no API key configured (set FINNHUB_API_KEY)")
    bucket = _bucket()
    bucket.acquire()
    url = f"{FINNHUB_BASE}{path}"
    merged = dict(params)
    merged["token"] = key
    timeout = float(getattr(TUNABLES, "finnhub_http_timeout_sec", 30.0))
    resp = requests.get(url, params=merged, timeout=timeout)
    return (resp.status_code, resp.text)


# ── normalization ─────────────────────────────────────────────────────


def _coerce_dt(epoch: Any) -> Optional[datetime]:
    if epoch in (None, "", 0):
        return None
    try:
        ts = int(epoch)
    except (TypeError, ValueError):
        try:
            ts = int(float(epoch))
        except Exception:
            return None
    if ts <= 0:
        return None
    # Finnhub returns UTC epoch seconds.
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _normalize(item: Dict[str, Any], ticker: str) -> Optional[NewsItem]:
    headline = (item.get("headline") or "").strip()
    if not headline:
        return None
    raw_id = item.get("id")
    if raw_id in (None, ""):
        # Finnhub generally always returns an id; if it doesn't we
        # synthesize one from (datetime + headline hash) so the row
        # still has a stable dedup key.
        dt_raw = item.get("datetime") or 0
        try:
            article_id = f"syn-{int(dt_raw)}-{abs(hash(headline)) % 10**12}"
        except Exception:
            article_id = f"syn-0-{abs(hash(headline)) % 10**12}"
    else:
        article_id = str(raw_id)
    published_at = _coerce_dt(item.get("datetime"))
    if published_at is None:
        return None
    return NewsItem(
        article_id=article_id,
        ticker=ticker.upper(),
        headline=headline,
        summary=(item.get("summary") or "").strip() or None,
        source=(item.get("source") or "").strip() or None,
        published_at=published_at,
        url=(item.get("url") or "").strip() or None,
        category=(item.get("category") or "").strip() or None,
        raw=dict(item),
    )


# ── public API ────────────────────────────────────────────────────────


def fetch_news(ticker: str, start_date: date, end_date: date,
                  *, window_days: Optional[int] = None
                  ) -> List[NewsItem]:
    """Pull all company-news articles for ``ticker`` in [start_date,
    end_date]. Paginates internally in ``window_days`` chunks so a
    multi-year window doesn't trip Finnhub's silent size cap on a
    single response."""
    ticker = ticker.upper().strip()
    if not ticker:
        return []
    if end_date < start_date:
        return []
    win = int(window_days or getattr(
        TUNABLES, "finnhub_news_window_days", 60))
    if win <= 0:
        win = 60
    seen_ids: set = set()
    out: List[NewsItem] = []
    cursor = start_date
    while cursor <= end_date:
        sub_end = min(end_date, cursor + timedelta(days=win - 1))
        params = {
            "symbol": ticker,
            "from": cursor.isoformat(),
            "to": sub_end.isoformat(),
        }
        status, body = _http_get("/company-news", params)
        if status == 429:
            # Rate-limit. Sleep + retry once within the same window
            # before bubbling up — token bucket SHOULD prevent this,
            # but Finnhub's window calculation drifts a bit.
            time.sleep(2.0)
            status, body = _http_get("/company-news", params)
        if status == 401 or status == 403:
            raise RuntimeError(
                f"finnhub: auth rejected status={status} body={body[:160]}")
        if status != 200:
            raise RuntimeError(
                f"finnhub: company-news status={status} ticker={ticker} "
                f"window=[{cursor},{sub_end}] body={body[:160]}")
        try:
            import json
            arr = json.loads(body)
        except Exception:
            arr = []
        if not isinstance(arr, list):
            arr = []
        for raw in arr:
            if not isinstance(raw, dict):
                continue
            item = _normalize(raw, ticker)
            if item is None:
                continue
            if item.article_id in seen_ids:
                continue
            seen_ids.add(item.article_id)
            out.append(item)
        cursor = sub_end + timedelta(days=1)
    return out


# ── persistence ───────────────────────────────────────────────────────


def write_news_rows(items: List[NewsItem]) -> int:
    """Bulk-insert ``items`` into ``news_articles``. Scores sentiment per
    row before insert. Idempotent via UniqueConstraint (skip rows where
    (ticker, article_id) already exists). Returns rows inserted."""
    if not items:
        return 0
    inserted = 0
    # Score sentiment up-front so we don't hold a session open during
    # potentially-slow model inference.
    scored: List[Tuple[NewsItem, str, float, str]] = []
    for it in items:
        try:
            sent = score_headline_summary(it.headline, it.summary)
            scored.append((it, sent.label, sent.score, sent.model))
        except Exception:
            logger.exception(
                "finnhub_news: sentiment scoring crashed for article_id=%s",
                it.article_id,
            )
            scored.append((it, "neutral", 0.0, "empty"))
    try:
        with session_scope() as s:
            tickers = {it.ticker for it, *_ in scored}
            ids = {it.article_id for it, *_ in scored}
            existing_rows = s.execute(
                select(NewsArticle.ticker, NewsArticle.article_id)
                .where(NewsArticle.ticker.in_(tickers))
                .where(NewsArticle.article_id.in_(ids))
            ).all()
            existing = {(r[0], r[1]) for r in existing_rows}
            seen_batch: set = set()
            payloads: List[Dict[str, Any]] = []
            for item, label, score, model in scored:
                key = (item.ticker, item.article_id)
                if key in existing or key in seen_batch:
                    continue
                seen_batch.add(key)
                payloads.append({
                    "article_id": item.article_id,
                    "ticker": item.ticker,
                    "headline": item.headline[:2000],
                    "summary": (item.summary or "")[:8000] or None,
                    "source": item.source,
                    "published_at": item.published_at,
                    "url": item.url,
                    "category": item.category,
                    "sentiment_label": label,
                    "sentiment_score": score,
                    "sentiment_model": model,
                })
            if payloads:
                bind = s.get_bind()
                dialect_name = getattr(bind.dialect, "name", "") if bind else ""
                if dialect_name == "sqlite":
                    stmt = (
                        sa_insert(NewsArticle.__table__)
                        .prefix_with("OR IGNORE")
                    )
                    result = s.execute(stmt, payloads)
                    rc = int(result.rowcount or 0)
                    if rc < 0:
                        rc = len(payloads)
                    inserted += rc
                else:
                    for payload in payloads:
                        try:
                            s.execute(
                                sa_insert(NewsArticle.__table__),
                                [payload],
                            )
                            inserted += 1
                        except IntegrityError:
                            s.rollback()
                            continue
    except Exception:
        logger.exception("finnhub_news: write_news_rows failed")
    return inserted


def write_news_bronze(ticker: str, items: List[NewsItem],
                          *, chunk_start: date, chunk_end: date) -> None:
    if not items:
        return
    try:
        from backend.bot.data import lake as _lake
        payload = []
        for it in items:
            row = dict(it.raw)
            row["normalized_published_at"] = it.published_at.isoformat()
            row["article_id"] = it.article_id
            row["ticker"] = it.ticker
            payload.append(row)
        _lake.write_bronze(
            source="finnhub", dtype="company_news",
            payload=payload, ticker=ticker,
            extra_tags={
                "chunk_start": chunk_start.isoformat(),
                "chunk_end": chunk_end.isoformat(),
            },
            request_url="finnhub://v1/company-news",
            source_version=__name__,
        )
    except Exception:
        logger.debug("finnhub_news: bronze write failed", exc_info=True)


# ── orchestrator callbacks ────────────────────────────────────────────


def finnhub_news_backfill_callback(ticker: str, chunk_start: date,
                                          chunk_end: date) -> CallbackResult:
    """SyncOrchestrator-shaped callback for the bulk + delta paths.

    Skip-no-op when the API key is missing — raises so the orchestrator
    marks the chunk error and retries on the next run (after the
    operator populates ``FINNHUB_API_KEY``).
    """
    if not _api_key():
        raise RuntimeError(
            "finnhub: no API key configured (set FINNHUB_API_KEY)")
    items = fetch_news(ticker, chunk_start, chunk_end)
    if not items:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "no_data"},
        )
    inserted = write_news_rows(items)
    write_news_bronze(ticker, items,
                          chunk_start=chunk_start, chunk_end=chunk_end)
    last_dt = max(it.published_at.date() for it in items)
    # Cap the watermark at chunk_end — articles can land with a
    # publication date slightly past the chunk window if Finnhub's
    # backfill of older sources is incomplete.
    last_complete = min(chunk_end, max(last_dt, chunk_start))
    return CallbackResult(
        last_completed_date=last_complete,
        rows_written=inserted,
        metadata={"raw_articles": len(items)},
    )


def finnhub_news_delta_callback(ticker: str, chunk_start: date,
                                       chunk_end: date) -> CallbackResult:
    """Same body as the backfill callback today — kept as a named entry
    point so we can split rate limits / windows later if needed without
    re-wiring the orchestrator registry."""
    return finnhub_news_backfill_callback(ticker, chunk_start, chunk_end)


__all__ = [
    "NewsItem",
    "fetch_news",
    "write_news_rows",
    "write_news_bronze",
    "finnhub_news_backfill_callback",
    "finnhub_news_delta_callback",
]
