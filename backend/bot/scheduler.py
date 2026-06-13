"""APScheduler wiring for pre-market, intraday, and post-market jobs."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger as _CronTrigger

from backend.bot.engine import BotEngine
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# MITS Phase 12.3 — timezone hardening.
#
# We discovered (2026-06-10) that ``AsyncIOScheduler(timezone="America/New_York")``
# alone does NOT propagate the timezone into ``CronTrigger`` instances when
# the trigger is constructed without an explicit ``timezone=`` kwarg under
# certain APScheduler 3.x deployments. The triggers silently fell back to
# the **process** TZ (UTC on our EC2 instance), so every ``hour=N`` cron
# fired 4 hours earlier than intended — e.g. the 17:30 ET delta-sync ran
# at 17:30 UTC = 13:30 ET, BEFORE EOD bars landed on ThetaData, and pulled
# 0 daily rows for the day.
#
# Wrapping CronTrigger here forces every cron in this module to be ET by
# default unless the caller explicitly overrides ``timezone``. ``*/N``
# style intervals are timezone-independent so the wrap is a no-op for the
# 1-minute drain.
def CronTrigger(*args, **kwargs):  # noqa: N802 — matches APScheduler name
    kwargs.setdefault("timezone", "America/New_York")
    return _CronTrigger(*args, **kwargs)

# US market holidays — minimal hard-coded list; expand as needed.
US_MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
}


def is_trading_day(now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    if now.weekday() >= 5:
        return False
    if now.date() in US_MARKET_HOLIDAYS_2026:
        return False
    return True


def most_recent_trading_day(
    today: Optional[date] = None,
    *,
    include_today: bool = False,
) -> date:
    """Return the most recent trading day on or before ``today``.

    MITS Phase 4 (P4.5) — the catch-up job runs on weekends/holidays so
    it needs to resolve "what day did the market last close" without
    pulling pandas_market_calendars.

    ``include_today=True`` returns today when today itself is a
    trading day; ``False`` (default) walks back at least one calendar
    day so the catch-up always considers a CLOSED session.
    """
    d = today or date.today()
    if not include_today:
        d = d - timedelta(days=1)
    for _ in range(10):
        # Weekday + not a holiday → trading day.
        if d.weekday() < 5 and d not in US_MARKET_HOLIDAYS_2026:
            return d
        d = d - timedelta(days=1)
    return d


class BotScheduler:
    def __init__(self, engine: BotEngine, notifier: Optional["object"] = None) -> None:
        self.engine = engine
        # Optional notifier handle so notifier-driven jobs (retry-queue
        # drain, EOD digest) can run without reaching into FastAPI app
        # state. None is fine — those jobs become no-ops.
        self.notifier = notifier
        # misfire_grace_time = 60s tolerates the occasional 2-3s lateness
        # that happens when the engine cycle overlaps the next tick during
        # an LLM call. Without it, APScheduler logs "Run time was missed"
        # warnings into the system warnings buffer.
        self.scheduler = AsyncIOScheduler(
            timezone="America/New_York",
            job_defaults={"misfire_grace_time": 60, "coalesce": True},
        )
        self._configured = False

    def configure(self) -> None:
        if self._configured:
            return
        self.scheduler.add_job(self._pre_market, CronTrigger(day_of_week="mon-fri", hour=8, minute=30))
        self.scheduler.add_job(
            self._intraday,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5"),
        )
        self.scheduler.add_job(
            self._swing_check, CronTrigger(day_of_week="mon-fri", hour=9, minute=35)
        )
        self.scheduler.add_job(
            self._post_market, CronTrigger(day_of_week="mon-fri", hour=16, minute=15)
        )
        self.scheduler.add_job(
            self._gex_history,
            CronTrigger(day_of_week="mon-fri", hour="9-16",
                        minute=f"*/{max(1, TUNABLES.gex_history_interval_min)}"),
        )
        # Stage-14 — capture a system-wide regime fingerprint every
        # ``regime_snapshot_interval_min`` minutes during market hours so the
        # similarity engine has a corpus to search against.
        self.scheduler.add_job(
            self._regime_snapshot,
            CronTrigger(day_of_week="mon-fri", hour="9-15",
                        minute=f"*/{max(1, TUNABLES.regime_snapshot_interval_min)}"),
        )
        # Stage-16 — nightly forward-outcome backfill so regime similarity
        # has trade-outcome stats when callers query.
        self.scheduler.add_job(
            self._regime_backfill,
            CronTrigger(day_of_week="mon-fri", hour=18, minute=0),
        )
        # Stage-16 — daily research digest pushed to the alert center
        # (operator gets "what changed today" without polling).
        self.scheduler.add_job(
            self._research_digest,
            CronTrigger(day_of_week="mon-fri", hour=17, minute=30),
        )
        # Stage-18a — free public data sources. All graceful no-ops
        # when their respective API keys / user-agents are missing.
        self.scheduler.add_job(
            self._fred_refresh,
            CronTrigger(day_of_week="mon-fri", hour=7, minute=0),
        )
        self.scheduler.add_job(
            self._breadth_refresh,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=45),
        )
        self.scheduler.add_job(
            self._edgar_refresh,
            CronTrigger(day_of_week="mon-fri",
                          hour=f"*/{max(1, TUNABLES.edgar_refresh_interval_hours)}",
                          minute=10),
        )
        # Stage-18b — FINRA daily short-volume + CFTC weekly COT.
        self.scheduler.add_job(
            self._finra_refresh,
            CronTrigger(day_of_week="tue-sat", hour=4, minute=0),
        )
        self.scheduler.add_job(
            self._cot_refresh,
            CronTrigger(day_of_week="sat", hour=6, minute=0),
        )
        # P1.3-FU3 — daily IV gap-filler. Walks the scan universe and
        # tops up any (ticker, date) pairs missing from iv_history in the
        # last 30 days. Runs after the EOD straddle is available (~5pm ET).
        self.scheduler.add_job(
            self._iv_history_gap_fill,
            CronTrigger(day_of_week="mon-fri", hour=17, minute=0),
        )
        # MITS Phase 11.G — nightly delta-sync pass. 17:30 ET weekdays
        # so it runs after the iv_history gap-filler and before the
        # 19:00 nightly outcome link. Pulls the gap between the
        # watermark and today for every (source, ticker) pair across
        # the universe + the 50-series FRED panel. Idempotent on
        # already-synced rows so a re-run within minutes is a no-op.
        self.scheduler.add_job(
            self._delta_sync_pass,
            CronTrigger(day_of_week="mon-fri", hour=17, minute=30),
        )
        # MITS Phase 0 — nightly outcome linker + knowledge-graph
        # recomputation, plus weekly full replay of the watchlist +
        # ETF benchmarks. All three are idempotent.
        self.scheduler.add_job(
            self._nightly_outcome_link,
            CronTrigger(day_of_week="mon-fri", hour=19, minute=0),
        )
        self.scheduler.add_job(
            self._nightly_recompute_cells,
            CronTrigger(day_of_week="mon-fri", hour=19, minute=30),
        )
        self.scheduler.add_job(
            self._weekly_full_replay,
            CronTrigger(day_of_week="sat", hour=6, minute=0),
        )
        # MITS Phase 1 — nightly snapshot of current knowledge_graph
        # cells into knowledge_graph_history. Weekdays + Sunday so we
        # have a fresh point for the sparkline even if no trading
        # happens that day. Idempotent on (cohort, snapshot_date).
        self.scheduler.add_job(
            self._nightly_snapshot_cells,
            CronTrigger(day_of_week="mon-fri,sun", hour=23, minute=50),
        )
        # MITS Phase 11.I — per-source health aggregator at 00:01 ET
        # daily. Walks backfill_progress + data_watermarks and computes
        # a one-row-per-source health snapshot. Operator UI (Agent 5)
        # reads `data_source_health` to render the 9-source grid.
        self.scheduler.add_job(
            self._data_source_health_pass,
            CronTrigger(hour=0, minute=1),
        )
        # MITS Phase 11.I — nightly corpus-replay (Agent 5 wire-up).
        # Runs at 03:00 ET after EOD catchup + outcome linking +
        # vector-indexing windows. Keeps the corpus current with the
        # day's new bars + fires detectors against fresh data.
        self.scheduler.add_job(
            self._corpus_replay_pass,
            CronTrigger(hour=3, minute=0),
        )
        # MITS Phase 11.I — nightly parity audit at 17:45 ET (after
        # EOD bar lands at 16:30). TODO sub-item from Agent 4 brief.
        self.scheduler.add_job(
            self._parity_audit_pass,
            CronTrigger(day_of_week="mon-fri", hour=17, minute=45),
        )
        # MITS Phase 11.1 — nightly bronze ferry at 04:00 ET. Walks the
        # ferry watermark, picks up new rows since the last run, and
        # writes 50k-row parquet batches to s3://.../bronze/sqlite_ferry/.
        # Capped at TUNABLES.bronze_ferry_delta_max_batches per table per
        # run so a runaway backfill doesn't blow the ferry over to a
        # multi-hour run.
        self.scheduler.add_job(
            self._bronze_ferry_pass,
            CronTrigger(hour=4, minute=0),
        )
        # MITS Phase 11.1 — nightly embed-new-rows at 04:30 ET. Walks
        # every row added since the last vector-index watermark and
        # upserts into pgvector. Runs AFTER the bronze ferry so any
        # rows that landed overnight are durably persisted to S3 first.
        self.scheduler.add_job(
            self._embed_new_rows_pass,
            CronTrigger(hour=4, minute=30),
        )
        # Telegram retry-queue drain — pulls any queued messages off
        # the persistent outbox and re-tries them. Cron second-fields
        # are capped at 59, so we clamp the configured interval and
        # express it in seconds (default 60s = once per minute via
        # `second=0`). Interval ≥ 60 → wire as `minute="*"`.
        drain_interval = max(
            5, int(getattr(TUNABLES, "telegram_drain_interval_sec", 60))
        )
        if drain_interval >= 60:
            # Once per minute (or every N minutes if > 60).
            minute_step = max(1, drain_interval // 60)
            self.scheduler.add_job(
                self._telegram_drain_queue,
                CronTrigger(minute=f"*/{minute_step}"),
            )
        else:
            self.scheduler.add_job(
                self._telegram_drain_queue,
                CronTrigger(second=f"*/{drain_interval}"),
            )
        # EOD digest — 16:30 ET weekdays. Composes the day's
        # summary + sends via the notifier.
        self.scheduler.add_job(
            self._telegram_eod_digest,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=30),
        )
        # MITS Phase 3 — End-of-Day pattern analysis pass. Walks the
        # watchlist + ETF benchmarks, runs every detector, queries the
        # corpus per pattern, composes per-ticker theses, and persists
        # them to `eod_analysis` for the Tomorrow's Setup UI.
        self.scheduler.add_job(
            self._eod_analysis_pass,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=30),
        )
        # Tomorrow's Setup Telegram digest — fires 5 min after the EOD
        # pass so the rows are populated. Graceful no-op when no
        # Telegram credentials are configured.
        self.scheduler.add_job(
            self._telegram_tomorrow_setup,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=35),
        )
        # MITS Phase 4 (P4.5) — EOD catch-up passes. If the weekday
        # 16:30 ET pass failed (service restart, hiccup), the Sunday
        # 10:00 ET catch-up runs the pass for the most recent trading
        # day. The Monday 06:00 ET catch-up handles post-holiday cases
        # (e.g. Memorial Day Monday → Tuesday morning).
        self.scheduler.add_job(
            self._eod_catchup_pass,
            CronTrigger(day_of_week="sun", hour=10, minute=0),
        )
        self.scheduler.add_job(
            self._eod_catchup_pass,
            CronTrigger(day_of_week="mon", hour=6, minute=0),
        )
        # MITS Phase 5 (P5.2) — nightly prediction→outcome reconcile at
        # 17:00 ET. By then the 16:30 EOD pass has populated EodAnalysis
        # and any same-day intraday trades are closed. The reconcile
        # walks the day's analysis rows and writes EodPredictionOutcome
        # rows with their final outcome (traded_matched / diverged /
        # not_traded + skip_reason / pending). Idempotent on re-runs.
        self.scheduler.add_job(
            self._eod_prediction_reconcile,
            CronTrigger(day_of_week="mon-fri", hour=17, minute=0),
        )
        # MITS Phase 6 (P6.1) — nightly live-outcome ingest at 23:40
        # ET. Converts every closed Trade since the last run into a
        # high-weight MarketObservation + MarketOutcome pair so the
        # knowledge graph reflects live performance. Runs BEFORE the
        # 23:50 ET snapshot job so the snapshot captures the
        # newly-recomputed cells. Idempotent via IngestWatermark.
        self.scheduler.add_job(
            self._ingest_live_outcomes,
            CronTrigger(day_of_week="mon-fri,sun", hour=23, minute=40),
        )
        # MITS Phase 6 (P6.3) — nightly self-disabling-detector
        # suggestion pass at 23:55 ET. Walks every detector's
        # out-of-sample posterior and creates DetectorSuggestion rows
        # when posteriors trip the disable/re-enable thresholds.
        self.scheduler.add_job(
            self._detector_suggestions_pass,
            CronTrigger(day_of_week="mon-fri,sun", hour=23, minute=55),
        )
        # MITS Phase 14.D — nightly BrainPrediction → Trade linker.
        # Walks pending BrainPrediction rows, ties each to a matching
        # Trade, replays bars to evaluate invalidation triggers, and
        # resolves win/loss/scratch/not_traded.
        self.scheduler.add_job(
            self._brain_prediction_link,
            CronTrigger(day_of_week="mon-fri,sun", hour=23, minute=45),
        )
        # MITS Phase 18-FU Stream A — nightly Decision Funnel daily
        # rollup. 21:55 ET, BEFORE the 22:00 18.A attribution job so
        # 18.A can read fresh funnel context. Captures the prior day's
        # 10-stage funnel + confidence histogram + cooldown audit +
        # counterfactual histogram into ``decision_funnel_daily``
        # keyed on yesterday's date. Always persists — not gated by
        # any advisory flag. Investigation-only; no thresholds changed.
        self.scheduler.add_job(
            self._decision_funnel_daily_pass,
            CronTrigger(hour=21, minute=55),
        )
        # MITS Phase 18.A — nightly Learned Hypothesis Attribution pass.
        # 22:00 ET, AFTER the EOD digest + EOD analysis pass + prediction
        # reconcile have run (so the day's newly-closed trades show up
        # in the calibration). Runs 7 days a week so weekend recomputes
        # absorb late-binding outcomes. Idempotent: each run appends a
        # fresh batch keyed by computed_at; the GET endpoints always
        # serve the most recent snapshot. Min-N guardrail (default 30 /
        # 30 / 10 for agent / axis / strategy) gates noisy reads.
        self.scheduler.add_job(
            self._learned_attribution_pass,
            CronTrigger(hour=22, minute=0),
        )
        # MITS Phase 18.C — nightly Policy Auto-Tuning advisory pass.
        # 22:30 ET, AFTER 18.A so the rule-tuning recommendation reads
        # the same closed-decision window 18.A just snapshot-aggregated.
        # Gated on ``TUNABLES.policy_tuning_advisory_enabled`` (default
        # OFF) — the operator opts in after seeing the first batch via
        # the on-demand recompute route. Even when ON, the output is
        # ADVISORY: rows are written to policy_tunings, never written
        # back to TUNABLES. Runs 7 days a week so weekend recomputes
        # absorb late-binding outcomes.
        self.scheduler.add_job(
            self._policy_tuning_pass,
            CronTrigger(hour=22, minute=30),
        )
        # MITS Phase 18.D — nightly Online Agent Weight Adaptation
        # advisory pass. 22:45 ET, AFTER 18.A (22:00) and 18.C (22:30)
        # so the weight advisor reads the same calibration scorecard
        # the attribution pass just snapshot-aggregated. Gated on
        # ``TUNABLES.adaptive_weights_advisory_enabled`` (default OFF).
        # Even when ON the output is ADVISORY — apply requires a
        # SEPARATE flag (``adaptive_weights_apply_enabled``). Runs 7
        # days a week so weekend recomputes absorb late-binding
        # outcomes.
        self.scheduler.add_job(
            self._weight_adaptation_pass,
            CronTrigger(hour=22, minute=45),
        )
        # MITS Phase 6 (P6.4) — Sunday 11:00 ET weekly retrospective
        # pass. Assembles the prior week's recap into a
        # WeeklyRetrospective row + composes the narrative.
        self.scheduler.add_job(
            self._weekly_retrospective_pass,
            CronTrigger(day_of_week="sun", hour=11, minute=0),
        )
        # MITS Phase 8.3 — silver normalization pass. Hourly during
        # market hours, daily off-hours. Idempotent: replays the
        # bronze partition for the current dt key and rewrites silver.
        self.scheduler.add_job(
            self._normalize_silver_pass,
            CronTrigger(day_of_week="mon-fri", hour="9-16", minute=15),
        )
        self.scheduler.add_job(
            self._normalize_silver_pass,
            CronTrigger(day_of_week="*", hour=22, minute=0),
        )
        # MITS Phase 8.4 — nightly gold-layer snapshot. Default
        # 23:30 ET; configurable via lake_gold_snapshot_hour_et /
        # lake_gold_snapshot_minute_et.
        self.scheduler.add_job(
            self._gold_snapshot_pass,
            CronTrigger(
                day_of_week="*",
                hour=int(TUNABLES.lake_gold_snapshot_hour_et),
                minute=int(TUNABLES.lake_gold_snapshot_minute_et),
            ),
        )
        # MITS Phase 8.5 — vector indexing pass every N minutes.
        self.scheduler.add_job(
            self._vector_indexing_pass,
            CronTrigger(
                minute=f"*/{max(1, int(TUNABLES.vector_indexing_interval_min))}"
            ),
        )
        # MITS Phase 8.2 — Cboe put/call ratio (~16:45 ET).
        self.scheduler.add_job(
            self._cboe_pcr_refresh,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=50),
        )
        # MITS Phase 9.5 — hourly lake-health monitor. Read-only sample
        # of bronze/gold/vector layers; writes LakeHealthAlert rows
        # when thresholds trip and auto-resolves cleared alerts. Runs
        # every day (lake objects can fail to land even outside market
        # hours).
        self.scheduler.add_job(
            self._lake_health_check,
            CronTrigger(minute=7),  # 07 past the hour avoids exactly-on-the-hour clusters.
        )
        self._configured = True

    def start(self) -> None:
        self.configure()
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # -- jobs ---------------------------------------------------------------
    def _pre_market(self) -> None:
        if not is_trading_day():
            return
        logger.info("pre-market scan")
        self.engine.run_cycle()

    def _intraday(self) -> None:
        if not is_trading_day():
            return
        if not self.engine.status.running:
            return
        self.engine.run_cycle()

    def _swing_check(self) -> None:
        if not is_trading_day():
            return
        logger.info("swing trade check")
        self.engine.run_cycle()

    def _lake_health_check(self) -> None:
        """MITS Phase 9.5 — hourly lake-health sample. Writes/resolves
        ``LakeHealthAlert`` rows. Safe no-op when AWS / pgvector are
        unreachable (the underlying ``stat_layer`` calls degrade
        gracefully)."""
        try:
            from backend.bot.monitoring.lake_health import run_health_check
            result = run_health_check()
            if result.fired or result.auto_resolved:
                logger.info(
                    "lake-health: fired=%s auto_resolved=%s samples=%s",
                    [a["kind"] for a in result.fired],
                    result.auto_resolved,
                    result.samples,
                )
        except Exception:
            logger.debug("_lake_health_check failed", exc_info=True)

    def _post_market(self) -> None:
        if not is_trading_day():
            return
        logger.info(
            "EOD summary: pnl=%s cycles=%s",
            self.engine.status.daily_pnl,
            self.engine.status.cycles,
        )
        self.engine.status.cycles = 0
        # CRITICAL: zero today's realized P&L tracker so tomorrow's
        # circuit breaker starts fresh. Without this, ``daily_pnl``
        # accumulates across days and a few red sessions would leave
        # the breaker permanently tripped on otherwise-green days.
        self.engine.status.daily_pnl = 0.0
        # MITS Phase 5 (P5.3) — zero the EOD-bias daily caps so the
        # next session starts with full notional + concurrent-position
        # budgets.
        self.engine._eod_high_conviction_open_today = 0
        self.engine._eod_daily_notional_today = 0.0
        # MITS Phase 7 — zero the opportunistic daily tallies so the
        # discretionary layer starts the next session with a fresh
        # notional + concurrent-position budget.
        try:
            self.engine._opportunistic_daily_notional = 0.0
            self.engine._opportunistic_concurrent_open = 0
        except AttributeError:
            pass

    def _regime_backfill(self) -> None:
        """Stage-16 — nightly forward-outcome backfill for regime snapshots."""
        if not is_trading_day():
            return
        try:
            from backend.bot.regime_similarity.backfill import backfill_forward_outcomes
            stats = backfill_forward_outcomes()
            logger.info("regime backfill: %s", stats)
        except Exception:
            logger.debug("regime_backfill job failed", exc_info=True)

    def _research_digest(self) -> None:
        """Stage-16 — daily research digest pushed to the alert center
        so operators see "what changed today" without polling endpoints."""
        if not is_trading_day():
            return
        try:
            from backend.bot.alerts import ALERT_CENTER, Alert
            from backend.bot.research import generate_digest
            digest = generate_digest()
            findings = digest.findings
            if not findings:
                return
            # Translate research severities (info/warn/alert) into alert-center
            # severities (info/success/warning/danger).
            sev_map = {"info": "info", "warn": "warning", "alert": "danger"}
            highest = sev_map.get(
                max((f.severity for f in findings),
                      key=lambda s: ("alert", "warn", "info").index(s)),
                "info",
            )
            counts = digest.counts
            ALERT_CENTER.fire(Alert(
                title=(f"Research digest: {counts.get('alert', 0)} alerts, "
                          f"{counts.get('warn', 0)} warnings, "
                          f"{counts.get('info', 0)} info"),
                body="; ".join(f.title for f in findings[:5]),
                severity=highest, category="ai",
                meta={"counts": counts,
                        "findings": [f.to_dict() for f in findings]},
            ))
        except Exception:
            logger.debug("research_digest job failed", exc_info=True)

    def _fred_refresh(self) -> None:
        """Stage-18a — daily FRED macro panel pull."""
        if not is_trading_day():
            return
        try:
            from backend.bot.data.fred import refresh
            logger.info("fred refresh: %s", refresh())
        except Exception:
            logger.debug("fred_refresh job failed", exc_info=True)

    def _breadth_refresh(self) -> None:
        """Stage-18a — end-of-day market breadth snapshot."""
        if not is_trading_day():
            return
        try:
            from backend.bot.breadth import refresh
            logger.info("breadth refresh: %s", refresh())
        except Exception:
            logger.debug("breadth_refresh job failed", exc_info=True)

    def _finra_refresh(self) -> None:
        """Stage-18b — daily FINRA short-volume pull, filtered to watchlist."""
        try:
            from backend.bot.data.finra import refresh
            from backend.db import session_scope
            from backend.models.config import load_config
            with session_scope() as session:
                tickers = (load_config(session).get("tickers") or [])[:50]
            stats = refresh(tickers=tickers)
            logger.info("finra refresh: %s", stats)
        except Exception:
            logger.debug("finra_refresh job failed", exc_info=True)

    def _cot_refresh(self) -> None:
        """Stage-18b — weekly CFTC COT pull (Saturday morning)."""
        try:
            from backend.bot.data.cot import refresh
            logger.info("cot refresh: %s", refresh())
        except Exception:
            logger.debug("cot_refresh job failed", exc_info=True)

    def _edgar_refresh(self) -> None:
        """Stage-18a — periodic SEC EDGAR filings pull for the watchlist."""
        if not is_trading_day():
            return
        try:
            from backend.bot.data.edgar import refresh_universe
            from backend.db import session_scope
            from backend.models.config import load_config
            with session_scope() as session:
                tickers = (load_config(session).get("tickers") or [])[:20]
            if tickers:
                stats = refresh_universe(tickers, limit_per_ticker=10)
                logger.info("edgar refresh: total_inserted=%s",
                              stats.get("total_inserted"))
        except Exception:
            logger.debug("edgar_refresh job failed", exc_info=True)

    def _regime_snapshot(self) -> None:
        """Persist a system-wide regime fingerprint. Reads the latest
        ``MarketState`` (engine writes one per cycle) and snapshots it to
        the ``regime_episode_snapshots`` table. Best-effort: skips when
        there's no recent MarketState (e.g. engine hasn't run yet today)."""
        if not is_trading_day():
            return
        try:
            from backend.bot.regime_similarity import snapshot_current
            from backend.bot.state import get_latest

            state = get_latest()
            if state is None:
                return
            snapshot_current(state)
        except Exception:
            logger.debug("regime_snapshot job failed", exc_info=True)

    def _iv_history_gap_fill(self) -> None:
        """P1.3-FU3 — fill missing (ticker, date) cells in iv_history for
        the scan universe over the last 30 days. ``backfill()`` is
        idempotent on existing rows so calling it daily is cheap when
        nothing is missing."""
        if not is_trading_day():
            return
        try:
            from backend.bot.data.iv_history import backfill
            from backend.db import session_scope
            from backend.models.config import load_config
            from backend.models.watchlist import WatchlistItem
            with session_scope() as session:
                cfg_tickers = [t.upper().strip() for t in
                                  (load_config(session).get("tickers") or []) if t]
                wl_tickers = [w.ticker.upper().strip()
                                for w in session.query(WatchlistItem).all()
                                if w.ticker and w.ticker.strip()]
            seen: set = set()
            tickers: list = []
            for t in cfg_tickers + wl_tickers:
                if t and t not in seen:
                    seen.add(t)
                    tickers.append(t)
            totals = {"inserted": 0, "skipped": 0, "errors": 0}
            for tk in tickers:
                try:
                    stats = backfill(tk, lookback_days=30, pace_seconds=0.02)
                    for key in totals:
                        totals[key] += stats.get(key, 0)
                except Exception:
                    totals["errors"] += 1
                    logger.debug("iv_history gap-fill failed for %s",
                                       tk, exc_info=True)
            logger.info("iv_history daily gap-fill: %s", totals)
        except Exception:
            logger.debug("iv_history gap-fill job failed", exc_info=True)

    def _delta_sync_pass(self) -> None:
        """MITS Phase 11.G — nightly delta sync across the universe.

        Pulls the gap between each (source, ticker) watermark and today
        for every Phase 11 backfill source:

          * thetadata_stocks_daily — 1d bars
          * thetadata_stocks_intraday_5m — 5-minute bars
          * thetadata_iv_history — ATM IV per date
          * fred — 50-series macro panel
          * finnhub_news — company-news + FinBERT sentiment
          * alphavantage_transcripts — earnings-call transcripts
          * edgar_form4 — insider transactions
          * edgar_13f — institutional holdings (per fund CIK)

        Bounded by the SyncOrchestrator's per-source rate limit so a
        nightly miss never floods the upstream vendor. Idempotent on
        already-current pairs. Per-source try/except keeps a single
        broken vendor from killing the rest of the nightly pass.
        """
        if not is_trading_day():
            return
        try:
            from backend.bot.data.sync_orchestrator import get_orchestrator

            orch = get_orchestrator()
            totals: dict = {}

            def _run(source: str, *, tickers=None) -> None:
                try:
                    results = orch.run_all_delta([source], tickers=tickers)
                    summaries = results.get(source, [])
                    totals[source] = {
                        "tickers": len(summaries),
                        "rows_written": sum(s.rows_written for s in summaries),
                        "error_chunks": sum(s.error_chunks for s in summaries),
                    }
                except Exception:
                    logger.debug(
                        "delta_sync_pass: source=%s crashed",
                        source, exc_info=True,
                    )
                    totals[source] = {"error": "crashed"}

            # Universe-driven (40 tickers).
            for source in (
                "thetadata_stocks_daily",
                "thetadata_stocks_intraday_5m",
                "thetadata_iv_history",
                "finnhub_news",
                "alphavantage_transcripts",
                "edgar_form4",
            ):
                _run(source)

            # FRED — series_ids drive the loop, not the universe.
            _run("fred")

            # 13F — fund CIKs drive the loop.
            try:
                from backend.bot.data.watched_funds import watched_fund_ciks
                ciks = list(watched_fund_ciks())
            except Exception:
                ciks = []
            if ciks:
                _run("edgar_13f", tickers=ciks)

            logger.info("delta_sync pass: %s", totals)
        except Exception:
            logger.debug("delta_sync_pass job failed", exc_info=True)

    def _nightly_outcome_link(self) -> None:
        """MITS Phase 0 — nightly outcome linker across the corpus."""
        if not is_trading_day():
            return
        try:
            from backend.bot.corpus.outcome_linker import link_outcomes_batch
            stats = link_outcomes_batch()
            logger.info("nightly outcome link: %s", stats)
        except Exception:
            logger.debug("nightly outcome link failed", exc_info=True)

    def _nightly_recompute_cells(self) -> None:
        """MITS Phase 0 — fold (obs + outcomes) into knowledge_graph cells."""
        if not is_trading_day():
            return
        try:
            from backend.bot.corpus.knowledge_aggregator import recompute_cells
            stats = recompute_cells()
            logger.info("nightly recompute cells: %s", stats)
        except Exception:
            logger.debug("nightly recompute cells failed", exc_info=True)

    def _nightly_snapshot_cells(self) -> None:
        """MITS Phase 1 — snapshot today's knowledge_graph cells into the
        history table so the UI drill-down can render a real sparkline.
        Idempotent on (cohort, snapshot_date)."""
        try:
            from backend.bot.corpus.knowledge_aggregator import (
                snapshot_cells_to_history,
            )
            stats = snapshot_cells_to_history()
            logger.info("nightly snapshot to history: %s", stats)
        except Exception:
            logger.debug("nightly snapshot to history failed", exc_info=True)

    def _data_source_health_pass(self) -> None:
        """MITS Phase 11.I — daily per-source health aggregator.

        Walks the rolling-24h `backfill_progress` ledger for every
        Phase 11 source family and writes a one-row-per-source
        snapshot to `data_source_health`. Status: green / yellow /
        red so Agent 5's UI can render a 9-source grid.

        Runs at 00:01 ET so it sees the prior trading day's complete
        backfill activity.
        """
        try:
            from backend.bot.monitoring.source_health import run_pass
            stats = run_pass()
            logger.info("data_source_health pass: %s", stats)
        except Exception:
            logger.debug("data_source_health pass failed", exc_info=True)

    def _corpus_replay_pass(self) -> None:
        """MITS Phase 11.I — nightly incremental detector-replay pass.

        Runs at 03:00 ET (after EOD catchup at 16:30, outcome reconcile
        at 17:00, live-outcome ingest at 23:40, and the source-health
        pass at 00:01). Walks the silver-layer `stock_bars` corpus for
        the last 2 trading days × the universe, re-runs detectors,
        re-runs outcome_linker on any new observations, then
        recomputes affected knowledge_graph cells and snapshots the
        history table for sparkline freshness.

        MITS Phase 12.3 — widened the replay window to 60 days. The
        prior 3-day window broke when the silver layer had <30 daily
        bars between start_date and end_date because
        :func:`replay_ticker` enforces ``daily_min_bars=30`` so the
        rolling detectors have meaningful context. The 60-day window
        always satisfies that floor while the per-row INSERT OR IGNORE
        dedup in :func:`_persist_observation` keeps the marginal cost
        of re-running observed days at ~O(seek). The replay also now
        advances the ``detector_replay`` watermark to the MAX bar date
        actually consumed so the next pass picks up cleanly.

        Idempotent — re-running just inserts 0 rows. Fail-open: any
        error in any sub-step logs but never crashes the scheduler.
        """
        from datetime import date as _date, timedelta as _td
        try:
            from backend.bot.corpus.replay_from_silver import replay_universe
            from backend.bot.data.universe import load_universe as canonical_tickers
            end = _date.today()
            # Widened to 60 days so the replay always sees enough bars
            # for the 30-bar rolling detectors. Operator can override
            # via TUNABLES.corpus_replay_lookback_days (default 60).
            lookback = int(getattr(
                TUNABLES, "corpus_replay_lookback_days", 60))
            start = end - _td(days=max(30, lookback))
            tickers = list(canonical_tickers())
            stats = replay_universe(
                tickers, start_date=start, end_date=end)
            logger.info("corpus_replay_pass [%s..%s] %d tickers: %s",
                                start, end, len(tickers), stats)
            # Phase 12.3 — explicit watermark advancement. Walks the
            # max(bar_ts) per ticker and writes detector_replay → that
            # date so downstream monitors can show "detector caught up".
            self._advance_detector_replay_watermarks(tickers, end)
        except Exception:
            logger.debug("corpus replay pass (silver) failed",
                                exc_info=True)
        try:
            from backend.bot.corpus.outcome_linker import (
                link_outcomes_batch,
            )
            link_outcomes_batch()
        except Exception:
            logger.debug("corpus replay outcome_linker failed",
                                exc_info=True)
        try:
            from backend.bot.corpus.knowledge_aggregator import (
                recompute_cells, snapshot_cells_to_history,
            )
            recompute_cells()
            snapshot_cells_to_history()
        except Exception:
            logger.debug(
                "corpus replay knowledge_aggregator failed",
                exc_info=True,
            )

    def _advance_detector_replay_watermarks(self, tickers, end_date) -> None:
        """Phase 12.3 — write the per-ticker high-water-mark for the
        ``detector_replay`` source. Walks ``stock_bars`` to find the
        actual MAX(bar_ts) date that was usable for each ticker (so the
        watermark reflects truth, not aspiration). Idempotent.
        """
        try:
            from sqlalchemy import select, func
            from backend.db import session_scope
            from backend.models.data_watermark import DataWatermark
            from backend.models.stock_bar import StockBar
            with session_scope() as s:
                for tk in tickers:
                    try:
                        max_dt = s.execute(
                            select(func.max(StockBar.bar_ts))
                            .where(StockBar.ticker == tk)
                            .where(StockBar.interval == "1d")
                        ).scalar()
                        if max_dt is None:
                            continue
                        wm_date = max_dt.date() if hasattr(max_dt, "date") else max_dt
                        wm_str = wm_date.isoformat()
                        row = s.execute(
                            select(DataWatermark)
                            .where(DataWatermark.source == "detector_replay")
                            .where(DataWatermark.ticker == tk)
                        ).scalar_one_or_none()
                        if row is None:
                            row = DataWatermark(
                                source="detector_replay",
                                ticker=tk,
                                last_synced_through_date=wm_str,
                            )
                            s.add(row)
                        elif (row.last_synced_through_date or "") < wm_str:
                            row.last_synced_through_date = wm_str
                    except Exception:
                        logger.debug(
                            "advance_detector_replay_watermarks: %s failed",
                            tk, exc_info=True,
                        )
        except Exception:
            logger.debug("advance_detector_replay_watermarks failed",
                                exc_info=True)

    def _bronze_ferry_pass(self) -> None:
        """MITS Phase 11.1 — nightly bronze ferry delta pass at 04:00 ET.

        Forks ``bin/bronze_ferry.py --mode delta`` so the heavy parquet
        serialization + S3 PUT loop doesn't blow up the scheduler
        process. Capped via ``TUNABLES.bronze_ferry_delta_max_batches``
        so a single nightly run can't loop forever on a fresh backfill
        landing 10M rows in a day.
        """
        import subprocess
        import sys
        try:
            # Memory pressure check first — if YELLOW, sleep + retry;
            # if still RED, skip this run and let tomorrow pick it up
            # from the watermark.
            from backend.bot.data.memory_guard import (
                memory_status, wait_until_ok,
            )
            status = memory_status()
            if not status.ok:
                logger.warning(
                    "bronze_ferry_pass: memory pressure %.1f%% — "
                    "waiting up to 5min", status.percent,
                )
                if not wait_until_ok(max_seconds=300, sleep_seconds=30):
                    logger.warning(
                        "bronze_ferry_pass: memory still high — "
                        "skipping this run (watermark protects)."
                    )
                    return
        except Exception:
            logger.debug("memory_guard probe failed", exc_info=True)
        try:
            from pathlib import Path
            repo_root = Path(__file__).resolve().parent.parent.parent
            ferry_bin = repo_root / "bin" / "bronze_ferry.py"
            max_batches = int(getattr(
                TUNABLES, "bronze_ferry_delta_max_batches", 20))
            batch_size = int(getattr(
                TUNABLES, "bronze_ferry_batch_size", 50000))
            cmd = [
                sys.executable, str(ferry_bin),
                "--mode", "delta",
                "--delta-max-batches", str(max_batches),
                "--batch-size", str(batch_size),
            ]
            logger.info("bronze_ferry_pass starting cmd=%s", cmd)
            proc = subprocess.run(cmd, check=False, capture_output=True,
                                       text=True, timeout=3600)
            logger.info("bronze_ferry_pass exit_rc=%d stdout_tail=%s",
                              proc.returncode,
                              (proc.stdout or "")[-2000:])
            if proc.returncode != 0:
                logger.warning(
                    "bronze_ferry_pass non-zero exit — stderr_tail=%s",
                    (proc.stderr or "")[-2000:],
                )
        except subprocess.TimeoutExpired:
            logger.error("bronze_ferry_pass timed out at 1h cap")
        except Exception:
            logger.exception("bronze_ferry_pass crashed")

    def _embed_new_rows_pass(self) -> None:
        """MITS Phase 11.1 — nightly embed-new-rows pass at 04:30 ET.

        Forks ``bin/embed_namespace.py --namespace all`` so the
        sentence-transformer model load + 5-namespace walk doesn't
        bloat the scheduler process. The runner is memory-guard aware
        and bails cleanly when pressure exceeds the threshold.
        """
        import subprocess
        import sys
        try:
            from backend.bot.data.memory_guard import (
                memory_status, wait_until_ok,
            )
            status = memory_status()
            if not status.ok:
                logger.warning(
                    "embed_new_rows_pass: memory pressure %.1f%% — "
                    "waiting up to 5min", status.percent,
                )
                if not wait_until_ok(max_seconds=300, sleep_seconds=30):
                    logger.warning(
                        "embed_new_rows_pass: memory still high — "
                        "skipping this run (vector_index_state "
                        "protects partial progress)."
                    )
                    return
        except Exception:
            logger.debug("memory_guard probe failed", exc_info=True)
        try:
            from pathlib import Path
            repo_root = Path(__file__).resolve().parent.parent.parent
            embed_bin = repo_root / "bin" / "embed_namespace.py"
            batch_size = int(getattr(TUNABLES, "embed_batch_size", 1000))
            cmd = [
                sys.executable, str(embed_bin),
                "--namespace", "all",
                "--batch-size", str(batch_size),
            ]
            logger.info("embed_new_rows_pass starting cmd=%s", cmd)
            proc = subprocess.run(cmd, check=False, capture_output=True,
                                       text=True, timeout=3600)
            logger.info(
                "embed_new_rows_pass exit_rc=%d stdout_tail=%s",
                proc.returncode, (proc.stdout or "")[-2000:],
            )
            if proc.returncode != 0:
                logger.warning(
                    "embed_new_rows_pass non-zero exit — stderr_tail=%s",
                    (proc.stderr or "")[-2000:],
                )
        except subprocess.TimeoutExpired:
            logger.error("embed_new_rows_pass timed out at 1h cap")
        except Exception:
            logger.exception("embed_new_rows_pass crashed")

    def _parity_audit_pass(self) -> None:
        """MITS Phase 11.I — nightly cross-vendor parity audit at 17:45
        ET. Walks today's bars across the universe, compares yfinance
        vs ThetaData close, writes parity_audit_history + flags
        market_observations.parity_warn on suspect days.

        TODO sub-item from Agent 4 brief: this wires the deferred
        daily parity_audit cron.
        """
        from datetime import date as _date, timedelta as _td
        try:
            from backend.bot.corpus.parity_audit import audit_universe
            from backend.bot.data.universe import load_universe as canonical_tickers
            end = _date.today()
            start = end - _td(days=int(getattr(
                TUNABLES, "parity_audit_lookback_days", 2)))
            tickers = list(canonical_tickers())
            stats = audit_universe(
                tickers, start_date=start, end_date=end)
            logger.info("parity_audit_pass [%s..%s] %d tickers: %s",
                                start, end, len(tickers), stats)
        except Exception:
            logger.debug("parity_audit pass failed", exc_info=True)

    def _weekly_full_replay(self) -> None:
        """MITS Phase 0 — weekend full corpus refresh: iterate the
        watchlist + canonical ETF benchmarks, re-run ``bootstrap_ticker``
        for each. Daily bars accumulate week-over-week so this picks up
        the new week's bars and fires any new detector signals.
        """
        try:
            from backend.bot.corpus.historical_replay import bootstrap_ticker
            from backend.db import session_scope
            from backend.models.watchlist import WatchlistItem
            tickers: list[str] = []
            with session_scope() as session:
                tickers.extend(
                    w.ticker.upper().strip()
                    for w in session.query(WatchlistItem).all()
                    if w.ticker and w.ticker.strip()
                )
            # ETF benchmarks always covered, regardless of watchlist.
            for bench in ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE"):
                if bench not in tickers:
                    tickers.append(bench)
            for tk in tickers:
                try:
                    bootstrap_ticker(tk)
                except Exception:
                    logger.debug("weekly replay failed for %s", tk,
                                       exc_info=True)
            logger.info("weekly full replay across %d tickers", len(tickers))
        except Exception:
            logger.debug("weekly full replay failed", exc_info=True)

    def _telegram_drain_queue(self) -> None:
        """Drain queued Telegram messages + sweep permanently-failed rows.

        Runs every ``telegram_drain_interval_sec`` (default 60s). Safe
        no-op when there's no notifier or no messages queued.
        """
        if self.notifier is None:
            return
        try:
            max_attempts = int(
                getattr(TUNABLES, "telegram_max_attempts", 5)
            )
            stats = self.notifier.drain_queue(max_attempts=max_attempts)
            if (stats.get("delivered", 0) or stats.get("rescheduled", 0)
                  or stats.get("swept", 0)):
                logger.info("telegram drain: %s", stats)
        except Exception:
            logger.debug("telegram drain failed", exc_info=True)

    def _telegram_eod_digest(self) -> None:
        """16:30 ET weekday EOD digest. Operator-facing."""
        if not is_trading_day():
            return
        if self.notifier is None or not self.notifier.enabled:
            return
        try:
            from backend.bot.notifications.digest import build_eod_digest
            text = build_eod_digest()
            self.notifier.send_text(text, bypass_filters=True)
            logger.info("telegram EOD digest sent (%d chars)", len(text))
        except Exception:
            logger.debug("telegram EOD digest failed", exc_info=True)

    def _eod_analysis_pass(self) -> None:
        """MITS Phase 3 — run the per-ticker EOD analysis pass after
        the close. Idempotent: re-running the same day overwrites rows.
        """
        if not is_trading_day():
            return
        try:
            from backend.bot.eod_analysis import run_eod_pass
            stats = run_eod_pass()
            logger.info("eod analysis pass: %s", stats)
        except Exception:
            logger.debug("eod analysis pass failed", exc_info=True)

    def _eod_prediction_reconcile(self) -> None:
        """MITS Phase 5 (P5.2) — nightly reconcile of EodAnalysis vs
        same-day Trade rows. Idempotent. Runs even on non-trading days
        so a partial holiday run still gets resolved later.
        """
        try:
            from backend.bot.eod_bias import reconcile_outcomes
            target = most_recent_trading_day(include_today=True)
            stats = reconcile_outcomes(target)
            logger.info("eod prediction reconcile (%s): %s",
                            target.isoformat(), stats)
        except Exception:
            logger.debug("eod prediction reconcile failed", exc_info=True)

    def _eod_catchup_pass(self) -> None:
        """MITS Phase 4 (P4.5) — fill in a missed EOD pass.

        Resolves the most recent trading day, checks ``EodAnalysis`` for
        existing rows on that date, and runs ``run_eod_pass(date=...)``
        when ZERO rows exist. Idempotent — repeated runs on an
        already-populated day are no-ops.

        Runs Sunday 10:00 ET (covers Friday close → Monday open) and
        Monday 06:00 ET (covers holiday-Monday → Tuesday open).
        """
        try:
            from sqlalchemy import func, select
            from backend.bot.eod_analysis import run_eod_pass
            from backend.db import session_scope
            from backend.models.eod_analysis import EodAnalysis
            target = most_recent_trading_day()
            with session_scope() as s:
                existing = s.execute(
                    select(func.count(EodAnalysis.id))
                    .where(EodAnalysis.analysis_date == target)
                ).scalar() or 0
            if existing > 0:
                logger.info(
                    "eod catchup: %s already has %d rows, skipping",
                    target.isoformat(), existing,
                )
                return
            logger.info(
                "eod catchup: %s has no rows, running pass",
                target.isoformat(),
            )
            stats = run_eod_pass(date=target)
            logger.info("eod catchup pass: %s", stats)
        except Exception:
            logger.debug("eod catchup pass failed", exc_info=True)

    def _telegram_tomorrow_setup(self) -> None:
        """MITS Phase 3 — push Tomorrow's Setup digest to Telegram.

        Graceful no-op when Telegram credentials are missing OR the EOD
        pass produced no rows for the day.
        """
        if not is_trading_day():
            return
        if self.notifier is None or not self.notifier.enabled:
            return
        try:
            from backend.bot.eod_analysis import format_tomorrow_digest_text
            text = format_tomorrow_digest_text(limit=3)
            if not text:
                logger.debug("tomorrow setup digest: no rows")
                return
            self.notifier.send_text(text, bypass_filters=True)
            logger.info("telegram tomorrow setup sent (%d chars)", len(text))
        except Exception:
            logger.debug("telegram tomorrow setup failed", exc_info=True)

    def _ingest_live_outcomes(self) -> None:
        """MITS Phase 6 (P6.1) — convert closed trades into corpus
        observations. Idempotent via the IngestWatermark row."""
        try:
            from backend.bot.corpus.live_outcome_ingest import (
                ingest_live_outcomes,
            )
            stats = ingest_live_outcomes()
            logger.info("live_outcome ingest: %s", stats)
        except Exception:
            logger.debug("live_outcome ingest failed", exc_info=True)

    def _detector_suggestions_pass(self) -> None:
        """MITS Phase 6 (P6.3) — recommend detector disable/re-enable
        based on out-of-sample posterior. Operator must accept."""
        try:
            from backend.bot.scorecard.suggestions import run_suggestions_pass
            stats = run_suggestions_pass()
            logger.info("detector suggestions: %s", stats)
        except Exception:
            logger.debug("detector suggestions failed", exc_info=True)

    def _brain_prediction_link(self) -> None:
        """MITS Phase 14.D — resolve pending BrainPrediction rows."""
        try:
            from backend.bot.scorecard.brain_linker import link_brain_predictions
            stats = link_brain_predictions()
            logger.info("brain_prediction_link: %s", stats)
        except Exception:
            logger.debug("brain_prediction_link failed", exc_info=True)

    def _decision_funnel_daily_pass(self) -> None:
        """MITS Phase 18-FU Stream A — nightly Decision Funnel rollup.

        Computes the 10-stage funnel + confidence histogram + cooldown
        audit + counterfactual histogram over the prior day's window
        and writes one row to ``decision_funnel_daily`` keyed on
        yesterday's date. Always persists; not gated by any advisory
        flag.

        Investigation/diagnostic surface — never changes any threshold.
        Never raises: a corrupted provenance row must not take the
        scheduler down.
        """
        try:
            from datetime import datetime as _dt, timedelta as _td
            from backend.bot.learning.funnel import (
                compute_funnel_report, persist_funnel_report,
            )
            # Window: prior day, 00:00:00 → 23:59:59 ET. We compute via
            # UTC + 1d window ending at the most recent midnight UTC
            # (close enough — operator reads daily granularity).
            now = _dt.utcnow()
            window_end = _dt(now.year, now.month, now.day)
            report = compute_funnel_report(
                window_days=1, window_end=window_end,
            )
            yesterday = (window_end - _td(days=1)).date()
            meta = persist_funnel_report(report, target_date=yesterday)
            logger.info("decision_funnel_daily: %s", meta)
        except Exception:
            logger.debug(
                "decision_funnel_daily pass failed", exc_info=True,
            )

    def _learned_attribution_pass(self) -> None:
        """MITS Phase 18.A — nightly Learned Hypothesis Attribution.

        Computes per-agent / per-axis / per-strategy calibration over
        the trailing 90-day window of closed decisions, writes one
        ``learned_attribution`` row per scope. Never raises — the
        engine + scheduler remain green even when the aggregator hits
        an unexpected JSON shape.
        """
        try:
            from backend.bot.learning.attribution_writer import (
                persist_attribution_report,
            )
            meta = persist_attribution_report(window_days=90)
            logger.info("learned_attribution: %s", meta)
        except Exception:
            logger.debug("learned_attribution pass failed", exc_info=True)

    def _policy_tuning_pass(self) -> None:
        """MITS Phase 18.C — nightly Policy Auto-Tuning advisory pass.

        Honors the ``TUNABLES.policy_tuning_advisory_enabled`` opt-in
        flag. When OFF (default), the job logs a telemetry line +
        returns without writing rows. When ON, it computes
        per-tunable-rule threshold recommendations over the trailing
        90-day window and appends them to ``policy_tunings``.

        ADVISORY ONLY. The auto-apply path (gated on the second flag,
        ``policy_tuning_auto_apply_enabled``) is wired but NOT used —
        18.C never writes back to TUNABLES.

        Never raises — even when the rule's scenario_value_fn hits a
        novel JSON shape, we log + move on so the scheduler stays
        green.
        """
        try:
            from backend.config import TUNABLES
            if not bool(getattr(
                TUNABLES, "policy_tuning_advisory_enabled", False,
            )):
                logger.info(
                    "policy_tuning advisory pass: SKIPPED "
                    "(TB_POLICY_TUNING_ENABLED=False) — operator opt-in pending"
                )
                return
            from backend.bot.learning.policy_tuning import (
                compute_policy_tuning, persist_policy_tuning,
            )
            recs = compute_policy_tuning(window_days=90)
            meta = persist_policy_tuning(recs)
            logger.info("policy_tuning advisory pass: %s", meta)
        except Exception:
            logger.debug("policy_tuning advisory pass failed", exc_info=True)

    def _weight_adaptation_pass(self) -> None:
        """MITS Phase 18.D — nightly Online Agent Weight Adaptation
        advisory pass.

        Honors the ``TUNABLES.adaptive_weights_advisory_enabled`` opt-in
        flag. When OFF (default), the job logs a telemetry line +
        returns without writing rows. When ON, it computes per-agent
        weight proposals over the trailing 90-day window and appends
        them to ``agent_weight_history``.

        ADVISORY ONLY. The apply path (gated on the second flag,
        ``adaptive_weights_apply_enabled``) is wired but writing rows
        does NOT change engine behavior until the operator separately
        flips that flag.

        Never raises — failures are logged + the scheduler stays green.
        """
        try:
            from backend.config import TUNABLES
            if not bool(getattr(
                TUNABLES, "adaptive_weights_advisory_enabled", False,
            )):
                logger.info(
                    "weight_adaptation advisory pass: SKIPPED "
                    "(TB_ADAPTIVE_WEIGHTS_ENABLED=False) — operator opt-in pending"
                )
                return
            from backend.bot.learning.weight_adaptation import (
                compute_weight_proposals, persist_weight_proposals,
            )
            report = compute_weight_proposals(window_days=90)
            written = persist_weight_proposals(report)
            logger.info(
                "weight_adaptation advisory pass: written=%d "
                "advisory_enabled=%s apply_enabled=%s",
                written, report.advisory_enabled, report.apply_enabled,
            )
        except Exception:
            logger.debug(
                "weight_adaptation advisory pass failed", exc_info=True,
            )

    def _weekly_retrospective_pass(self) -> None:
        """MITS Phase 6 (P6.4) — Sunday 11:00 ET weekly retrospective
        for the prior Mon-Fri."""
        try:
            from datetime import timedelta as _td
            from backend.bot.retrospective import (
                build_weekly_retrospective, monday_of_week,
            )
            today = date.today()
            # We're on Sunday — the prior week's Monday is today - 6d.
            prior_monday = monday_of_week(today - _td(days=7))
            build_weekly_retrospective(prior_monday)
            logger.info(
                "weekly retrospective built for week %s",
                prior_monday.isoformat(),
            )
        except Exception:
            logger.debug("weekly retrospective failed", exc_info=True)

    # ── MITS Phase 8 jobs ────────────────────────────────────────────
    def _normalize_silver_pass(self) -> None:
        try:
            from backend.bot.data.silver import normalize_pass
            stats = normalize_pass()
            logger.info("silver normalize pass: %s", stats)
        except Exception:
            logger.debug("silver normalize pass failed", exc_info=True)

    def _gold_snapshot_pass(self) -> None:
        try:
            from backend.bot.data.gold import run_snapshot_pass
            stats = run_snapshot_pass()
            logger.info("gold snapshot pass: %d tables",
                            len(stats.get("tables") or {}))
        except Exception:
            logger.debug("gold snapshot pass failed", exc_info=True)

    def _vector_indexing_pass(self) -> None:
        try:
            from backend.bot.ai.vector_indexing import index_pass
            stats = index_pass(full=False)
            logger.info("vector indexing pass: %s", stats)
        except Exception:
            logger.debug("vector indexing pass failed", exc_info=True)

    def _cboe_pcr_refresh(self) -> None:
        try:
            from backend.bot.data.cboe import refresh as _refresh
            stats = _refresh()
            logger.info("cboe pcr refresh: %s", stats)
        except Exception:
            logger.debug("cboe pcr refresh failed", exc_info=True)

    def _gex_history(self) -> None:
        """Persist a GEX regime snapshot per configured ticker (#8)."""
        if not is_trading_day():
            return
        try:
            from backend.bot.signals.gex import store_regime_snapshot
            from backend.db import session_scope
            from backend.models.config import load_config

            with session_scope() as session:
                tickers = load_config(session).get("tickers") or ["SPY"]
            for tk in tickers[:12]:
                store_regime_snapshot(tk)
        except Exception:
            logger.debug("gex history job failed", exc_info=True)
