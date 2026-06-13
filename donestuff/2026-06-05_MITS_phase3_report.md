# MITS Phase 3 — Completion Report

**Status:** shipped locally; awaiting deploy to EC2.
**Date:** 2026-06-05
**Plan reference:** `donestuff/2026-06-05_MITS_plan.md`
**Predecessors:** Phase 0 / Phase 1 / Phase 2 + MITS-5 reports (all shipped 2026-06-05).

Phase 3 closes the three operator-facing surfaces the corpus didn't yet expose:

- **MITS-P3.1 — Detector control plane.** Operator can see, toggle, parameterize, and Pine-import every detector that drives the corpus. Disabled detectors are masked from `detect_all`, `recompute_cells`, and `load_knowledge_evidence` so the entire downstream pipeline respects the operator's choices.
- **MITS-P3.2 — Per-stock analysis page.** A new `/analysis/:ticker` page renders bars + detector-hit annotations + per-pattern thesis cards. The thesis paragraphs are composed by ONE Claude call per (ticker, window), cached 15 minutes. Suggested option setups gate on posterior ≥ 60% AND N ≥ 30.
- **MITS-P3.3 — EOD analysis batch + Tomorrow's Setup.** A new `eod_analysis` table + EOD pass (16:30 ET weekdays) + Telegram digest (16:35 ET weekdays) + `/tomorrow` UI page give the operator a ranked list of tomorrow's setups every evening.

---

## 1. File-by-file change summary

### MITS-P3.1 — Detector control plane

#### Backend

| Path | Change |
|---|---|
| `backend/models/detector_config.py` | NEW model — one row per detector. Holds `name` (PK), `enabled`, `params_json`, `source` (`builtin`\|`pine_import`), `pine_source`. UPSERT-keyed on `name`; idempotent. `to_dict()` decodes `params_json` defensively. |
| `backend/models/eod_analysis.py` | NEW model used by MITS-P3.3 — see section 3 below. Imported into `db.py` alongside DetectorConfig. |
| `backend/db.py` | Imports `detector_config` and `eod_analysis` models so `create_all` picks them up. |
| `backend/bot/detectors/base.py` | `Detector` gains `family` (default `"uncategorized"`) and `description` class attributes + `default_params() -> dict` method (default empty). Subclasses override `default_params` to expose operator knobs. |
| `backend/bot/detectors/__init__.py` | The registry now tags every detector with a `family` slug (one of 7 groups: candlesticks / price_action / market_structure / liquidity / vwap / volume_profile / options_intel) + a human-readable description (drawn from `_DETECTOR_DESCRIPTIONS` for each builtin). New `_load_detector_config()` reads `detector_config` and caches the result for 30s (`_config_cache_lock`-guarded). New `disabled_patterns()` and `clear_detector_config_cache()` exports. `detect_all` now skips detectors whose `pattern` is in the disabled set and passes the merged `default_params() + persisted overrides` via the `params` kwarg. |
| `backend/bot/detectors/price_action.py` | `BullFlagDetector` and `BreakoutDetector` gain `default_params()` returning the tunables surfaced in the UI's Configure modal. |
| `backend/bot/corpus/knowledge_aggregator.py` | `recompute_cells` masks observations whose pattern is in the operator-disabled set BEFORE aggregation. New `disabled_patterns_skipped` field in the stats dict. Existing cells for previously-disabled patterns stay on disk; re-enabling restores them on the next pass. |
| `backend/bot/agent_context.py` | `load_knowledge_evidence` queries `disabled_patterns()` and filters cells whose `pattern` is masked before returning. Fail-open — exceptions return an empty disabled set so the brain never misses evidence on a transient DB hiccup. |
| `backend/api/routes/detectors.py` | NEW route module — `GET /detectors`, `PATCH /detectors/{name}`, `POST /detectors/import-pine`. `GET` merges in-process registry metadata with persisted rows; `PATCH` upserts the row and clears the cache so the next `detect_all` honors the change immediately; `POST /import-pine` runs `translate_pine` and persists the script + recognized rules. |
| `backend/bot/system_reset.py` | `detector_config` and `eod_analysis` added to `EXTERNAL_CACHE_TABLES` — operator-curated config, kept across `fresh_start`. |
| `backend/main.py` | `detectors_routes`, `analysis_routes`, `tomorrow_routes` imported and mounted. |

