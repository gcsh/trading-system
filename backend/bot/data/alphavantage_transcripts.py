"""MITS Phase 11.D — AlphaVantage earnings-call transcript ingest.

AlphaVantage free tier:
    - 25 requests/day (hard cap, daily counter resets at 00:00 UTC)
    - 5 requests/minute (token-bucket pacing)
    - Endpoint: GET /query?function=EARNINGS_CALL_TRANSCRIPT
                       &symbol=AAPL&quarter=2024q3&apikey=...
    - Response: {transcript: [{speaker, title, content}, ...],
                 symbol: "AAPL", quarter: "2024Q3"}

Because of the 25/day cap the FULL backfill (40 tickers × 20 quarters =
800 calls) takes ~32 days. The SyncOrchestrator handles this naturally:
when the daily counter exhausts, ``fetch_transcript`` raises a
:class:`DailyQuotaExhausted` which the callback re-raises so the
chunk is marked error + retried on the next orchestrator pass.

Write paths:
  1. SQLite ``earnings_transcripts`` + ``transcript_paragraphs`` tables.
  2. Bronze parquet via :func:`backend.bot.data.lake.write_bronze`.

Quarter format: AlphaVantage takes ``2024q3`` (lowercase q). Some
endpoints accept ``2024Q3``; we send the lowercase form which the
docs guarantee.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.earnings_transcript import EarningsTranscript
from backend.models.transcript_paragraph import TranscriptParagraph

logger = logging.getLogger(__name__)


ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"


# ── shapes ────────────────────────────────────────────────────────────


@dataclass
class TranscriptParagraphData:
    paragraph_index: int
    speaker: Optional[str]
    speaker_title: Optional[str]
    content: str


@dataclass
class TranscriptData:
    ticker: str
    fiscal_year: int
    fiscal_quarter: int  # 1-4
    paragraphs: List[TranscriptParagraphData] = field(default_factory=list)
    report_date: Optional[date] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        out: List[str] = []
        for p in self.paragraphs:
            head = (p.speaker or "").strip()
            if p.speaker_title:
                head = f"{head} ({p.speaker_title})" if head else p.speaker_title
            out.append(f"[{head}] {p.content.strip()}" if head
                       else p.content.strip())
        return "\n\n".join(t for t in out if t).strip()


# ── permanent / transient errors ──────────────────────────────────────


class DailyQuotaExhausted(RuntimeError):
    """Raised when AlphaVantage signals the 25 req/day cap is hit.
    Treated as transient by the orchestrator — the next-day retry will
    succeed."""


class NoTranscriptAvailable(RuntimeError):
    """Raised when AlphaVantage returns an empty / sentinel response
    for a quarter that has no transcript on file (pre-IPO, missed call,
    AlphaVantage gap)."""


# ── rate limiter (5/min + 25/day) ─────────────────────────────────────


class _AlphaBucket:
    """Two-axis limiter: 5 req/min AND 25 req/day. Both must permit
    before a call goes out."""

    def __init__(self, per_minute: float, per_day: int) -> None:
        self.per_minute = max(1.0, float(per_minute))
        self.per_day = max(1, int(per_day))
        self._minute_window: deque = deque()  # monotonic timestamps
        self._day_count_anchor: Optional[datetime] = None
        self._day_count: int = 0
        self._lock = threading.Lock()

    def _refresh_day(self) -> None:
        now_utc = datetime.now(timezone.utc)
        if self._day_count_anchor is None or \
                now_utc.date() != self._day_count_anchor.date():
            self._day_count_anchor = now_utc
            self._day_count = 0

    def remaining_today(self) -> int:
        with self._lock:
            self._refresh_day()
            return max(0, self.per_day - self._day_count)

    def acquire_or_raise(self) -> None:
        """Either acquires a token immediately, blocks briefly on the
        minute window, OR raises :class:`DailyQuotaExhausted` when the
        daily cap is reached. The orchestrator treats the latter as a
        retryable error so the chunk gets re-queued tomorrow."""
        while True:
            with self._lock:
                self._refresh_day()
                if self._day_count >= self.per_day:
                    raise DailyQuotaExhausted(
                        f"alphavantage: daily quota exhausted "
                        f"({self._day_count}/{self.per_day})"
                    )
                # Trim minute-window deque to entries within last 60s.
                cutoff = time.monotonic() - 60.0
                while self._minute_window and \
                        self._minute_window[0] < cutoff:
                    self._minute_window.popleft()
                if len(self._minute_window) < self.per_minute:
                    self._minute_window.append(time.monotonic())
                    self._day_count += 1
                    return
                # Sleep until the oldest slot ages out + a small jitter.
                sleep_for = (self._minute_window[0] + 60.0) - time.monotonic()
            time.sleep(max(0.5, min(sleep_for, 5.0)))


_BUCKET: Optional[_AlphaBucket] = None
_BUCKET_LOCK = threading.Lock()


def _bucket() -> _AlphaBucket:
    global _BUCKET
    if _BUCKET is not None:
        return _BUCKET
    with _BUCKET_LOCK:
        if _BUCKET is None:
            per_min = float(getattr(
                TUNABLES, "alphavantage_rate_per_minute", 5.0))
            per_day = int(getattr(
                TUNABLES, "alphavantage_rate_per_day", 25))
            _BUCKET = _AlphaBucket(per_min, per_day)
        return _BUCKET


def reset_bucket_for_tests() -> None:
    """Drop the singleton bucket. Tests call this between cases so the
    rate-limit state from the prior test doesn't leak across."""
    global _BUCKET
    with _BUCKET_LOCK:
        _BUCKET = None


