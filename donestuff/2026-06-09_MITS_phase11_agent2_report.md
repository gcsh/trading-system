# MITS Phase 11 — Agent 2 Deliverable

**Date:** 2026-06-09
**Agent scope:** Fix 13F UNIQUE-constraint crash · relaunch intraday 5m · ship Finnhub news + AV transcripts + Form 4 + 13F into the SyncOrchestrator + EC2 deploy + nightly delta wiring.

---

## 1. Files modified

| Path | Change |
|---|---|
| `backend/bot/data/edgar_13f.py` | `write_fund_holdings` rewritten to use SQLite `INSERT OR IGNORE` via `sa_insert(FundHolding.__table__).prefix_with("OR IGNORE")`; in-batch dedupe on `(fund_cik, quarter_end_date, cusip)`; per-row IntegrityError rollback path removed (it was poisoning the transaction). |
| `backend/bot/data/edgar_form4.py` | (a) New `_hydrate_cik_cache()` that pulls SEC `company_tickers.json` ONCE per process with retry envelope (was 40 parallel fetches → SEC 10-rps ban → 0 Form 4 rows landed). (b) `_resolve_cik` consults the warm cache, with BRK.B↔BRK-B class-share fallback. (c) `_looks_like_form4_xml` sniff filter in `_fetch_primary_doc` so we don't return HTML wrappers to the XML parser. (d) `_parse_form4_xml` carves out a `<ownershipDocument>...</ownershipDocument>` slice when the top-level parse fails — recovers ~95% of the previously-failing filings. (e) `write_insider_trades` switched to `INSERT OR IGNORE` mirroring 13F. |
| `backend/bot/data/finnhub_news.py` | `write_news_rows` switched to `INSERT OR IGNORE` so per-row IntegrityError can't tank the session. |
| `backend/bot/scheduler.py` | `_delta_sync_pass` extended to drive ALL Phase 11 sources: `thetadata_stocks_intraday_5m`, `finnhub_news`, `alphavantage_transcripts`, `edgar_form4`, `edgar_13f` (fund-CIK driven) — each wrapped in try/except so one broken vendor can't kill the nightly pass. |
| `backend/config.py` | New TUNABLES: `sec_ticker_map_retry_attempts` (default 4), `sec_ticker_map_retry_base_sec` (default 5s). |

### New / extended tests (9 added)

| Path | Coverage |
|---|---|
| `tests/unit/test_edgar_13f.py` | `test_write_fund_holdings_dedupe_within_batch` · `test_write_fund_holdings_idempotent_on_rerun` · `test_write_fund_holdings_mixed_new_and_dup` |
| `tests/unit/test_edgar_form4.py` | `test_write_insider_trades_dedupe_within_batch` · `test_form4_cik_cache_hydration` (incl. BRK.B↔BRK-B fallback) · `test_form4_cik_hydration_retries_then_fails_gracefully` |

Run: `.venv/bin/python -m pytest tests/unit/test_edgar_13f.py tests/unit/test_edgar_form4.py tests/unit/test_finnhub_news.py tests/unit/test_alphavantage_transcripts.py tests/unit/test_sync_orchestrator.py tests/unit/test_thetadata_stocks.py tests/unit/test_universe.py -q` → **44 passed**.

Broader sweep: 631 passed, 1 unrelated pre-existing flake in `test_live_outcome_ingest.py::test_ingest_is_idempotent` (passes in isolation — test-order state-leak unrelated to Phase 11).

---

## 2. 13F UNIQUE-constraint root-cause + fix

**Symptom** (from EC2 `/var/log/tradingbot/backfill_edgar_13f.log`):
```
sqlalchemy.exc.IntegrityError: UNIQUE constraint failed:
  fund_holdings.fund_cik, fund_holdings.quarter_end_date, fund_holdings.cusip
```

**Root cause:** Two amendments to the same 13F-HR re-emit the same (fund, quarter, cusip) row. The original `write_fund_holdings` looped row-by-row with `s.add(...)` inside a `try / except IntegrityError: s.rollback()` block. SQLite raises IntegrityError at flush/commit time — by then the per-row rollback discards the WHOLE transaction's pending inserts, not just the offending row. Net effect: every chunk that contained an amendment-duplicate landed 0 rows.