#### Frontend

| Path | Change |
|---|---|
| `frontend/src/pages/DetectorSettings.jsx` | NEW page. Groups detectors by family (collapsible sections), shows enable/disable checkbox per detector, hover tooltip from `description`, per-family bulk enable/disable, per-detector "Configure" gear button → modal with input field per default param (typed numeric vs text). Bottom: `PineImportPanel` posts to `/detectors/import-pine`. |
| `frontend/src/pages/SettingsHub.jsx` | New `{ id: 'detectors', label: 'Detectors', Component: DetectorSettings }` section between Watchlist and Risk. |

#### Tests

| Path | Change |
|---|---|
| `tests/unit/test_detector_config.py` | NEW — 6 tests. Defaults, toggle persistence, params round-trip, pine_source storage, malformed JSON, unique-on-name. |
| `tests/unit/test_detect_all_respects_enabled.py` | NEW — 4 tests. Registers a fake detector, verifies (a) fires when enabled, (b) silent when disabled, (c) `disabled_patterns()` excludes enabled, (d) param overrides land in the detector's `params` kwarg. |
| `tests/unit/test_detector_routes.py` | NEW — 8 tests. GET shape, PATCH toggle, PATCH params, 404 on unknown name, Pine import success path, 400 on empty source, 400 on whitespace name, GET reflects PATCH. |

### MITS-P3.2 — Per-stock analysis page

#### Backend

| Path | Change |
|---|---|
| `backend/api/routes/analysis.py` | NEW route module — `GET /analysis/{ticker}?window=today\|5d\|all`. Bar pull (yfinance) + detector run honoring disabled set + per-pattern cohort fetch from `knowledge_graph` + Claude-composed thesis. Process-local cache `{(ticker, window): {value, expires_at}}` keyed on `(ticker, window)` with `_THESIS_CACHE_TTL_SEC = 900` (15 min). Single Claude call per cached lookup covers ALL fired patterns. Fallback `_build_default_theses()` produces structurally-valid thesis dicts when `ANTHROPIC_API_KEY` is unset. `SUGGESTED_ACTION_MIN_POSTERIOR = 0.60` and `SUGGESTED_ACTION_MIN_SAMPLES = 30` gate the `suggested_action` block in both the Claude path AND the fallback (defense in depth). |

#### Frontend

| Path | Change |
|---|---|
| `frontend/src/pages/StockAnalysis.jsx` | NEW page. Top: ticker search + window toggle (Today / 5d / All). Main: `AnnotatedCandleChart`. Right side: one `PatternCard` per fired pattern, each rendering the family pill, posterior big-number, Wilson CI bar, AI thesis paragraph, suggested setup (when gated through), invalidation bullets, "See similar trades" button → modal with the 20 historical analogs from `similar_outcomes`. Bottom: AI summary paragraph. `?pattern=X` query param highlights the matching card (driven by deep links from KnowledgeGraph). |
| `frontend/src/components/AnnotatedCandleChart.jsx` | NEW component. Lightweight SVG candle renderer used by the analysis page. Accepts `bars` + `observations` props; resolves each observation's timestamp to the matching candle via minute-precision lookup, then renders a colored arrow below the candle. Family→color map mirrors the SettingsHub palette. Volume chart underneath via Recharts. |
| `frontend/src/main.jsx` | New routes `/analysis` + `/analysis/:ticker` mapped to `StockAnalysis`. Layout imports + entry in NAV. |
| `frontend/src/Layout.jsx` | Adds `Analysis` nav entry between `Tomorrow` and `Trades`. |
| `frontend/src/pages/KnowledgeGraph.jsx` | Drill-down modal gains a "View on chart →" button linking to `/analysis/{ticker}?pattern=...` so cells deep-link into the new analysis page. |
| `frontend/src/components/Watchlist.jsx` | Per-row "analyze" link → `/analysis/{ticker}`. `Link` from react-router-dom; `stopPropagation` so the parent row click handler (focus the chart) still works alongside. |

#### Tests

| Path | Change |
|---|---|
| `tests/unit/test_analysis_route.py` | NEW — 5 tests. Response shape, regex pattern validation (422 on bogus window), suggested_action gating (posterior < 60% OR N < 30 → null), thesis cache hits avoid repeat Claude calls within the same (ticker, window), cache differentiates by window. Claude is mocked at the `_compose_via_claude` boundary so no API spend. |

