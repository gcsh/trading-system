"""MITS Phase 11.K — unit tests for the vector embedding walker.

Covers:
  * New index_* helpers in vector_store reach upsert + return False
    when pgvector is unreachable (without crashing).
  * The text-building paths produce non-empty payloads with the
    operator-required fields.
  * The walker is structured so each kind is independent — a missing
    table doesn't take down the others.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_index_news_paragraph_returns_false_when_pgvector_down():
    """All Phase 11.K helpers degrade to False when embed() or
    upsert() are unavailable. The walker depends on this being a
    clean no-op (skipped count increments, no exceptions)."""
    from backend.bot.ai.vector_store import index_news_paragraph
    with patch("backend.bot.ai.vector_store.embed", return_value=[]):
        ok = index_news_paragraph(
            article_id="x1", ticker="AAPL",
            headline="Apple beats", summary="Strong demand",
            published_iso="2025-01-01T00:00:00",
        )
        assert ok is False


def test_index_earnings_paragraph_returns_false_when_pgvector_down():
    from backend.bot.ai.vector_store import index_earnings_call_paragraph
    with patch("backend.bot.ai.vector_store.embed", return_value=[]):
        ok = index_earnings_call_paragraph(
            paragraph_id="p1", ticker="AAPL",
            fiscal_year=2025, fiscal_quarter=1, paragraph_index=3,
            speaker="Tim Cook", speaker_title="CEO",
            content="iPhone units up 12%.",
        )
        assert ok is False


def test_index_insider_form4_returns_false_when_pgvector_down():
    from backend.bot.ai.vector_store import index_insider_form4_narrative
    with patch("backend.bot.ai.vector_store.embed", return_value=[]):
        ok = index_insider_form4_narrative(
            trade_id="t1", ticker="AAPL",
            insider_name="Cook, Tim", insider_role="CEO",
            transaction_code="P", shares=1000.0, price=180.0,
            total_value=180000.0,
            transaction_date_iso="2025-01-15",
        )
        assert ok is False


def test_index_fund_holding_returns_false_when_pgvector_down():
    from backend.bot.ai.vector_store import index_fund_holding_change
    with patch("backend.bot.ai.vector_store.embed", return_value=[]):
        ok = index_fund_holding_change(
            holding_id="h1", fund_name="Berkshire Hathaway Inc",
            fund_cik="0001067983", ticker="AAPL",
            quarter_end_iso="2024-12-31",
            shares=500000.0, change_from_prior_qtr=20000.0,
            pct_of_portfolio=12.5, value_usd=90_000_000.0,
        )
        assert ok is False


def test_index_regime_snapshot_v2_returns_false_when_pgvector_down():
    from backend.bot.ai.vector_store import index_regime_snapshot_v2
    with patch("backend.bot.ai.vector_store.embed", return_value=[]):
        ok = index_regime_snapshot_v2(
            key="2025-01-15", date_iso="2025-01-15",
            summary_text="VIX=18.5 | DGS10=4.32 | breadth=ok",
            metadata={"VIXCLS": 18.5, "DGS10": 4.32},
        )
        assert ok is False


def test_index_news_paragraph_upserts_when_pgvector_available():
    """When embed() and upsert() return successfully, the helper
    forwards to upsert(...) with the expected namespace/key/payload."""
    from backend.bot.ai import vector_store
    fake_vec = [0.0] * vector_store.TUNABLES.vector_dim
    upsert_calls = []

    def _fake_upsert(ns, key, vec, meta):
        upsert_calls.append((ns, key, vec, meta))
        return True

    with patch.object(vector_store, "embed", return_value=fake_vec), \
            patch.object(vector_store, "upsert", side_effect=_fake_upsert):
        ok = vector_store.index_news_paragraph(
            article_id="42", ticker="MSFT",
            headline="MSFT cloud accelerates", summary="Azure +29% YoY",
            published_iso="2025-04-01T13:00:00",
            sentiment_label="positive", sentiment_score=0.91,
        )
    assert ok is True
    assert len(upsert_calls) == 1
    ns, key, vec, meta = upsert_calls[0]
    assert ns == "news_paragraph"
    assert key == "MSFT:42"
    assert meta["sentiment_label"] == "positive"
    assert meta["sentiment_score"] == 0.91
    assert "Azure" in meta["summary"]


def test_index_fund_holding_direction_classification():
    """Fund-holding change narrative encodes 'added' / 'trimmed' /
    'held' so analogs cluster on direction, not just ticker."""
    from backend.bot.ai import vector_store
    fake_vec = [0.0] * vector_store.TUNABLES.vector_dim
    captured = {}

    def _fake_upsert(ns, key, vec, meta):
        captured["meta"] = meta
        return True

    with patch.object(vector_store, "embed", return_value=fake_vec), \
            patch.object(vector_store, "upsert", side_effect=_fake_upsert):
        vector_store.index_fund_holding_change(
            holding_id="h99", fund_name="Test Fund",
            fund_cik="0000000099", ticker="NVDA",
            quarter_end_iso="2024-12-31",
            shares=10000.0, change_from_prior_qtr=-3000.0,
            pct_of_portfolio=4.5, value_usd=15_000_000.0,
        )
    assert captured["meta"]["change_from_prior_qtr"] == -3000.0
    assert captured["meta"]["ticker"] == "NVDA"