# ── HTTP ──────────────────────────────────────────────────────────────


def _api_key() -> str:
    return (os.environ.get("ALPHAVANTAGE_API_KEY") or
            os.environ.get("TB_ALPHAVANTAGE_API_KEY") or "").strip()


def _http_get(params: Dict[str, Any]) -> Tuple[int, str]:
    import requests
    key = _api_key()
    if not key:
        raise RuntimeError(
            "alphavantage: no API key configured "
            "(set ALPHAVANTAGE_API_KEY)"
        )
    bucket = _bucket()
    bucket.acquire_or_raise()
    merged = dict(params)
    merged["apikey"] = key
    timeout = float(getattr(TUNABLES, "alphavantage_http_timeout_sec", 30.0))
    resp = requests.get(ALPHAVANTAGE_BASE, params=merged, timeout=timeout)
    return (resp.status_code, resp.text)


# ── quarter helpers ───────────────────────────────────────────────────


_QUARTER_RE = re.compile(r"^(\d{4})\s*[qQ]\s*([1-4])$")


def parse_quarter_token(token: str) -> Tuple[int, int]:
    """``"2024q3"`` → ``(2024, 3)``. Accepts upper / lower / spaced.
    Raises ``ValueError`` on a malformed token."""
    if not token:
        raise ValueError("empty quarter token")
    m = _QUARTER_RE.match(token.strip())
    if not m:
        raise ValueError(f"invalid quarter token: {token!r}")
    return int(m.group(1)), int(m.group(2))


def format_quarter_token(year: int, quarter: int) -> str:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"invalid quarter {quarter}")
    return f"{int(year):04d}q{int(quarter)}"


def quarters_between(start_token: str, end_token: str
                       ) -> List[Tuple[int, int]]:
    """Inclusive list of (year, quarter) tuples between ``start_token``
    and ``end_token``. Order: chronological."""
    s_y, s_q = parse_quarter_token(start_token)
    e_y, e_q = parse_quarter_token(end_token)
    if (e_y, e_q) < (s_y, s_q):
        return []
    out: List[Tuple[int, int]] = []
    y, q = s_y, s_q
    while (y, q) <= (e_y, e_q):
        out.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return out


# ── parser ────────────────────────────────────────────────────────────


_NO_DATA_KEYS = (
    "Note", "Information", "Error Message",
)


