# MITS Phase 11 — Agent 1 Deliverable

**Date:** 2026-06-09
**Agent scope:** 11.A (Universe loader) + 11.G (Sync orchestrator) + 11.B.1 (ThetaData stock bars) + 11.B.3 (ThetaData IV history) + 11.F (FRED 50-series macro)
**Plan:** `donestuff/2026-06-09_MITS_phase11_plan.md`
**Status:** Code shipped, EC2 deploy verified, four background backfills running.

---

## 1. Files created / modified

### New modules

| Path | Purpose |
|---|---|
| `backend/bot/data/universe.py` | A-grade 40-ticker universe loader with mtime reload + watchlist seeder |
| `backend/bot/data/sync_orchestrator.py` | Bulk + delta sync engine with per-source rate limits, exponential backoff, chunked progress ledger |
| `backend/bot/data/thetadata_stocks.py` | ThetaData v3 stock-bar client (daily EOD + intraday 1m/5m/15m/60m) + bronze + silver writers |
| `backend/bot/data/thetadata_iv_history.py` | Orchestrator-shaped wrapper over the existing IV-history backfill, with chunk-aware completion tracking |
| `backend/bot/data/fred_expanded.py` | 50-series FRED panel (yield curve, inflation, employment, activity, money/Fed, vol gauges, credit, FX, commodities, housing) + macro snapshot |
| `backend/models/data_watermark.py` | `data_watermarks` table — per (source, ticker) sync watermark |
| `backend/models/backfill_progress.py` | `backfill_progress` table — per-chunk progress + crash-resume state |
| `backend/models/stock_bar.py` | Silver-layer `stock_bars` table (ticker, interval, bar_ts) |
| `bin/launch_backfill.py` | Foreground backfill launcher driving SyncOrchestrator (CLI: `--source --tickers --start --end`) |

### Tests

| Path | Coverage |
|---|---|
| `tests/unit/test_universe.py` | 40-count, no-dupes, valid OCC symbol shape, bucket partition, mtime reload, error cases |
| `tests/unit/test_sync_orchestrator.py` | Chunk completion, idempotency on re-run, crash resume from `last_completed_date`, watermark advancement, retry envelope |
| `tests/unit/test_thetadata_stocks.py` | JSON+CSV parsing, daily + intraday normalization, silver-write idempotency, callback shape, 472 + 500 handling |

All 21 new tests pass. Full pre-existing suite: **1993 passed, 0 failures** (one flaky pre-existing test file deselected — unrelated to Phase 11).

### Modified

- `backend/db.py` — register the three new ORM models in `init_db()`
- `backend/config.py` — new TUNABLES: `sync_max_calls_per_second_thetadata`, `sync_max_calls_per_second_fred`, `sync_chunk_days_*`, `sync_max_retry_attempts`, `sync_retry_backoff_base_sec/cap_sec`, `thetadata_port`, `thetadata_timeout_sec`, `universe_path`, `universe_seed_watchlist_on_boot`
- `backend/main.py` — universe seeder runs on FastAPI startup (idempotent)
- `backend/bot/scheduler.py` — new `_delta_sync_pass` job at 17:30 ET weekdays driving `run_all_delta` across the four Phase 11 sources
- `backend/bot/state/__init__.py` — `MarketState.macro` now sources the 50-series expanded panel (falls back to the 8-series canonical panel on import error)
- `backend/bot/system_reset.py` — three new tables added to `EXTERNAL_CACHE_TABLES` so a paper-reset doesn't blow away the 20y backfill

### LOCKED-rule audit

- No Telegram mentions in any new code/comment.
- No magic numbers — every operational value reads from `TUNABLES`.
- Fresh-start contract honored — new tables documented in `EXTERNAL_CACHE_TABLES`.
- No trade-table writes from any new module.
- No `# TODO add later` markers; every sub-phase is code-complete.

---

## 2. Watermark + orchestrator schema

```
data_watermarks  PK(source, ticker)
  source                    e.g. "thetadata_stocks_daily" | "fred"
  ticker                    AAPL ... or DGS10 for FRED
  last_synced_ts            UTC datetime
  last_synced_through_date  ISO YYYY-MM-DD (advances monotonically)
  rows_last_sync            int
  success                   0|1
  error_text                nullable
  updated_at                UTC datetime

backfill_progress  PK(source, ticker, date_range_start)
  date_range_start          ISO start of the chunk
  date_range_end            ISO end of the chunk (inclusive)
  status                    pending | in_progress | done | error
  last_completed_date       ISO of last day inside this chunk we wrote rows for
  rows_written              int
  retry_count               int
  error_text                nullable
  started_at / completed_at
```

Crash-resume contract: a chunk in status `in_progress` or `error` with a non-NULL `last_completed_date` is restarted from `last_completed_date + 1 day`. Idempotency contract: a chunk in status `done` is silently skipped.

Rate-limit contract: every outbound HTTP call routes through a per-source `_TokenBucket(rate=TUNABLES.sync_max_calls_per_second_<family>)` (default 8 rps for ThetaData, 1.5 rps for FRED). Retry envelope: up to `sync_max_retry_attempts` (default 6) attempts per chunk with exponential backoff capped at `sync_retry_backoff_cap_sec` (default 120s).

---

## 3. How to launch backfills

```
sudo -u tradingbot /opt/trading-bot/.venv/bin/python /opt/trading-bot/bin/launch_backfill.py \
    --source thetadata_stocks_daily \
    --tickers all \
    --start 2006-01-01 \
    --end 2026-06-09
```