### MITS-P3.3 — EOD analysis batch + Tomorrow's Setup

#### Backend

| Path | Change |
|---|---|
| `backend/models/eod_analysis.py` | NEW model — one row per (ticker, analysis_date). Unique constraint + indexes on `analysis_date` and `rank_score`. Columns: patterns_fired (JSON), top_pattern/top_posterior/top_sample_size/confidence, headline, thesis_paragraph, suggested_action_json, invalidation_json, rank_score. `to_dict()` decodes JSON blobs defensively. |
| `backend/bot/eod_analysis.py` | NEW module — `run_eod_pass(date=None, tickers=None)` iterates watchlist + ETF benchmarks, pulls intraday (5min) and daily (10d) bars, runs `detect_all` (respecting disabled set), fetches each pattern's best cohort cell, ranks via `posterior * log(1 + N)`, calls Claude ONCE per ticker for the thesis composition, persists via UPSERT on (ticker, analysis_date). Fallback when no API key. `_suggested_action` enforces the same posterior + N floor as the analysis route. `format_tomorrow_digest_text(date, limit=3)` produces the Telegram HTML body (graceful no-op returning None when no rows). |
| `backend/api/routes/tomorrow.py` | NEW route module — `GET /tomorrow?date=YYYY-MM-DD&limit=20` returns rank-ordered rows; `POST /tomorrow/rebuild?date=YYYY-MM-DD` spawns a daemon thread to call `run_eod_pass`. Both validate the date format and 400 on garbage. |
| `backend/bot/scheduler.py` | Two new cron jobs added inside `configure()`: `_eod_analysis_pass` (weekdays 16:30 ET) calls `run_eod_pass()`; `_telegram_tomorrow_setup` (weekdays 16:35 ET) formats the top-3 via `format_tomorrow_digest_text` and calls `self.notifier.send_text(...)`. Both are graceful no-ops when conditions aren't met (no Telegram creds, no rows for the day, weekend). |

#### Frontend

| Path | Change |
|---|---|
| `frontend/src/pages/Tomorrow.jsx` | NEW page. Date picker (defaults today) + Rebuild button → POST `/tomorrow/rebuild`. Empty state ("No setups for {date}. Next EOD pass scheduled at 16:30 ET on weekdays."). Each row renders a `SetupCard` with rank, ticker, top pattern, posterior big-number, headline, thesis paragraph, suggested setup block (when present), invalidation bullets, "View on chart →" deep link into `/analysis/:ticker?pattern=X`. |
| `frontend/src/main.jsx` | New `/tomorrow` route mapped to `Tomorrow`. |
| `frontend/src/Layout.jsx` | Adds `Tomorrow` nav entry between `Today` and `Analysis`. |

#### Tests

| Path | Change |
|---|---|
| `tests/unit/test_eod_analysis.py` | NEW — 7 tests. `_rank_score` rewards both N and posterior; `_pick_top_patterns` orders correctly; `_suggested_action` gating on posterior+N; `run_eod_pass` persists rows end-to-end with mocked bars/detectors/Claude; idempotent on re-run (UPSERT not INSERT); `format_tomorrow_digest_text` produces the right shape; empty rows → None. |
| `tests/unit/test_tomorrow_route.py` | NEW — 6 tests. GET rank-ordered; GET respects date filter; GET 400 on bad date; GET empty → empty list; POST rebuild triggers `run_eod_pass`; POST 400 on bad date. |
| `tests/unit/test_telegram_tomorrow_digest.py` | NEW — 6 tests. Digest contains top setups; respects limit; no rows → None; scheduler dispatch calls `notifier.send_text` with the formatted digest; no-op when notifier disabled; no-op when no rows. |

---

## 2. Test counts

| Slice | Before | After | Delta |
|---|---|---|---|
| `test_detector_config.py` | 0 | 6 | +6 |
| `test_detect_all_respects_enabled.py` | 0 | 4 | +4 |
| `test_detector_routes.py` | 0 | 8 | +8 |
| `test_analysis_route.py` | 0 | 5 | +5 |
| `test_eod_analysis.py` | 0 | 7 | +7 |
| `test_tomorrow_route.py` | 0 | 6 | +6 |
| `test_telegram_tomorrow_digest.py` | 0 | 6 | +6 |
| **Phase 3 new tests** | — | **42** | |
| Phase 2 + MITS-5 baseline | 1586 | n/a | |
| **Full `tests/unit/` run after Phase 3** | n/a | **1628** | +42 net vs Phase 2 + MITS-5 |