def _parse_payload(ticker: str, year: int, quarter: int,
                       body: str) -> TranscriptData:
    """Convert AlphaVantage response body into :class:`TranscriptData`.
    Raises ``NoTranscriptAvailable`` on empty / missing-quarter
    responses; ``DailyQuotaExhausted`` on the rate-limit note pattern.
    """
    try:
        payload = json.loads(body)
    except Exception as exc:
        raise RuntimeError(
            f"alphavantage: failed to parse JSON ticker={ticker} "
            f"year={year} quarter={quarter}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise NoTranscriptAvailable(
            f"alphavantage: non-dict response ticker={ticker} "
            f"year={year} quarter={quarter}")
    # Sentinel keys = error / quota messages.
    for k in _NO_DATA_KEYS:
        if k in payload:
            msg = str(payload[k])[:200]
            if "API call frequency" in msg or "premium" in msg.lower() or \
                    "rate limit" in msg.lower():
                raise DailyQuotaExhausted(
                    f"alphavantage: {k}={msg}")
            # Treat "no data" notes as empty quarter — not an error.
            raise NoTranscriptAvailable(
                f"alphavantage: {k}={msg}")
    transcript = payload.get("transcript") or []
    if not isinstance(transcript, list) or not transcript:
        raise NoTranscriptAvailable(
            f"alphavantage: empty transcript ticker={ticker} "
            f"year={year} quarter={quarter}")
    paragraphs: List[TranscriptParagraphData] = []
    for idx, raw in enumerate(transcript):
        if not isinstance(raw, dict):
            continue
        content = (raw.get("content") or "").strip()
        if not content:
            continue
        paragraphs.append(TranscriptParagraphData(
            paragraph_index=idx,
            speaker=(raw.get("speaker") or "").strip() or None,
            speaker_title=(raw.get("title") or "").strip() or None,
            content=content,
        ))
    if not paragraphs:
        raise NoTranscriptAvailable(
            f"alphavantage: transcript had no non-empty paragraphs "
            f"ticker={ticker} year={year} quarter={quarter}")
    # Best-effort report-date extraction. AlphaVantage doesn't expose a
    # canonical field, so we attempt a few common keys + skip on miss.
    report_date: Optional[date] = None
    for key in ("reportDate", "earnings_call_date", "callDate"):
        raw_dt = payload.get(key)
        if raw_dt:
            try:
                report_date = datetime.strptime(
                    str(raw_dt)[:10], "%Y-%m-%d").date()
                break
            except Exception:
                continue
    return TranscriptData(
        ticker=ticker.upper(),
        fiscal_year=year,
        fiscal_quarter=quarter,
        paragraphs=paragraphs,
        report_date=report_date,
        metadata={k: v for k, v in payload.items() if k != "transcript"},
        raw=payload,
    )


# ── public fetch ──────────────────────────────────────────────────────


def fetch_transcript(ticker: str, year: int, quarter: int
                       ) -> Optional[TranscriptData]:
    """Fetch a single transcript. Returns ``None`` when the quarter
    has no transcript on file (pre-IPO, missed call). Raises
    ``DailyQuotaExhausted`` on quota hit so the orchestrator can mark
    the chunk error and retry tomorrow."""
    ticker_norm = ticker.upper().strip()
    if not ticker_norm:
        return None
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"invalid quarter {quarter}")
    qtok = format_quarter_token(year, quarter)
    params = {
        "function": "EARNINGS_CALL_TRANSCRIPT",
        "symbol": ticker_norm,
        "quarter": qtok,
    }
    status, body = _http_get(params)
    if status == 429:
        raise DailyQuotaExhausted(
            f"alphavantage: HTTP 429 (rate limit) ticker={ticker_norm} q={qtok}")
    if status in (401, 403):
        raise RuntimeError(
            f"alphavantage: auth rejected status={status} body={body[:160]}")
    if status != 200:
        raise RuntimeError(
            f"alphavantage: status={status} ticker={ticker_norm} q={qtok} "
            f"body={body[:160]}")
    try:
        return _parse_payload(ticker_norm, year, quarter, body)
    except NoTranscriptAvailable:
        return None


# ── persistence ───────────────────────────────────────────────────────