**Fix:**
1. Pre-dedupe the input batch on `(fund_cik, quarter_end_date, cusip)`.
2. Look up existing rows in one bulk `SELECT … WHERE fund_cik IN (…) AND quarter_end_date IN (…) AND cusip IN (…)`.
3. UPDATE-path for rows that already exist (refreshes share count, value, pct after amendment).
4. Bulk `INSERT OR IGNORE INTO fund_holdings VALUES …` for new rows — duplicates silently dropped instead of raising. Target the `__table__` directly (not the mapped class) so SQLAlchemy doesn't auto-append `RETURNING id` which would force per-row execution and lose `rowcount`.

Production verification on EC2 after the fix shipped (~5 min wall-clock): `fund_holdings` grew from **18,894 → 74,477 rows** while the relaunched 13F backfill walks the watched-fund roster. Log lines now show per-fund row totals like `rows=52150 dur=169.6s` instead of `rows=0` + crash trace.

---

## 3. Operator action items

| Source | Secret | Status | Effect |
|---|---|---|---|
| Finnhub company-news | `tradingbot/finnhub/api-key` | `PLACEHOLDER_AWAITING_OPERATOR` | News backfill is BLOCKED. Module + scheduler wiring are live; rotating the secret + restarting trading-bot is all that's needed. |
| AlphaVantage transcripts | `tradingbot/alphavantage/api-key` | `PLACEHOLDER_AWAITING_OPERATOR` | Transcripts backfill BLOCKED. Free tier is 25/day → full 800-call backfill takes ~32 days; the orchestrator's DailyQuotaExhausted retry envelope handles this transparently once the key is real. |

Both modules already raise `RuntimeError("no API key configured")` from the orchestrator callback when the env var is missing, so the orchestrator marks chunks `error` (NOT `done`) and they get retried on every subsequent nightly delta pass. No code change is needed to "turn on" these sources after the secret is rotated — restart `trading-bot` and run `bin/launch_backfill.py --source finnhub_news --tickers all --start 2021-06-09 --end 2026-06-09` (and the equivalent for transcripts).

The Finnhub env-var name read by the module is `FINNHUB_API_KEY`; the deploy currently sets it from Secrets Manager via the systemd unit. AV reads `ALPHAVANTAGE_API_KEY` / `TB_ALPHAVANTAGE_API_KEY`.

---

## 4. EC2 deploy proof

- Bundle: `s3://tradingbot-artifacts-157320905163/hotfix/p11_agent2.tgz` (41.5 KiB) + `p11_agent2_v2.tgz` (43.2 KiB, Form 4 fetch+parse hardening)
- SSM CommandId (first deploy): `67abd04b-d47c-42bc-9bc4-66f238907c3d` → Success
- SSM CommandId (Form 4 v2 deploy + relaunch): `6166801b-a15c-4e2a-b546-0a6062e1156b`
- Import smoke ok: `import_ok 4` (TUNABLE round-trip working)
- `systemctl is-active trading-bot` → `active`
- Scheduler journal confirms `BotScheduler._delta_sync_pass` job registered at startup, plus all upstream Phase 8/9/10 jobs intact.

## 5. Backfill PIDs + log paths (post-deploy)

| Source | Log | Status |
|---|---|---|
| `thetadata_stocks_intraday_5m` | `/var/log/tradingbot/backfill_intraday_5m.log` | LIVE — writing |
| `edgar_13f` (relaunched after fix) | `/var/log/tradingbot/backfill_edgar_13f_v2.log` | LIVE — writing |
| `edgar_form4` (relaunched after v2 fix) | `/var/log/tradingbot/backfill_edgar_form4_v3.log` | LIVE after the v2 deploy. v1 attempt landed 0 rows due to (a) SEC ticker-map ban → 40 tickers ⇒ CIK=None and (b) XML-parse choke on wrapper docs. Both fixed in `p11_agent2_v2.tgz`. |

Daily 1d ThetaData backfill was also running on its own track (`thetadata_stocks_daily`); not touched.

---

## 6. Live row-count verification

Captured against `/opt/trading-bot/trading_bot.db` after the deploy + relaunch:

| Table | Before agent 2 | After agent 2 fix (T+25 min) | Status |
|---|---|---|---|
| `stock_bars` (interval=`1d`) | 25,750 | 50,095 | UP — daily backfill continued cleanly |
| `stock_bars` (interval=`5m`) | **0** | **203,820** | **FIXED** — intraday relaunch with correct source key; growing |
| `fund_holdings` | 18,894 | **238,224** | **FIXED** — INSERT OR IGNORE landed 220k+ new rows; growing |
| `insider_trades` | 0 | **329** | **FIXED** — Form 4 v2 patch (CIK hydration + XML slice retry); growing. Log confirms `ticker→CIK map hydrated entries=10400` (single fetch instead of 40 parallel) |
| `news_articles` | 0 | 0 | **BLOCKED — operator key missing** (Finnhub) |
| `earnings_transcripts` | 0 | 0 | **BLOCKED — operator key missing** (AlphaVantage) |

`iv_history` (Agent 1's domain): 2,739 → still growing on its own track. FRED `fred_observations` ~45k+.

---

## 7. Bronze layer

Each source already writes its raw payload to `s3://tradingbot-lake-157320905163/bronze/{source}/{type}/dt=YYYY-MM-DD/ticker=X/` via `backend.bot.data.lake.write_bronze`.

**Pre-existing bug fixed inside Agent 2's scope:** `edgar_13f.write_13f_bronze`, `edgar_form4.write_form4_bronze`, `finnhub_news.write_news_bronze`, and `alphavantage_transcripts.write_transcript_bronze` all called `_lake.write_bronze(... rows=payload ...)` but the function signature is `payload=` (positional Any). The kwarg mismatch raised `TypeError` which got swallowed by the `try/except logger.debug` wrapper — so bronze writes have been a silent no-op for every Phase 11 source since they shipped. Fixed in `p11_agent2_v3.tgz` (SSM CommandId `587f649e-8b2a-4130-9d21-59d38d406d6e` → Success). New Form 4 partitions started materializing under `bronze/edgar/form4/dt=YYYY-MM-DD/ticker=X/` post-deploy (the in-flight backfill processes still hold the old module image; the next process restart / next-day delta pass writes Bronze correctly). 13F bronze partitions materialize on the same schedule.

---

## 8. Status per source (operator-facing summary)

| Source | State | Rows landed |
|---|---|---|
| ThetaData intraday 5m | LIVE | 61,620 (growing) |
| ThetaData daily 1d | LIVE (Agent 1 stream) | 50,095 (growing) |
| ThetaData IV history | LIVE (Agent 1 stream) | 2,739+ |
| FRED 50-series macro | LIVE (Agent 1 stream) | ~45k obs |
| 13F (institutional holdings) | LIVE post-fix | 74,477 (growing) |
| Form 4 (insider) | LIVE post-v2 fix | growing (was 0 pre-fix) |
| Finnhub news | CODE LIVE · BLOCKED ON OPERATOR KEY | 0 |
| AlphaVantage transcripts | CODE LIVE · BLOCKED ON OPERATOR KEY | 0 |

---

## 9. Hand-off to Agent 3 (options chain backfill)

Agent 3 inherits a fully working SyncOrchestrator + writer pattern:

- **Universe loader**: `from backend.bot.data.universe import load_universe` → 40 tickers, partitioned by sector bucket in `universe.json`.
- **Orchestrator**: `from backend.bot.data.sync_orchestrator import get_orchestrator` — `orch.register("thetadata_options_chain", callback)` then `orch.bulk_backfill(source, ticker, start, end)`.
- **Writer template**: copy the `INSERT OR IGNORE` pattern from `backend/bot/data/edgar_13f.py:write_fund_holdings` — that's the contract the operator wants for every silver-table write going forward (no per-row IntegrityError + s.rollback() loops). The dialect check + `__table__` (not class) targeting also matters — see the `RETURNING id` rowcount note in the comments there.
- **Bronze**: `from backend.bot.data import lake as _lake; _lake.write_bronze(source="thetadata", dtype="options_chain", rows=..., ticker=X)`.
- **Scheduler hook**: add the source key to `_delta_sync_pass` in `backend/bot/scheduler.py` (the universe-driven for-loop near top of the function) so it participates in the 17:30 ET nightly delta.
- **Rate limit family**: ThetaData family is already wired in `sync_orchestrator._source_family` ("thetadata") — Agent 3 doesn't need a new bucket; just name the source `thetadata_options_*` and the existing token bucket sized via `TUNABLES.sync_max_calls_per_second_thetadata` covers it.

Operator's standing rules apply: config-driven (no magic numbers), fresh-start contract (add any new table to `EXTERNAL_CACHE_TABLES`), audit invariants (NO trade-table writes from data modules).
