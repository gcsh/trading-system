"""MITS Phase 11.G — bulk + delta sync orchestration.

The single owner of "have we pulled this (source, ticker) up to today?".

Two flows:

  * :meth:`SyncOrchestrator.bulk_backfill` — walks a long history window
    (e.g. 2006-01-01 → 2026-06-09) in calendar chunks. Per-chunk progress
    is persisted to :class:`BackfillProgress` so a crash / restart resumes
    from the first un-finished chunk instead of re-doing 20 years.
  * :meth:`SyncOrchestrator.delta_sync` — pulls the gap between
    ``DataWatermark.last_synced_through_date`` and ``today``. Cheap
    nightly refresh.

Design constraints (from the operator):

  - Config-driven: rate limits + chunk sizes live in
    :data:`TUNABLES` (no magic numbers).
  - Idempotent: re-running ``bulk_backfill`` for a fully-completed
    window is a no-op (every chunk's status is ``done``).
  - Crash-resumable: progress is persisted PER CHUNK and PER LAST
    COMPLETED DAY WITHIN A CHUNK, so even a SIGKILL mid-chunk loses at
    most a few rows.
  - Rate-limit aware: token-bucket per source obeys
    ``TUNABLES.sync_max_calls_per_second_<source>``.
  - Exponential backoff: any callback exception → exponential wait
    capped at ``sync_retry_backoff_cap_sec``, max
    ``sync_max_retry_attempts`` per chunk before marking it ``error``
    and moving on (the operator can manually re-queue later).

The orchestrator NEVER calls vendor endpoints directly — it dispatches
to a callback supplied by each source module (see
:mod:`backend.bot.data.thetadata_stocks`, :mod:`backend.bot.data.fred_expanded`,
etc.). Keeps source-specific HTTP / parquet logic out of the orchestrator.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.backfill_progress import BackfillProgress
from backend.models.data_watermark import DataWatermark

logger = logging.getLogger(__name__)


# ── public shapes ─────────────────────────────────────────────────────


@dataclass
class CallbackResult:
    """Returned by every source callback. Tells the orchestrator how
    far it actually got + how many rows landed, so we can persist
    progress accurately.

    ``last_completed_date`` — the latest CALENDAR date the callback
    successfully wrote rows for. May be earlier than ``chunk_end`` if
    the vendor returned a partial window.
    ``rows_written`` — total rows written by this callback invocation.
    """
    last_completed_date: Optional[date]
    rows_written: int
    # Free-form metadata the callback wants to log — e.g.
    # ``{"thetadata_pages": 4}``. Persisted into ``error_text`` only
    # when status != "done" (so we don't pollute the success path).
    metadata: Dict[str, object] = field(default_factory=dict)


# ``callback(ticker, chunk_start, chunk_end) -> CallbackResult``.
Callback = Callable[[str, date, date], CallbackResult]


@dataclass
class BackfillSummary:
    source: str
    ticker: str
    total_chunks: int
    completed_chunks: int
    error_chunks: int
    skipped_chunks: int  # already done before the run
    rows_written: int
    duration_sec: float

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ticker": self.ticker,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "error_chunks": self.error_chunks,
            "skipped_chunks": self.skipped_chunks,
            "rows_written": self.rows_written,
            "duration_sec": round(self.duration_sec, 2),
        }


# ── rate limiting ─────────────────────────────────────────────────────


class _TokenBucket:
    """Per-source token bucket. Thread-safe. Refills at
    ``rate`` tokens / second, capacity = ``rate`` (1-second burst)."""

    def __init__(self, rate: float) -> None:
        self.rate = max(0.1, float(rate))
        self.capacity = max(1.0, float(rate))
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
                # Tokens needed: 1 - self.tokens. Sleep until we have one.
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))


_BUCKETS: Dict[str, _TokenBucket] = {}
_BUCKETS_LOCK = threading.Lock()


def _bucket_for(source: str) -> _TokenBucket:
    base = _source_family(source)
    key = base
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            rate = _rate_for_source(base)
            bucket = _TokenBucket(rate)
            _BUCKETS[key] = bucket
        return bucket


def _source_family(source: str) -> str:
    """Source name → rate-limit family key.

    ``thetadata_stocks_daily``, ``thetadata_stocks_intraday_1m``,
    ``thetadata_iv_history`` all share the ``thetadata`` family.
    """
    s = source.lower()
    if s.startswith("thetadata"):
        return "thetadata"
    if s.startswith("fred"):
        return "fred"
    if s.startswith("finnhub"):
        return "finnhub"
    if s.startswith("alphavantage"):
        return "alphavantage"
    if s.startswith("edgar"):
        return "edgar"
    return s


def _rate_for_source(family: str) -> float:
    attr = f"sync_max_calls_per_second_{family}"
    return float(getattr(TUNABLES, attr, 4.0))


# ── orchestrator ──────────────────────────────────────────────────────


class SyncOrchestrator:
    """Owns watermark + chunk progress for every vendor pull."""

    def __init__(self) -> None:
        self._registry: Dict[str, Callback] = {}

    def register(self, source: str, callback: Callback) -> None:
        """Wire a callback for a given source key. Subsequent
        ``bulk_backfill`` / ``delta_sync`` calls dispatch to it."""
        self._registry[source] = callback

    # ── bulk ──────────────────────────────────────────────────────────

    def bulk_backfill(
        self,
        source: str,
        ticker: str,
        start_date: date,
        end_date: date,
        callback: Optional[Callback] = None,
        chunk_days: Optional[int] = None,
    ) -> BackfillSummary:
        """Walk ``[start_date, end_date]`` in calendar chunks calling
        ``callback`` for each. Idempotent + crash-resumable. Returns a
        summary report.

        ``chunk_days`` overrides the source-family default
        (:data:`TUNABLES.sync_chunk_days_daily` for daily-grain sources,
        ``sync_chunk_days_intraday`` for minute bars, ``sync_chunk_days_iv``
        for IV history). Default picked off ``source`` family.
        """
        cb = callback or self._registry.get(source)
        if cb is None:
            raise ValueError(
                f"sync_orchestrator: no callback registered for source={source!r}"
            )
        if end_date < start_date:
            raise ValueError(
                f"sync_orchestrator: end_date {end_date} < start_date {start_date}"
            )
        ticker = ticker.upper().strip()
        chunk_days_resolved = int(chunk_days or _default_chunk_days(source))
        if chunk_days_resolved <= 0:
            chunk_days_resolved = 365
        chunks = _split_into_chunks(start_date, end_date, chunk_days_resolved)
        bucket = _bucket_for(source)

        t_start = time.monotonic()
        completed = 0
        errored = 0
        skipped = 0
        rows_total = 0

        for c_start, c_end in chunks:
            # Look up the chunk's persisted state. Idempotent skip on
            # already-done chunks; resume on partial.
            row, _was_new = self._load_or_create_chunk(
                source, ticker, c_start, c_end)
            if row.status == "done":
                skipped += 1
                continue
            chunk_resume_start = c_start
            if row.last_completed_date:
                try:
                    last = date.fromisoformat(row.last_completed_date)
                    if last >= c_start and last < c_end:
                        chunk_resume_start = last + timedelta(days=1)
                except Exception:
                    pass
            if chunk_resume_start > c_end:
                # Edge case: the prior partial covered the full window;
                # mark as done so future runs skip immediately.
                self._mark_chunk(
                    source, ticker, c_start, status="done",
                    last_completed_date=c_end, rows_written_delta=0)
                completed += 1
                continue
            ok, rows_written, last_complete, err = self._run_chunk_with_retry(
                source, ticker, cb, chunk_resume_start, c_end, bucket,
            )
            self._mark_chunk(
                source, ticker, c_start,
                status=("done" if ok else "error"),
                last_completed_date=(last_complete.isoformat()
                                      if last_complete else row.last_completed_date),
                rows_written_delta=rows_written,
                error_text=(err if not ok else None),
            )
            rows_total += rows_written
            if ok:
                completed += 1
                # Advance the high-water-mark watermark as we go so a
                # mid-backfill crash still lets ``delta_sync`` pick up
                # cleanly from the LAST completed day.
                effective = last_complete or c_end
                self._update_watermark(
                    source, ticker,
                    last_synced_through_date=effective,
                    rows_last_sync=rows_written,
                    success=True, error_text=None,
                )
            else:
                errored += 1
                self._update_watermark(
                    source, ticker,
                    last_synced_through_date=(last_complete or
                                              (chunk_resume_start - timedelta(days=1))),
                    rows_last_sync=rows_written,
                    success=False, error_text=err,
                )

        duration = time.monotonic() - t_start
        return BackfillSummary(
            source=source, ticker=ticker,
            total_chunks=len(chunks),
            completed_chunks=completed,
            error_chunks=errored,
            skipped_chunks=skipped,
            rows_written=rows_total,
            duration_sec=duration,
        )

    # ── delta ─────────────────────────────────────────────────────────

    def delta_sync(
        self,
        source: str,
        ticker: str,
        callback: Optional[Callback] = None,
        *,
        as_of: Optional[date] = None,
    ) -> BackfillSummary:
        """Pull ``[watermark + 1, as_of]`` (or [genesis, as_of] on first run)
        and update the watermark on success. ``as_of`` defaults to today."""
        cb = callback or self._registry.get(source)
        if cb is None:
            raise ValueError(
                f"sync_orchestrator: no callback registered for source={source!r}"
            )
        as_of = as_of or date.today()
        ticker = ticker.upper().strip()
        wm = self._load_watermark(source, ticker)
        if wm and wm.last_synced_through_date:
            try:
                prev = date.fromisoformat(wm.last_synced_through_date)
                start = prev + timedelta(days=1)
            except Exception:
                start = as_of - timedelta(days=5)
        else:
            # No watermark — pick a reasonable seed window so the first
            # delta sync doesn't accidentally re-pull 10 years.
            start = as_of - timedelta(days=5)
        if start > as_of:
            return BackfillSummary(
                source=source, ticker=ticker,
                total_chunks=0, completed_chunks=0, error_chunks=0,
                skipped_chunks=1, rows_written=0, duration_sec=0.0,
            )
        return self.bulk_backfill(
            source, ticker, start, as_of, callback=cb,
            chunk_days=max(1, (as_of - start).days + 1),
        )

    def run_all_delta(
        self,
        sources: Iterable[str],
        tickers: Optional[Iterable[str]] = None,
        *,
        as_of: Optional[date] = None,
    ) -> Dict[str, List[BackfillSummary]]:
        """Fan out ``delta_sync`` across (sources × tickers).

        Tickers default to the universe loader; FRED-style sources pull
        their own series list when the caller passes ``tickers=None``
        AND the source family is FRED."""
        from backend.bot.data.universe import load_universe

        out: Dict[str, List[BackfillSummary]] = {}
        for source in sources:
            family = _source_family(source)
            if family == "fred" and tickers is None:
                from backend.bot.data.fred_expanded import EXPANDED_SERIES
                target = list(EXPANDED_SERIES)
            elif tickers is not None:
                target = [t.upper() for t in tickers]
            else:
                target = list(load_universe())
            results: List[BackfillSummary] = []
            for ticker in target:
                try:
                    summary = self.delta_sync(source, ticker, as_of=as_of)
                except Exception as exc:
                    logger.exception(
                        "delta_sync errored for source=%s ticker=%s",
                        source, ticker,
                    )
                    summary = BackfillSummary(
                        source=source, ticker=ticker,
                        total_chunks=0, completed_chunks=0,
                        error_chunks=1, skipped_chunks=0,
                        rows_written=0, duration_sec=0.0,
                    )
                results.append(summary)
            out[source] = results
        return out

    # ── retry envelope ────────────────────────────────────────────────

    def _run_chunk_with_retry(
        self,
        source: str,
        ticker: str,
        callback: Callback,
        chunk_start: date,
        chunk_end: date,
        bucket: _TokenBucket,
    ) -> Tuple[bool, int, Optional[date], Optional[str]]:
        max_attempts = int(getattr(TUNABLES, "sync_max_retry_attempts", 6))
        base = float(getattr(TUNABLES, "sync_retry_backoff_base_sec", 2.0))
        cap = float(getattr(TUNABLES, "sync_retry_backoff_cap_sec", 120.0))
        attempt = 0
        last_err: Optional[str] = None
        while attempt < max_attempts:
            attempt += 1
            bucket.acquire()
            try:
                result = callback(ticker, chunk_start, chunk_end)
                rows = int(getattr(result, "rows_written", 0))
                last_complete = getattr(result, "last_completed_date", None)
                if last_complete is None:
                    # Callback returned but did not record a date — treat
                    # as a zero-row no-op for THIS chunk so we don't
                    # advance the watermark falsely. Still success.
                    return (True, rows, None, None)
                return (True, rows, last_complete, None)
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                # Subscription / tier errors aren't transient — short-
                # circuit the retry loop so 40 tickers × 6 retries don't
                # add up to 240 wasted calls.
                permanent = (
                    type(exc).__name__ == "SubscriptionError"
                    or "SubscriptionError" in str(type(exc).__mro__)
                    or "403" in str(exc)[:80]
                    or "subscription" in str(exc).lower()[:200]
                )
                logger.warning(
                    "sync_orchestrator chunk failed source=%s ticker=%s "
                    "chunk=[%s,%s] attempt=%d/%d permanent=%s: %s",
                    source, ticker, chunk_start, chunk_end,
                    attempt, max_attempts, permanent, last_err,
                )
                if permanent or attempt >= max_attempts:
                    break
                wait = min(cap, base * (2 ** (attempt - 1)))
                time.sleep(wait)
        return (False, 0, None, last_err)

    # ── persistence ───────────────────────────────────────────────────

    def _load_watermark(self, source: str, ticker: str
                              ) -> Optional[DataWatermark]:
        try:
            with session_scope() as s:
                row = s.execute(
                    select(DataWatermark)
                    .where(DataWatermark.source == source)
                    .where(DataWatermark.ticker == ticker)
                ).scalar_one_or_none()
                if row is not None:
                    s.expunge(row)
                return row
        except Exception:
            logger.exception("watermark load failed source=%s ticker=%s",
                              source, ticker)
            return None

    def _update_watermark(
        self,
        source: str,
        ticker: str,
        *,
        last_synced_through_date: Optional[date],
        rows_last_sync: int,
        success: bool,
        error_text: Optional[str],
    ) -> None:
        try:
            with session_scope() as s:
                row = s.execute(
                    select(DataWatermark)
                    .where(DataWatermark.source == source)
                    .where(DataWatermark.ticker == ticker)
                ).scalar_one_or_none()
                now = datetime.utcnow()
                if row is None:
                    row = DataWatermark(
                        source=source, ticker=ticker,
                        last_synced_ts=now,
                        last_synced_through_date=(
                            last_synced_through_date.isoformat()
                            if last_synced_through_date else None),
                        rows_last_sync=int(rows_last_sync),
                        success=1 if success else 0,
                        error_text=error_text,
                        updated_at=now,
                    )
                    s.add(row)
                else:
                    row.last_synced_ts = now
                    if last_synced_through_date is not None:
                        new_through = last_synced_through_date.isoformat()
                        if not row.last_synced_through_date \
                                or new_through > row.last_synced_through_date:
                            row.last_synced_through_date = new_through
                    row.rows_last_sync = int(rows_last_sync)
                    row.success = 1 if success else 0
                    row.error_text = error_text
                    row.updated_at = now
        except IntegrityError:
            # Concurrent writer raced us; the row exists. Fall through
            # — next call will read its state cleanly.
            pass
        except Exception:
            logger.exception("watermark update failed source=%s ticker=%s",
                              source, ticker)

    def _load_or_create_chunk(
        self,
        source: str,
        ticker: str,
        c_start: date,
        c_end: date,
    ) -> Tuple[BackfillProgress, bool]:
        try:
            with session_scope() as s:
                row = s.execute(
                    select(BackfillProgress)
                    .where(BackfillProgress.source == source)
                    .where(BackfillProgress.ticker == ticker)
                    .where(BackfillProgress.date_range_start == c_start.isoformat())
                ).scalar_one_or_none()
                created = False
                if row is None:
                    row = BackfillProgress(
                        source=source, ticker=ticker,
                        date_range_start=c_start.isoformat(),
                        date_range_end=c_end.isoformat(),
                        status="pending",
                    )
                    s.add(row)
                    s.flush()
                    created = True
                s.expunge(row)
                return (row, created)
        except IntegrityError:
            # Race — re-read.
            with session_scope() as s2:
                row = s2.execute(
                    select(BackfillProgress)
                    .where(BackfillProgress.source == source)
                    .where(BackfillProgress.ticker == ticker)
                    .where(BackfillProgress.date_range_start == c_start.isoformat())
                ).scalar_one()
                s2.expunge(row)
                return (row, False)

    def _mark_chunk(
        self,
        source: str,
        ticker: str,
        c_start: date,
        *,
        status: str,
        last_completed_date: Optional[str],
        rows_written_delta: int,
        error_text: Optional[str] = None,
    ) -> None:
        try:
            with session_scope() as s:
                row = s.execute(
                    select(BackfillProgress)
                    .where(BackfillProgress.source == source)
                    .where(BackfillProgress.ticker == ticker)
                    .where(BackfillProgress.date_range_start == c_start.isoformat())
                ).scalar_one_or_none()
                if row is None:
                    # Shouldn't happen — _load_or_create_chunk created it.
                    return
                if status == "in_progress" and row.started_at is None:
                    row.started_at = datetime.utcnow()
                if status == "done":
                    row.completed_at = datetime.utcnow()
                if status == "error":
                    row.retry_count = int(row.retry_count or 0) + 1
                row.status = status
                if last_completed_date is not None:
                    row.last_completed_date = last_completed_date
                row.rows_written = int(row.rows_written or 0) + int(rows_written_delta)
                if error_text is not None:
                    row.error_text = error_text[:1900]
                elif status == "done":
                    row.error_text = None
        except Exception:
            logger.exception(
                "mark_chunk failed source=%s ticker=%s c_start=%s",
                source, ticker, c_start,
            )


# ── helpers ───────────────────────────────────────────────────────────


def _default_chunk_days(source: str) -> int:
    s = source.lower()
    if "intraday" in s:
        return int(TUNABLES.sync_chunk_days_intraday)
    if "iv" in s:
        return int(TUNABLES.sync_chunk_days_iv)
    return int(TUNABLES.sync_chunk_days_daily)


def _split_into_chunks(start: date, end: date, chunk_days: int
                          ) -> List[Tuple[date, date]]:
    """Inclusive [start, end] split into fixed-width windows. The final
    chunk may be shorter."""
    chunks: List[Tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        c_end = min(end, cursor + timedelta(days=chunk_days - 1))
        chunks.append((cursor, c_end))
        cursor = c_end + timedelta(days=1)
    return chunks


# ── module-level singleton ────────────────────────────────────────────


_ORCHESTRATOR: Optional[SyncOrchestrator] = None
_ORCHESTRATOR_LOCK = threading.Lock()


def get_orchestrator() -> SyncOrchestrator:
    """Process-wide shared orchestrator with all source callbacks
    pre-registered."""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        return _ORCHESTRATOR
    with _ORCHESTRATOR_LOCK:
        if _ORCHESTRATOR is not None:
            return _ORCHESTRATOR
        o = SyncOrchestrator()
        _register_default_callbacks(o)
        _ORCHESTRATOR = o
        return o


def _register_default_callbacks(orch: SyncOrchestrator) -> None:
    """Wire every shipped backfill source into the orchestrator. Imports
    are lazy so a broken vendor module doesn't take down the orchestrator
    for the other sources."""
    try:
        from backend.bot.data.thetadata_stocks import (
            daily_backfill_callback,
            intraday_backfill_callback_factory,
        )
        orch.register("thetadata_stocks_daily", daily_backfill_callback)
        for interval in ("1m", "5m", "15m", "60m"):
            orch.register(
                f"thetadata_stocks_intraday_{interval}",
                intraday_backfill_callback_factory(interval),
            )
    except Exception:
        logger.exception("thetadata_stocks callbacks not registered")
    try:
        from backend.bot.data.thetadata_iv_history import iv_history_backfill_callback
        orch.register("thetadata_iv_history", iv_history_backfill_callback)
    except Exception:
        logger.exception("thetadata_iv_history callback not registered")
    try:
        # MITS Phase 11.B.2 — per-(ticker, expiry) EOD chain backfill.
        from backend.bot.data.thetadata_options_history import (
            options_eod_backfill_callback,
        )
        orch.register("thetadata_options_eod",
                      options_eod_backfill_callback)
    except Exception:
        logger.exception("thetadata_options_eod callback not registered")
    try:
        from backend.bot.data.fred_expanded import fred_backfill_callback
        orch.register("fred", fred_backfill_callback)
    except Exception:
        logger.exception("fred callback not registered")
    try:
        from backend.bot.data.finnhub_news import (
            finnhub_news_backfill_callback,
            finnhub_news_delta_callback,
        )
        orch.register("finnhub_news", finnhub_news_backfill_callback)
        orch.register("finnhub_news_delta", finnhub_news_delta_callback)
    except Exception:
        logger.exception("finnhub_news callbacks not registered")
    try:
        from backend.bot.data.alphavantage_transcripts import (
            alphavantage_transcripts_backfill_callback,
        )
        orch.register("alphavantage_transcripts",
                       alphavantage_transcripts_backfill_callback)
    except Exception:
        logger.exception("alphavantage_transcripts callback not registered")
    try:
        # MITS Phase 11.1 — free public-source earnings transcript path
        # via SEC 8-K Exhibit 99.1 (the management commentary press
        # release). Replaces the blocked AlphaVantage Premium path.
        from backend.bot.data.sec_earnings_release import (
            sec_earnings_release_backfill_callback,
        )
        orch.register("sec_8k_earnings",
                       sec_earnings_release_backfill_callback)
    except Exception:
        logger.exception("sec_8k_earnings callback not registered")
    try:
        # MITS Phase 11.2 — supplemental pre-2025 news from SEC 8-K
        # Exhibit 99.X. Closes the news-history gap that Finnhub Free
        # can't backfill past ~2 years.
        from backend.bot.data.sec_press_releases import (
            sec_press_releases_backfill_callback,
        )
        orch.register("sec_press_releases",
                       sec_press_releases_backfill_callback)
    except Exception:
        logger.exception("sec_press_releases callback not registered")
    try:
        # MITS Phase 12.3 — env-flag fallback. When ``TB_USE_FINNHUB_FORM4=true``
        # we replace the SEC EDGAR path with Finnhub's parsed-Form-4
        # endpoint, because our EC2 IP is currently rate-limit-banned at
        # data.sec.gov (status=403). Operator can flip the flag back if
        # SEC unblocks. Same source key keeps the schedule + consumers
        # untouched.
        import os as _os
        if (_os.environ.get("TB_USE_FINNHUB_FORM4", "").strip().lower()
                in ("1", "true", "yes", "on")):
            from backend.bot.data.finnhub_form4 import (
                finnhub_form4_backfill_callback,
            )
            orch.register("edgar_form4", finnhub_form4_backfill_callback)
            logger.info(
                "sync_orchestrator: TB_USE_FINNHUB_FORM4=true — "
                "edgar_form4 routed to Finnhub /stock/insider-transactions"
            )
        else:
            from backend.bot.data.edgar_form4 import (
                edgar_form4_backfill_callback,
            )
            orch.register("edgar_form4", edgar_form4_backfill_callback)
    except Exception:
        logger.exception("edgar_form4 callback not registered")
    try:
        # MITS Phase 15.followup.1 — env-flag fallback. When
        # ``TB_USE_13F_INFO=true`` we replace the SEC EDGAR path with
        # 13f.info's per-filing JSON endpoint, because our EC2 IP is
        # currently rate-limit-banned at both ``data.sec.gov`` and
        # ``www.sec.gov`` Archives (status=403). Same source key keeps
        # the schedule + consumers (smart_money, scorecards, analysis
        # composer) untouched.
        import os as _os
        if (_os.environ.get("TB_USE_13F_INFO", "").strip().lower()
                in ("1", "true", "yes", "on")):
            from backend.bot.data.thirteenf_info import (
                thirteenf_info_backfill_callback,
            )
            orch.register("edgar_13f", thirteenf_info_backfill_callback)
            logger.info(
                "sync_orchestrator: TB_USE_13F_INFO=true — edgar_13f "
                "routed to 13f.info /data/13f/* JSON endpoint"
            )
        else:
            from backend.bot.data.edgar_13f import (
                edgar_13f_backfill_callback,
            )
            orch.register("edgar_13f", edgar_13f_backfill_callback)
    except Exception:
        logger.exception("edgar_13f callback not registered")


__all__ = [
    "CallbackResult",
    "SyncOrchestrator",
    "BackfillSummary",
    "get_orchestrator",
]