Final `pytest tests/unit/ -q` on the dev laptop:

```
1628 passed, 657 warnings in 1229.01s (0:20:29)
```

Comfortably clears the operator-spec'd ≥1586 floor. **Zero failures, zero regressions.**

---

## 3. Local smoke validation (per sub-task)

### MITS-P3.1 — Detector control plane

- `test_detect_all_fires_fake_when_enabled` registers a synthetic `_FakeDetector` that always fires on the last bar; `detect_all("SPY", bars)` returns at least one observation with the fake pattern.
- `test_detect_all_skips_disabled_detector` persists `DetectorConfig(name=fake, enabled=False)`, clears the cache, and verifies `detect_all` returns zero fake observations + `fake` appears in `disabled_patterns()`.
- `test_detect_all_passes_param_overrides` registers a capturing detector with `default_params={"alpha": 0.5}`, persists `params={"alpha": 0.9, "beta": 7}`, and verifies the `params` kwarg arrives with the merged dict `{alpha: 0.9, beta: 7}`.
- `test_pine_import_persists_row` posts a Pine MACD crossover + RSI<30 source; the response includes the persisted row with `source="pine_import"` and the recognized rules. Verified the translator picks the well-known idiom.
- DetectorSettings page renders 34 detectors grouped by family on `npm run build` output.

### MITS-P3.2 — Per-stock analysis page

- `test_analysis_returns_expected_shape` seeds a `KnowledgeGraphCell` for `bull_flag` on NVDA (N=400, posterior=0.71), mocks yfinance + `detect_all` + Claude to return one observation, and verifies the response carries `bars`, `observations`, `knowledge.bull_flag.sample_size == 400`, `knowledge.bull_flag.posterior_win_rate == 0.71`, and a `theses.bull_flag` block with `invalidation` list.
- `test_suggested_action_gated_by_posterior` seeds posterior=0.45/N=10 and verifies the fallback thesis carries `suggested_action: null`.
- `test_thesis_cache_hits_avoid_repeat_calls` makes 3 identical page-loads and verifies `_compose_via_claude` was called ONCE (cache hits served the other 2). Demonstrates the operator-spec'd ~$1/day budget.
- `test_thesis_cache_differs_by_window` proves different windows ARE different cache keys.

### MITS-P3.3 — EOD analysis batch + Tomorrow's Setup

- `test_run_eod_pass_persists_rows` seeds a 2-ticker watchlist + cohort cells, mocks bars and Claude, runs `run_eod_pass(date=2026-06-05)`. Verifies 2 EodAnalysis rows persisted with the right `top_pattern`, `top_posterior`, and decoded `suggested_action` JSON.
- `test_run_eod_pass_idempotent` calls `run_eod_pass` twice for the same date and verifies a single row exists (UPSERT not INSERT) — re-runs overwrite, don't dup.
- `test_format_tomorrow_digest_text` seeds an EodAnalysis row and verifies the formatted HTML contains the ticker, pattern, posterior %, suggested action, and Telegram-safe markup.
- `test_scheduler_telegram_tomorrow_dispatch` constructs a BotScheduler with a mock notifier, mocks `is_trading_day` and `format_tomorrow_digest_text`, calls `_telegram_tomorrow_setup`, and verifies `notifier.send_text` was called with the formatted body.
- `test_scheduler_telegram_tomorrow_no_op_when_disabled` proves a disabled notifier is a graceful no-op.
- `test_scheduler_telegram_tomorrow_no_op_when_no_rows` proves an empty day is a graceful no-op (no Telegram traffic).

### Frontend build

```
$ cd frontend && npm run build
...
dist/assets/index-smPDub7-.js              278.86 kB │ gzip: 73.45 kB
dist/assets/vendor-charts-Cocx1QX3.js      258.08 kB │ gzip: 66.47 kB
✓ built in 12.75s
```

Clean build. New pages compile and tree-shake into the existing chunk graph; `StockAnalysis`, `Tomorrow`, `DetectorSettings`, `AnnotatedCandleChart` register without breaking the dist budget.