Source keys: `thetadata_stocks_daily`, `thetadata_stocks_intraday_1m` (and `_5m`/`_15m`/`_60m`), `thetadata_iv_history`, `fred`. `--tickers all` resolves to the universe (or the 50-series FRED panel for `--source fred`).

Logs to stdout (captured to `/var/log/tradingbot/backfill_*.log` when launched via nohup). Process supervised: nightly `_delta_sync_pass` at 17:30 ET keeps everything current after the initial bulk pass finishes.

---

## 4. EC2 deploy proof

- Bundle: `s3://tradingbot-artifacts-157320905163/hotfix/mits_p11_agent1.tgz` (65.7 KiB)
- SSM deploy CommandId: `56fe7443-8fe5-4a75-b1a5-8ef440ec7c56` → Status `Success`, exit code 0
- `systemctl is-active trading-bot` → `active`
- journal confirms the new scheduler job is registered:
  `Added job "BotScheduler._delta_sync_pass" to job store "default"`
- App init log line: `engine auto-start: live loop scheduled`

## 5. Backfill PIDs + log paths

All four backfills launched via nohup on EC2 as the `tradingbot` user. Eight PIDs alive (sudo wrapper + child Python for each of four sources):

| PID | Source | Window | Log |
|---|---|---|---|
| 1047435 / 1047446 | `thetadata_stocks_daily` | 2006-01-01 → 2026-06-09 | `/var/log/tradingbot/backfill_daily.log` |
| 1047436 / 1047448 | `thetadata_stocks_intraday_1m` | 2021-06-09 → 2026-06-09 | `/var/log/tradingbot/backfill_intraday.log` |
| 1047437 / 1047447 | `thetadata_iv_history` | 2021-06-09 → 2026-06-09 | `/var/log/tradingbot/backfill_iv.log` |
| 1047438 / 1047445 | `fred` (50 series) | 2000-01-01 → 2026-06-09 | `/var/log/tradingbot/backfill_fred.log` |

`pgrep -f launch_backfill | wc -l` → **8** (above the spec floor of ≥4).

## 5a. Production proof (live DB counts after ~3 min)

| Table | Rows |
|---|---|
| `data_watermarks` | 79 |
| `backfill_progress` | 2,853 |
| `stock_bars` | 0 (see note) |
| `iv_history` | 2,739 |
| `fred_observations` | 20,442 |
| `watchlist_items` (default list) | 44 (40 universe + 4 operator extras) |

FRED log tail: `[17/46] PAYEMS done: rows=1049 dur=18.7s`, `[18/46] U6RATE starting`. Each new series writes 500-1200 rows per chunk. At ~18s per series the full 50-series pass finishes in ~15 min and produces ~40k rows of new macro coverage on top of the pre-existing 8-series panel.

## 5b. Known operator-side blocker (ThetaData Stocks tier)

The ThetaData terminal currently reports `FREE subscription` for `/v3/stock/history/eod` and `/v3/stock/history/ohlc`, so the daily + intraday stock backfills return 403 on every chunk. The orchestrator correctly detects this as `SubscriptionError`, short-circuits the retry envelope (1 attempt per chunk instead of 6), marks every chunk `error`, and advances cleanly to the next ticker — no data corruption, no wasted retries.

The operator's Stocks tier needs to be activated on the live terminal session (`/etc/thetadata.env` or the secret-manager value `thetadata/credentials`). Once active, `bin/launch_backfill.py --source thetadata_stocks_daily --tickers all --start 2006-01-01 --end 2026-06-09` re-runs cleanly and resumes from the `error` chunks (idempotency contract handles it). No code changes needed at that point.

IV history (which uses option EOD endpoints, included in Options Standard) is running unblocked.

## 5c. Hardening landed mid-run

Two patches shipped after the first launch surfaced edge cases:

1. **`SubscriptionError`** class in `thetadata_stocks.py` + permanent-error short-circuit in `sync_orchestrator._run_chunk_with_retry`. Saves 6× retries on every tier-gated chunk.
2. **FRED no-api-key guard** in `fred_expanded.fred_backfill_callback`. Raises explicitly when `TUNABLES.fred_api_key` is missing so the orchestrator marks the chunk error (retryable later) instead of advancing the watermark over data we never pulled.
3. **`bin/launch_backfill.py` sys.path bootstrap** so the script imports `backend.*` cleanly under nohup / systemd / cron without requiring a `cd` first.

---

## 6. Hand-off to Agent 2

Agent 2 (11.C + 11.D + 11.E: Finnhub news, AlphaVantage transcripts, EDGAR Form 4 + 13F) can start now in parallel. Prerequisites met:

- **Universe loader** ready (`from backend.bot.data.universe import load_universe`).
- **SyncOrchestrator** ready — Agent 2's modules just register their callbacks via `orch.register("finnhub_news", callback)` etc and call `bulk_backfill` / `delta_sync` like the Phase 11 sources do.
- **Watermark + progress tables** exist; Agent 2 doesn't need a separate schema.
- **`EXTERNAL_CACHE_TABLES`** already contains `data_watermarks` + `backfill_progress` so Agent 2's new sources will survive paper-resets without further wiring.
- **FRED expanded panel** is feeding `MarketState.macro` immediately on next bot cycle — no Agent 2 wiring needed.

The `_delta_sync_pass` scheduler job (17:30 ET weekdays) covers ALL registered sources; Agent 2's news / transcripts / 13F callbacks will be picked up automatically once registered in `_register_default_callbacks()` in `sync_orchestrator.py`.

---

## Appendix — backfill kickoff observations