def write_transcript(td: TranscriptData) -> Tuple[int, int]:
    """Upsert into ``earnings_transcripts`` + bulk-insert paragraphs.
    Returns ``(transcripts_written, paragraphs_written)``. Idempotent —
    if the row already exists with non-zero paragraph_count, we no-op."""
    if not td.paragraphs:
        return (0, 0)
    transcripts_written = 0
    paragraphs_written = 0
    max_text = int(getattr(TUNABLES, "transcript_full_text_max_chars", 2_000_000))
    full_text = td.full_text[:max_text]
    try:
        with session_scope() as s:
            existing = s.execute(
                select(EarningsTranscript)
                .where(EarningsTranscript.ticker == td.ticker)
                .where(EarningsTranscript.fiscal_year == td.fiscal_year)
                .where(EarningsTranscript.fiscal_quarter == td.fiscal_quarter)
            ).scalar_one_or_none()
            if existing is not None:
                if int(existing.paragraph_count or 0) > 0:
                    # Already ingested — no-op.
                    return (0, 0)
                transcript_row = existing
                # Replace header fields in case the prior write was a
                # stub from a half-failed attempt.
                transcript_row.report_date = td.report_date
                transcript_row.full_text = full_text
                transcript_row.metadata_json = json.dumps(td.metadata)[:200_000]
                transcript_row.paragraph_count = len(td.paragraphs)
                transcript_row.fetched_at = datetime.utcnow()
            else:
                transcript_row = EarningsTranscript(
                    ticker=td.ticker,
                    fiscal_year=int(td.fiscal_year),
                    fiscal_quarter=int(td.fiscal_quarter),
                    report_date=td.report_date,
                    full_text=full_text,
                    metadata_json=json.dumps(td.metadata)[:200_000],
                    paragraph_count=len(td.paragraphs),
                )
                s.add(transcript_row)
                transcripts_written = 1
            s.flush()
            for p in td.paragraphs:
                try:
                    s.add(TranscriptParagraph(
                        transcript_id=transcript_row.id,
                        ticker=td.ticker,
                        fiscal_year=int(td.fiscal_year),
                        fiscal_quarter=int(td.fiscal_quarter),
                        paragraph_index=int(p.paragraph_index),
                        speaker=p.speaker,
                        speaker_title=p.speaker_title,
                        content=p.content[:64_000],
                    ))
                    paragraphs_written += 1
                except IntegrityError:
                    s.rollback()
                    continue
    except Exception:
        logger.exception(
            "alphavantage_transcripts: write_transcript failed ticker=%s "
            "year=%s quarter=%s", td.ticker, td.fiscal_year, td.fiscal_quarter,
        )
    return (transcripts_written, paragraphs_written)


def write_transcript_bronze(td: TranscriptData) -> None:
    try:
        from backend.bot.data import lake as _lake
        _lake.write_bronze(
            source="alphavantage",
            dtype="earnings_call_transcript",
            payload=[td.raw or {"transcript": [
                {"speaker": p.speaker, "title": p.speaker_title,
                 "content": p.content, "paragraph_index": p.paragraph_index}
                for p in td.paragraphs
            ]}],
            ticker=td.ticker,
            extra_tags={
                "fiscal_year": str(td.fiscal_year),
                "fiscal_quarter": str(td.fiscal_quarter),
                "quarter_token": format_quarter_token(
                    td.fiscal_year, td.fiscal_quarter),
            },
            request_url="alphavantage://EARNINGS_CALL_TRANSCRIPT",
            source_version=__name__,
        )
    except Exception:
        logger.debug("alphavantage_transcripts: bronze write failed",
                      exc_info=True)


# ── orchestrator callback ─────────────────────────────────────────────