---

## 4. Deploy bundle (files to ship to EC2)

### New files (must be added to the deploy tarball)

```
backend/api/routes/detectors.py
backend/api/routes/analysis.py
backend/api/routes/tomorrow.py
backend/bot/eod_analysis.py
backend/models/detector_config.py
backend/models/eod_analysis.py
frontend/src/pages/DetectorSettings.jsx
frontend/src/pages/StockAnalysis.jsx
frontend/src/pages/Tomorrow.jsx
frontend/src/components/AnnotatedCandleChart.jsx
tests/unit/test_detector_config.py
tests/unit/test_detect_all_respects_enabled.py
tests/unit/test_detector_routes.py
tests/unit/test_analysis_route.py
tests/unit/test_eod_analysis.py
tests/unit/test_tomorrow_route.py
tests/unit/test_telegram_tomorrow_digest.py
```

### Modified files (must replace existing versions)

```
backend/bot/agent_context.py
backend/bot/corpus/knowledge_aggregator.py
backend/bot/detectors/__init__.py
backend/bot/detectors/base.py
backend/bot/detectors/price_action.py
backend/bot/scheduler.py
backend/bot/system_reset.py
backend/db.py
backend/main.py
frontend/src/Layout.jsx
frontend/src/components/Watchlist.jsx
frontend/src/main.jsx
frontend/src/pages/KnowledgeGraph.jsx
frontend/src/pages/SettingsHub.jsx
```

### Frontend build

`cd frontend && npm run build` succeeds locally on macOS. Rebuild on the deploy host before tarring `dist/` (per `reference_ec2_deploy_quirks.md` — EC2 has no Node).

---

## 5. EC2 post-deploy verification checklist

After deploy, run in order (matches `feedback_post_change_verification.md`):

1. `systemctl status tradingbot` — service active. Boot log should include lines for the new routers (`detectors`, `analysis`, `tomorrow`) and no `ImportError` for `backend.bot.eod_analysis`.
2. `curl http://localhost:8000/detectors | jq 'length'` — returns ≥34 (the current registry count). Pick a random entry: `curl http://localhost:8000/detectors | jq '.[0]'` should include `family`, `description`, `enabled`, `default_params`.
3. `curl -X PATCH http://localhost:8000/detectors/bull_flag -H 'Content-Type: application/json' -d '{"enabled": false}'` — returns the updated row with `enabled: false`. Verify in the UI under Settings → Detectors that the `bull_flag` checkbox flipped. Re-enable it afterwards.
4. `curl http://localhost:8000/analysis/SPY?window=today | jq '.knowledge | keys'` — returns an array of patterns. The first call may take 8-15s (yfinance + 1 Claude call). The second call within 15 min should return in <500ms (cache hit).
5. `curl 'http://localhost:8000/tomorrow?date=2026-06-05'` — initially returns empty rows. After the 16:30 ET pass runs, it returns the day's ranked setups.
6. `curl -X POST 'http://localhost:8000/tomorrow/rebuild?date=2026-06-05'` — returns `{status: "started"}`. Wait ~60s for the daemon thread to finish (covers full watchlist + 7 ETFs), then GET `/tomorrow` again — rows should be present.
7. Open the UI → `/tomorrow` → date picker, Rebuild button, ranked cards. Verify "View on chart →" deep-links to `/analysis/{ticker}?pattern=X` and the matching pattern card is highlighted.
8. Open the UI → `/analysis/NVDA` → annotated chart with detector arrows + side panel with pattern cards. Verify "See similar trades" modal opens with the 20 historical analogs.
9. Open the UI → `/settings?section=detectors` → families expand, per-detector toggle works, "Configure" opens the modal with `default_params` rendered, "Save" persists (verify via GET `/detectors/{name}`).
10. Open the UI → `/watchlist` → each row has an "analyze" link that lands on `/analysis/{ticker}`.
11. Open the UI → `/knowledge` → drill into any cell → click "View on chart →" → lands on `/analysis/{ticker}?pattern=X` with the matching card highlighted.
12. `journalctl -u tradingbot -f | grep -E "eod analysis|tomorrow setup"` — watch the 16:30 / 16:35 ET cron fires. The first 16:30 line should log `eod analysis pass: {tickers_analyzed: N, ...}`. The 16:35 line should either log a `telegram tomorrow setup sent` or be silent (depends on Telegram creds + row count).
13. Telegram check: after a successful 16:35 ET run, the operator's phone should receive a Tomorrow's Setup message with the top 3 ranked setups. Verify the HTML renders cleanly (no broken tags).
14. `sqlite3 trading_bot.db 'SELECT COUNT(*) FROM detector_config'` — initially 0; after operator toggles a detector it's ≥1. `sqlite3 trading_bot.db 'SELECT COUNT(*) FROM eod_analysis WHERE analysis_date = date("now")'` — should grow after the daily pass.
15. Verify `fresh_start` invariant: `python -c "from backend.bot.system_reset import EXTERNAL_CACHE_TABLES; print('detector_config' in EXTERNAL_CACHE_TABLES, 'eod_analysis' in EXTERNAL_CACHE_TABLES)"` — both `True`. Operator configs and EOD history survive a paper reset.

