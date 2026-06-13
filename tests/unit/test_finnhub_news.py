"""MITS Phase 11.C — Finnhub news + sentiment unit tests.

Coverage:
  1. ``fetch_news`` paginates by ``window_days`` across the requested
     range, dedupes article IDs across windows, and normalizes
     malformed rows out.
  2. ``write_news_rows`` is idempotent on (ticker, article_id).
  3. ``finnhub_news_backfill_callback`` produces a CallbackResult with
     the expected ``last_completed_date`` + ``rows_written``.
  4. Sentiment scorer never raises and always returns one of the
     three FinBERT labels.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Tuple

import pytest


# ── helpers ───────────────────────────────────────────────────────────


def _make_article(article_id: int, ts: int,
                       headline: str = "AAPL beats earnings",
                       summary: str = "Strong iPhone sales drove revenue.",
                       source: str = "Reuters") -> Dict[str, Any]:
    return {
        "id": article_id,
        "headline": headline,
        "summary": summary,
        "source": source,
        "datetime": ts,
        "url": f"https://example.com/{article_id}",
        "category": "company news",
    }


def _epoch(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _install_http_stub(monkeypatch, responses: List[Tuple[int, str]]):
    """Patch ``backend.bot.data.finnhub_news._http_get`` to return the
    canned responses in sequence. Each call pops one off the list."""
    queue = list(responses)
    calls: List[Tuple[str, Dict[str, Any]]] = []

    def _stub(path: str, params: Dict[str, Any]) -> Tuple[int, str]:
        calls.append((path, dict(params)))
        if not queue:
            return (200, "[]")
        return queue.pop(0)

    import backend.bot.data.finnhub_news as mod
    monkeypatch.setattr(mod, "_http_get", _stub)
    monkeypatch.setattr(mod, "_api_key", lambda: "test-key")
    return calls


# ── tests ─────────────────────────────────────────────────────────────


def test_fetch_news_paginates_and_dedupes(temp_db, monkeypatch) -> None:
    from backend.bot.data.finnhub_news import fetch_news

    window_a = [
        _make_article(1001, _epoch(2024, 1, 5)),
        _make_article(1002, _epoch(2024, 1, 15)),
    ]
    window_b = [
        # Repeated ID 1002 — must be deduped.
        _make_article(1002, _epoch(2024, 1, 15)),
        _make_article(1003, _epoch(2024, 3, 5)),
    ]
    calls = _install_http_stub(monkeypatch, [
        (200, json.dumps(window_a)),
        (200, json.dumps(window_b)),
    ])

    items = fetch_news("AAPL", date(2024, 1, 1), date(2024, 3, 31),
                          window_days=60)
    assert len(items) == 3
    ids = {it.article_id for it in items}
    assert ids == {"1001", "1002", "1003"}
    # Must have hit at least two windows.
    assert len(calls) >= 2


def test_fetch_news_skips_malformed_rows(temp_db, monkeypatch) -> None:
    from backend.bot.data.finnhub_news import fetch_news

    payload = [
        _make_article(2001, _epoch(2024, 5, 1)),
        # No headline → dropped.
        {"id": 2002, "datetime": _epoch(2024, 5, 2), "summary": "x"},
        # No datetime → dropped.
        {"id": 2003, "headline": "x"},
    ]
    _install_http_stub(monkeypatch, [(200, json.dumps(payload))])

    items = fetch_news("MSFT", date(2024, 5, 1), date(2024, 5, 10),
                          window_days=60)
    assert len(items) == 1
    assert items[0].article_id == "2001"


def test_write_news_rows_idempotent(temp_db, monkeypatch) -> None:
    from backend.bot.data.finnhub_news import (
        NewsItem, write_news_rows,
    )
    # Avoid loading FinBERT or VADER in CI: monkeypatch sentiment.
    import backend.bot.data.finnhub_news as mod
    from backend.bot.data.sentiment import SentimentResult
    monkeypatch.setattr(
        mod, "score_headline_summary",
        lambda h, s: SentimentResult(label="neutral", score=0.5,
                                        model="finbert"),
    )
    items = [
        NewsItem(article_id="9001", ticker="AAPL",
                 headline="Test article",
                 summary="Some summary",
                 source="Reuters",
                 published_at=datetime(2024, 7, 1, 14, 30),
                 url="https://example.com/9001",
                 category="company news",
                 raw={"id": 9001}),
    ]
    inserted_first = write_news_rows(items)
    inserted_second = write_news_rows(items)
    assert inserted_first == 1
    assert inserted_second == 0


def test_finnhub_backfill_callback_no_data(temp_db, monkeypatch) -> None:
    from backend.bot.data.finnhub_news import (
        finnhub_news_backfill_callback,
    )
    _install_http_stub(monkeypatch, [(200, "[]")])

    result = finnhub_news_backfill_callback(
        "AAPL", date(2024, 1, 1), date(2024, 2, 1))
    assert result.rows_written == 0
    assert result.last_completed_date == date(2024, 2, 1)


def test_sentiment_score_text_never_raises() -> None:
    from backend.bot.data.sentiment import score_text
    for txt in ["", "  ", "Apple earnings crushed estimates",
                "Tesla guidance was below consensus"]:
        result = score_text(txt)
        assert result.label in ("positive", "neutral", "negative")
        assert 0.0 <= result.score <= 1.0
        assert result.model in ("finbert", "vader", "empty")