def _date_to_quarter(d: date) -> Tuple[int, int]:
    q = ((d.month - 1) // 3) + 1
    return (d.year, q)


def _quarters_in_window(chunk_start: date,
                            chunk_end: date) -> List[Tuple[int, int]]:
    sy, sq = _date_to_quarter(chunk_start)
    ey, eq = _date_to_quarter(chunk_end)
    out: List[Tuple[int, int]] = []
    y, q = sy, sq
    while (y, q) <= (ey, eq):
        out.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return out


def _quarter_end_date(year: int, quarter: int) -> date:
    end_month = quarter * 3
    if end_month == 12:
        return date(year, 12, 31)
    return date(year, end_month + 1, 1) - timedelta(days=1)


def alphavantage_transcripts_backfill_callback(
        ticker: str, chunk_start: date, chunk_end: date) -> CallbackResult:
    """For each (year, quarter) overlapping the chunk window, fetch the
    transcript (if missing) and persist it. Skips already-stored rows.

    Errors:
      * ``DailyQuotaExhausted`` — bubbles up so the orchestrator
        records error_text + retries on the next pass (tomorrow).
      * ``RuntimeError`` / ``NoTranscriptAvailable`` — caught locally.
        Missing quarters are recorded as "no data" in metadata.
    """
    if not _api_key():
        raise RuntimeError(
            "alphavantage: no API key configured "
            "(set ALPHAVANTAGE_API_KEY)")
    quarters = _quarters_in_window(chunk_start, chunk_end)
    if not quarters:
        return CallbackResult(
            last_completed_date=chunk_end, rows_written=0,
            metadata={"reason": "no_quarters_in_window"},
        )
    written_paragraphs = 0
    written_transcripts = 0
    skipped_existing = 0
    skipped_no_data = 0
    last_completed = chunk_start - timedelta(days=1)
    errored: List[str] = []
    for (y, q) in quarters:
        # Skip already-present rows so the daily quota isn't wasted.
        try:
            with session_scope() as s:
                existing = s.execute(
                    select(EarningsTranscript.id,
                            EarningsTranscript.paragraph_count)
                    .where(EarningsTranscript.ticker == ticker.upper())
                    .where(EarningsTranscript.fiscal_year == y)
                    .where(EarningsTranscript.fiscal_quarter == q)
                ).first()
                if existing is not None and int(existing[1] or 0) > 0:
                    skipped_existing += 1
                    last_completed = max(last_completed, _quarter_end_date(y, q))
                    continue
        except Exception:
            logger.exception(
                "alphavantage_transcripts: existence check failed for "
                "%s %sQ%s", ticker, y, q,
            )
        try:
            td = fetch_transcript(ticker, y, q)
        except DailyQuotaExhausted:
            # Bubble up — orchestrator marks chunk error + reschedules
            # tomorrow. The transcripts we've already written so far
            # this run are persisted; only the un-processed quarters
            # need another pass.
            raise
        except Exception as exc:
            logger.warning(
                "alphavantage_transcripts: fetch failed ticker=%s "
                "year=%s quarter=%s: %s", ticker, y, q, exc,
            )
            errored.append(f"{y}Q{q}:{type(exc).__name__}")
            continue
        if td is None:
            skipped_no_data += 1
            last_completed = max(last_completed, _quarter_end_date(y, q))
            continue
        wt, wp = write_transcript(td)
        written_transcripts += wt
        written_paragraphs += wp
        write_transcript_bronze(td)
        last_completed = max(last_completed, _quarter_end_date(y, q))
    final_last = min(chunk_end, last_completed) if last_completed >= chunk_start \
        else chunk_end
    return CallbackResult(
        last_completed_date=final_last,
        rows_written=written_paragraphs,
        metadata={
            "transcripts_written": written_transcripts,
            "paragraphs_written": written_paragraphs,
            "skipped_existing": skipped_existing,
            "skipped_no_data": skipped_no_data,
            "errored": errored,
            "quarters_attempted": [f"{y}Q{q}" for (y, q) in quarters],
        },
    )


__all__ = [
    "TranscriptData",
    "TranscriptParagraphData",
    "DailyQuotaExhausted",
    "NoTranscriptAvailable",
    "parse_quarter_token",
    "format_quarter_token",
    "quarters_between",
    "fetch_transcript",
    "write_transcript",
    "write_transcript_bronze",
    "alphavantage_transcripts_backfill_callback",
    "reset_bucket_for_tests",
]