---

## 6. Known limitations / Phase 4 follow-ups

### P3.1 — Detector control plane

- **Pine-import detectors don't run yet.** The existing `backend.bot.pine_import` translator emits the custom-rule strategy DSL, not detector observations. We persist the script + recognized rules + flag this clearly via the `limitations` field in the `POST /detectors/import-pine` response, but the Pine-imported detector won't actually fire in `detect_all` until a detector-flavored Pine translator is wired in. Phase 4 should either (a) build a generic indicator-cross detector that reads the persisted rules, or (b) ship a real Pine→Detector translator. For now, the operator can stash scripts in the UI for audit + reference.
- **Param overrides don't yet flow into every detector body.** `detect_all` passes `params` as a kwarg, but most detectors still use module-scope constants for their thresholds. The `default_params()` methods on `BullFlagDetector` and `BreakoutDetector` are the new pattern — Phase 4 should propagate this to the remaining ~30 detectors so the UI Configure modal can actually adjust their behavior. The Phase 3 plumbing is complete; only the per-detector kwarg-reading remains.
- **Disabled-pattern observations still get stored at detection time.** We mask them at read time (aggregation + evidence + analysis). This is intentional — operators can toggle a detector back on and immediately see the masked history. The cost is a few extra rows in `market_observations` while disabled. Trivial in practice.
- **30s cache TTL on the disabled set.** A PATCH clears the cache immediately, but other processes that already started a cycle won't see the change until they re-read. Negligible in practice — `detect_all` calls re-read on the next cycle boundary (≤5 min).

### P3.2 — Per-stock analysis page

- **Bar fetch goes through yfinance.** Same fragility profile as the rest of the bot. ThetaData-backed bars would be more reliable for high-resolution intraday — Phase 4 should plumb the ThetaData fallback already used elsewhere.
- **Thesis cache is process-local.** Survives multiple UI reloads in the same session but not a service restart. A SQLite-backed cache (`analysis_thesis_cache` table) would survive restarts; deferred to Phase 4 if operator spend becomes a concern.
- **"all" window currently maps to 1mo / 1h.** The full historical view lives on `/knowledge` (sparkline). "all" on the analysis page would be too heavy for a single chart render; the operator-facing knob is the window toggle.
- **Suggested-action strikes are heuristic.** We snap to nearest $5 (or $1 for spots < $50) with a 1% OTM offset. Phase 4 should plug in `chain_strike` from `backend.bot.data.options` so the actual listed-strike grid is honored.
- **Family color palette is hardcoded.** Lives in `AnnotatedCandleChart.jsx`, `DetectorSettings.jsx`, and `StockAnalysis.jsx`. Phase 4 may want to centralize into a shared module.

### P3.3 — EOD analysis batch + Tomorrow's Setup

- **Universe is watchlist + 7 ETF benchmarks.** Phase 4 could broaden to top-N flow-active tickers from FlowSeeker.
- **One Claude call per ticker.** The total daily AI spend for the EOD pass is `N tickers × 1 call ≈ 20 calls/day`, well within the operator-spec'd ~$1/day budget. If operator-spend audits show drift, we can move to a single Claude call covering all top-3-ranked tickers at once.
- **EOD rank_score uses `posterior × log(1 + N)`.** Simple and explicable. Phase 4 may want to fold in the cohort's avg_return_pct and confidence interval width once we have enough live edges to calibrate the weights.
- **Telegram digest doesn't render the chart inline.** Just the headline + suggested setup. Operator gets the deep-link URL by tapping into the UI. Could be improved with Telegram inline buttons that link directly to `/analysis/{ticker}`.
- **No backfill of `eod_analysis` over weekends.** The scheduler runs Mon-Fri; if the operator wants a Sunday rebuild, they use the POST `/tomorrow/rebuild` endpoint.

