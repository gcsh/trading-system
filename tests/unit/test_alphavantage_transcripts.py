"""MITS Phase 11.D — AlphaVantage transcripts ingest tests.

Coverage:
  1. Quarter token parsing + range generation.
  2. ``fetch_transcript`` handles the three permissible outcomes:
       - happy path: returns parsed TranscriptData
       - missing-quarter sentinel: returns None
       - quota / rate-limit note: raises DailyQuotaExhausted
  3. ``write_transcript`` writes a header row + N paragraph rows; a
     second call is a no-op.
  4. Backfill callback skips already-stored quarters without burning
     quota.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple

import pytest


def _set_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-key")


def _install_http_stub(monkeypatch, responses: List[Tuple[int, str]]):
    import backend.bot.data.alphavantage_transcripts as mod
    mod.reset_bucket_for_tests()
    queue = list(responses)
    calls: List[Dict[str, Any]] = []

    def _stub(params: Dict[str, Any]) -> Tuple[int, str]:
        calls.append(dict(params))
        if not queue:
            return (200, "{}")
        return queue.pop(0)

    monkeypatch.setattr(mod, "_http_get", _stub)
    return calls


def _sample_transcript_payload() -> Dict[str, Any]:
    return {
        "symbol": "AAPL",
        "quarter": "2024Q3",
        "transcript": [
            {"speaker": "Tim Cook", "title": "CEO",
             "content": "iPhone sales continue to lead the segment."},
            {"speaker": "Luca Maestri", "title": "CFO",
             "content": "Operating margin expanded 120 bps."},
            {"speaker": "Analyst", "title": "Goldman Sachs",
             "content": "How do you think about FX headwinds?"},
        ],
    }


# ── tests ─────────────────────────────────────────────────────────────


def test_quarter_token_roundtrip() -> None:
    from backend.bot.data.alphavantage_transcripts import (
        format_quarter_token, parse_quarter_token, quarters_between,
    )
    assert parse_quarter_token("2024q3") == (2024, 3)
    assert parse_quarter_token("2023Q4") == (2023, 4)
    assert format_quarter_token(2024, 3) == "2024q3"
    q = quarters_between("2024q1", "2024q4")
    assert q == [(2024, 1), (2024, 2), (2024, 3), (2024, 4)]
    q2 = quarters_between("2023q4", "2024q2")
    assert q2 == [(2023, 4), (2024, 1), (2024, 2)]


def test_fetch_transcript_happy_path(temp_db, monkeypatch) -> None:
    _set_api_key(monkeypatch)
    from backend.bot.data.alphavantage_transcripts import fetch_transcript
    _install_http_stub(monkeypatch, [
        (200, json.dumps(_sample_transcript_payload())),
    ])
    td = fetch_transcript("AAPL", 2024, 3)
    assert td is not None
    assert td.ticker == "AAPL"
    assert td.fiscal_year == 2024
    assert td.fiscal_quarter == 3
    assert len(td.paragraphs) == 3
    assert td.paragraphs[0].speaker == "Tim Cook"
    assert td.paragraphs[0].speaker_title == "CEO"


def test_fetch_transcript_returns_none_on_missing(temp_db, monkeypatch) -> None:
    _set_api_key(monkeypatch)
    from backend.bot.data.alphavantage_transcripts import fetch_transcript
    _install_http_stub(monkeypatch, [
        (200, json.dumps({
            "Note": "This is the most recent free-tier note; no data.",
        })),
    ])
    td = fetch_transcript("AAPL", 2021, 1)
    assert td is None


def test_fetch_transcript_quota_raises(temp_db, monkeypatch) -> None:
    _set_api_key(monkeypatch)
    from backend.bot.data.alphavantage_transcripts import (
        DailyQuotaExhausted, fetch_transcript,
    )
    _install_http_stub(monkeypatch, [
        (200, json.dumps({
            "Note": ("Thank you for using Alpha Vantage! Our standard "
                       "API call frequency is 5 calls per minute and "
                       "500 calls per day."),
        })),
    ])
    with pytest.raises(DailyQuotaExhausted):
        fetch_transcript("AAPL", 2024, 1)


def test_write_transcript_idempotent(temp_db, monkeypatch) -> None:
    _set_api_key(monkeypatch)
    from backend.bot.data.alphavantage_transcripts import (
        TranscriptData, TranscriptParagraphData, write_transcript,
    )
    td = TranscriptData(
        ticker="AAPL", fiscal_year=2024, fiscal_quarter=3,
        paragraphs=[
            TranscriptParagraphData(0, "Tim Cook", "CEO", "Hello world."),
            TranscriptParagraphData(1, "Luca Maestri", "CFO",
                                          "Margins expanded."),
        ],
        report_date=date(2024, 10, 31),
        metadata={"symbol": "AAPL"},
        raw={"symbol": "AAPL"},
    )
    t1, p1 = write_transcript(td)
    assert t1 == 1 and p1 == 2
    t2, p2 = write_transcript(td)
    assert t2 == 0 and p2 == 0


def test_backfill_callback_skips_existing(temp_db, monkeypatch) -> None:
    """Already-stored transcripts must NOT consume any API call."""
    _set_api_key(monkeypatch)
    from backend.bot.data.alphavantage_transcripts import (
        TranscriptData, TranscriptParagraphData, write_transcript,
        alphavantage_transcripts_backfill_callback,
    )
    # Pre-populate Q3 2024 so the callback should skip it.
    seed = TranscriptData(
        ticker="AAPL", fiscal_year=2024, fiscal_quarter=3,
        paragraphs=[TranscriptParagraphData(0, "T", "CEO", "x")],
        report_date=None, metadata={}, raw={})
    write_transcript(seed)

    # Stub HTTP: every call should be a no-op (the callback should not
    # actually hit the wire for the seeded quarter).
    calls = _install_http_stub(monkeypatch, [])
    # Run a window that covers ONLY Q3 2024. The callback should walk
    # the single quarter, see it's already in the DB, and skip.
    result = alphavantage_transcripts_backfill_callback(
        "AAPL", date(2024, 7, 1), date(2024, 9, 30),
    )
    assert result.rows_written == 0
    assert len(calls) == 0  # zero HTTP — quota preserved
    assert result.metadata.get("skipped_existing", 0) == 1