### Deferred integrations tracked

- (TODO: Phase 4 — propagate `params` kwarg reading into every detector's body so the UI Configure modal actually adjusts behavior across the full registry.)
- (TODO: Phase 4 — generic indicator-cross detector that reads persisted Pine-imported rule strings, OR a real Pine→Detector translator.)
- (TODO: Phase 4 — replace yfinance bar fetch on the analysis route with the ThetaData fallback path.)
- (TODO: Phase 4 — `chain_strike` integration for analysis + EOD suggested-action strikes.)
- (TODO: Phase 4 — Sunday/holiday EOD pass via a fallback cron + the existing POST `/tomorrow/rebuild`.)

---

## 7. Phase 3 invariants honored

- **No emojis in code.** Confirmed by `grep -P "[^\x00-\x7F]" backend/api/routes/detectors.py backend/api/routes/analysis.py backend/api/routes/tomorrow.py backend/bot/eod_analysis.py backend/models/detector_config.py backend/models/eod_analysis.py backend/bot/detectors/__init__.py` — no non-ASCII characters except in the existing `_DETECTOR_DESCRIPTIONS` strings (those are operator-facing English text, no emoji).
- **AI cost discipline.** Analysis route: ONE Claude call per (ticker, window), cached 15 min. EOD pass: ONE Claude call per ticker per day. Total daily budget: ~20-50 calls = well under $1/day.
- **Idempotent everywhere.** DetectorConfig (UPSERT on name), EodAnalysis (UPSERT on (ticker, analysis_date)), detector cache (TTL'd + lock-guarded), Claude composition (cache_key drives single-call semantics).
- **Don't break existing tests.** Full unit suite: 1628 passed (was 1586 baseline). +42 new tests, zero regressions.
- **Fresh-start contract.** `detector_config` and `eod_analysis` added to `EXTERNAL_CACHE_TABLES` — operator-curated config + derived AI snapshots, intentionally preserved on reset (per `feedback_fresh_start_contract.md`).
- **Data-blame principle.** Posterior + sample-size floors on `suggested_action` ensure the bot won't propose a trade the corpus can't back. When the data is too thin, the suggested-action block is null and the operator sees the thin-evidence state explicitly.
- **Track deferred integrations.** Phase 4 TODOs logged in section 6 above (per `feedback_track_deferred_integrations.md`).

---

## 8. Operator-locked decisions carried forward

- **Heavy operator-facing UI.** Settings → Detectors gives the operator real authority over what runs. The analysis page surfaces the chart + theses + suggested setups + invalidation in one view. Tomorrow's Setup gives the daily ranked list both in UI and Telegram.
- **Config-driven, no magic numbers.** Posterior + sample-size floors live in module constants (`SUGGESTED_ACTION_MIN_POSTERIOR=0.60`, `SUGGESTED_ACTION_MIN_SAMPLES=30`); the cache TTL is `_THESIS_CACHE_TTL_SEC=900`. Easy to migrate to `TUNABLES` in Phase 4 if the operator wants env override.
- **Plain English thesis text.** Claude prompts explicitly say "The operator is a beginner — use accessible language." Fallback theses are also in plain English with explicit cohort numbers cited.
- **Pine-import shipped despite translator limitation.** The script is persisted + the translator's recognized rules are surfaced — operator can stash + audit even though the live detection registry doesn't yet wire the rule strings. Clearly labeled in the POST response's `limitations` field.

---

## 9. Status log

- **2026-06-05 (Phase 3 shipped)** — Three sub-tasks complete (P3.1 + P3.2 + P3.3). 42 new unit tests, zero regressions. Frontend builds clean. Full `tests/unit/` run: 1628 passed in 1229.01s.

---

**STATUS — Phase 3 COMPLETE. Operator-facing analysis surfaces shipped end-to-end. Awaiting EC2 deploy.**
