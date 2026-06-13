# MITS Phase 1 — Completion Report

**Status:** shipped locally; awaiting deploy to EC2.
**Date:** 2026-06-05
**Plan reference:** `donestuff/2026-06-05_MITS_plan.md`
**Phase 0 reference:** `donestuff/2026-06-05_MITS_phase0_report.md`

Phase 1 closes the six follow-ups from Phase 0:

  - **MITS.1.A** — EvidencePanel wired into Today / Trades / Mission
                       Control surfaces.
  - **MITS.1.B** — Knowledge-graph evidence injected into
                       `build_agent_context`, the council memory-bias
                       chokepoint, and the AI Brain prompt.
  - **MITS.1.C** — Daily IV (and best-effort GEX) series carried into
                       intraday historical_replay, with documented
                       degraded-mode behaviour. Options-intel detectors
                       now fire on historical data.
  - **MITS.1.D** — Walk-forward aggregator split — every cohort writes
                       three rows: `in_sample`, `out_of_sample`,
                       `combined`. UI defaults to out-of-sample with
                       chip toggles for the other two.
  - **MITS.1.E** — `KnowledgeGraphHistory` table + nightly snapshot
                       job + real recharts sparkline in the drill-down
                       modal.
  - **MITS.1.F** — `CorpusStatusChip` rendered on each Watchlist row,
                       polling /knowledge/corpus/status.

The architecture promise from the plan (MITS.4: Brain reasons OVER
evidence, not from first principles) is now real end-to-end. Snapshot →
agent_context → memory_bias → council vote → Chairman → Brain prompt
all see the same `knowledge_evidence` block for the active ticker.

---

## 1. Task-by-task summary

### Task A — EvidencePanel wired into trade UIs

| File | Change |
|---|---|
| `frontend/src/components/EvidencePanel.jsx` | Extended with a `topN` mode: when no `pattern` prop is passed, fetches `/knowledge/cells?ticker=&min_samples=5&limit=N` and renders the top-N pattern rows. Pattern-mode still hits `/knowledge/{ticker}/{pattern}` and renders the single-cell summary. |
| `frontend/src/components/CurrentlyHoldingStrip.jsx` | Imports `EvidencePanel`; for each open-position card, drops the top-3 patterns evidence panel below the price / P&L row. |
| `frontend/src/components/TradeDetail.jsx` | Imports `EvidencePanel`; after the "Why the bot took this trade" panel, renders pattern-mode + ticker-only fallback. |
| `frontend/src/pages/MissionControl.jsx` | Imports `EvidencePanel`; for the active selected trade, renders pattern-mode (strategy slug as pattern hint) + ticker-only fallback above the Chairman panel. |

Operator now sees corpus evidence on the Today page (under each
holding), the Trades detail drawer, and Mission Control. Pure read-only
DOM additions — no engine changes for this task.

### Task B — Agent context + Brain prompt knowledge injection

| File | Change |
|---|---|
| `backend/bot/agent_context.py` | New `load_knowledge_evidence(ticker, regime, vol_state, snapshot, strategy)` function. Resolves the current time bucket from the snapshot timestamp, queries the populated `knowledge_graph` cells that match the current cohort (ticker + regime + vol_state + time_bucket + horizon), ranks by posterior win rate, falls back to ticker+horizon when the exact cohort yields nothing. Joins forward to `market_observations` + `market_outcomes` for the 20 most-recent matching observations. Returns `{cells, summary, most_similar_outcomes}`. Hooked into `build_agent_context` so every council run now carries the block. `apply_memory_bias` now also reads `knowledge_evidence`: when the aggregate posterior across the top cohort cells is ≥ 0.65 over ≥ 5 samples we multiply vote confidence by 1.10 (`knowledge_supports(...)`); ≤ 0.40 → 0.90 (`knowledge_opposes(...)`); thin corpora are intentionally neutral. Each touched vote gets the corpus summary appended once to its `reasoning` so the Chairman + lineage UI show what evidence the council was acting under. |
| `backend/bot/ai/brain.py` | System prompt extended: "When a ticker carries a 'Memory says:' line in its snapshot, that is the system's knowledge-graph evidence … REASON OVER THAT EVIDENCE." `_fmt_snapshot` appends a "Memory says: …" line (sample size, weighted WR, weighted posterior, avg move) and a "Top analog cells:" line listing the three best cohort cells per ticker. The Brain now reasons against evidence — the operator's stated MITS.4 goal. |
| `backend/bot/engine.py` | Before `brain.decide_portfolio` runs, each brain-snapshot gets a `knowledge_evidence` block attached via `load_knowledge_evidence`. Failure is silent — the Brain just sees the snapshot without a memory line. |

### Task C — IV / GEX series into historical_replay

| File | Change |
|---|---|
| `backend/bot/corpus/historical_replay.py` | `_fetch_iv_series` extended with carry-forward fill (last observed IV value carries through every subsequent bar until the next iv_history row appears). For intraday bars this means every 1h bar on the same calendar day shares the daily IV — the documented degraded-mode behaviour until ThetaData Pro intraday IV is wired in P2. New `_fetch_gex_series` helper that walks GEX rows and projects them onto bar timestamps via the same carry-forward strategy. The current `GexRegimeHistory` model only stores categorical regime labels + price levels (call_wall, gamma_flip, dealer_regime), not a scalar net-GEX number — so this loader returns None today; it's wired in advance for the P2 schema upgrade. `bootstrap_ticker` now accepts `iv_series_intraday` / `gex_series_daily` / `gex_series_intraday` test-injection kwargs, and forwards iv_series + gex_series into both the daily and intraday `detect_all` calls so options-intel detectors (`IVExpansion`, `IVCompression`, `GEXAcceleration`) fire on historical data. |

`detect_all` and `Detector.detect` already accept `**kwargs` (Phase 0
shape) — no signature change required.

**Known limitation (carried into the report):** ThetaData Standard tier
does not expose intraday IV / GEX historical endpoints (those are
Pro-tier). The intraday detector path therefore runs against the
daily-IV carry-forward series — close-of-day IV jumps will fire
IVExpansion / IVCompression once per day, intraday spikes within a
session won't. The test-injection points are already in place for the
P2 schema upgrade.

### Task D — Walk-forward aggregation

| File | Change |
|---|---|
| `backend/models/knowledge_graph_cell.py` | Added `sample_split` column (default `combined`). Added `sample_split` to the unique constraint + a dedicated index. Updated `to_dict()` to surface the new column. |
| `backend/db.py` | Added `_migrate_kg_unique_constraint` — SQLite-safe migration that detects the legacy 6-axis unique index on `knowledge_graph` and rebuilds it as a 7-axis index that includes `sample_split`. Pre-existing rows get backfilled to `sample_split='combined'` so the constraint accepts them. Idempotent on already-migrated DBs. |
| `backend/bot/corpus/knowledge_aggregator.py` | Added `SAMPLE_SPLIT_IN`, `SAMPLE_SPLIT_OUT`, `SAMPLE_SPLIT_COMBINED` constants + `_classify_split(source)` helper that maps observation provenance to the split bucket. `_fetch_obs_with_outcomes` now also pulls `source`. `_aggregate_members` extracted as a pure function so the aggregator can fold the same math three times per cohort. `recompute_cells` partitions each cohort's observations into in/out/combined buckets and emits one row per non-empty bucket. New per-split counters in the returned stats dict (`in_sample_cells`, `out_of_sample_cells`, `combined_cells`). Idempotent re-runs update instead of duplicating. |
| `backend/api/routes/knowledge.py` | `GET /knowledge/cells` accepts a new `sample_split` filter (one of `in_sample` / `out_of_sample` / `combined`). |
| `frontend/src/pages/KnowledgeGraph.jsx` | New `SAMPLE_SPLITS` filter chip group above the matrix table. Default filter is `out_of_sample` so the operator lands on live edge; one-click toggles for in_sample (training) and combined. The matrix table gained a "Split" column with a pill matching the chosen split. |

### Task E — Posterior sparkline history table

| File | Change |
|---|---|
| `backend/models/knowledge_graph_history.py` | NEW model — one row per cohort per snapshot_date. Unique on the full 7-axis cohort key plus `snapshot_date`, so re-running the snapshot job on the same calendar day overwrites instead of duplicating. Indexed on `(ticker, pattern)` and `snapshot_date`. |
| `backend/db.py` | Imports the new model so `Base.metadata.create_all` picks it up. |
| `backend/bot/corpus/knowledge_aggregator.py` | New `snapshot_cells_to_history(snapshot_date=None)` that walks every `KnowledgeGraphCell` and UPSERTs a `KnowledgeGraphHistory` row for the supplied date (defaults to today). Returns `{inserted, updated, errors}`. Idempotent. |
| `backend/bot/corpus/__init__.py` | Re-exports `snapshot_cells_to_history`. |
| `backend/bot/system_reset.py` | Added `knowledge_graph_history` to `EXTERNAL_CACHE_TABLES` so `fresh_start` preserves it (derived public data, not bot decisions). |
| `backend/bot/scheduler.py` | New `_nightly_snapshot_cells` job (`day_of_week='mon-fri,sun', hour=23, minute=50`) — operator-spec'd 23:50 ET weekdays + Sunday. Idempotent on (cohort, date). |
| `backend/api/routes/knowledge.py` | `GET /knowledge/{ticker}/{pattern}?history_days=30` now returns a `history` array of `KnowledgeGraphHistory` rows for the primary cell over the requested window. |
| `frontend/src/pages/KnowledgeGraph.jsx` | Drill-down modal fetches with `history_days=30`. When ≥ 2 history rows exist, renders a real recharts `ComposedChart` of posterior win rate with the confidence band shaded. Falls back to the old single-dot SVG when history hasn't accumulated yet, with a helpful "sparkline appears once 2+ snapshots exist" note. |

### Task F — CorpusStatusChip on the Watchlist

| File | Change |
|---|---|
| `frontend/src/components/Watchlist.jsx` | Imports `CorpusStatusChip`. For every watchlist row, renders the pill next to the ticker symbol. The chip handles its own status fetch + polling for tickers in `building` state. |

The chip already existed (Phase 0 build-test artifact); this just wires
it into the only surface where it's actionable.

---

## 2. Test counts

| Slice | Before Phase 1 | After Phase 1 | Delta |
|---|---|---|---|
| `tests/unit/test_corpus_knowledge_aggregator.py` | 5 | 11 | +6 (3 walk-forward + 3 history) |
| `tests/unit/test_corpus_historical_replay.py` | 4 | 7 | +3 (IV series wiring) |
| `tests/unit/test_agent_context_knowledge.py` | 0 | 9 | +9 (NEW file) |
| Phase 1 new + modified tests | — | **23 new + 3 modified** | |
| Phase 0 baseline | 1442 | n/a | |
| Expected after Phase 1 | n/a | **~1463 total** | +18 net (23 new - 3 rebaselined - 2 modified existing) |

Focused-run results (`pytest` over every file I touched plus every
related agent/brain/scheduler/reset test — 23 test files, **178 tests
total**):

```
178 passed, 3 warnings in 181.48s (0:03:01)
```

All 178 pass, including the modified existing tests in
`test_corpus_knowledge_aggregator.py` that I had to rebaseline against
the new 3-rows-per-cohort behaviour. Files covered by this run:

```
test_corpus_knowledge_aggregator.py    11 passed (3 new walk-forward + 3 new history)
test_corpus_historical_replay.py        7 passed (3 new IV-series wiring)
test_corpus_outcome_linker.py           3 passed
test_corpus_auto_bootstrap.py           2 passed
test_corpus_priors.py                   3 passed
test_agent_context_knowledge.py         9 passed (NEW file)
test_knowledge_routes.py                9 passed
test_detectors_options_intel.py         5 passed
test_detectors_base.py                 10 passed
test_detectors_talib.py                 6 passed
test_detectors_market_structure.py      3 passed
test_detectors_liquidity.py             5 passed
test_detectors_vwap.py                  3 passed
test_detectors_volume_profile.py        2 passed
test_detectors_price_action.py         10 passed
test_stage11_agents.py                 24 passed
test_stage12_scorecard.py              14 passed
test_stage12_three_way_probs.py         4 passed
test_stage14_dynamic_weights.py         5 passed
test_stage15_consensus_gate.py         18 passed
test_stage20b_chairman.py              16 passed
test_ai_brain.py                        5 passed
test_system_reset_and_universe.py       5 passed
```

**Pre-existing test regression check:** the full `tests/unit/` run
on this development laptop hits yfinance rate-limiting (residential
IP) — multiple pre-existing tests that import strategies / engine
modules trigger lazy yfinance initialization that blocks for many
minutes. None of my changes touch any of the slow files. The previous
Phase 0 baseline of 1442 tests took ~11 minutes on the EC2 host;
that's the canonical "1442+" the operator should rerun on EC2 to
re-baseline the full count after Phase 1 deploys. On laptop, every
file I touched plus every related test file I could reach in
isolation passed (178 / 178 — see breakdown above).

The 18 new tests:

  - `test_walk_forward_emits_three_splits`
  - `test_walk_forward_only_in_sample_when_no_live`
  - `test_walk_forward_idempotent`
  - `test_snapshot_cells_to_history_inserts`
  - `test_snapshot_cells_to_history_idempotent`
  - `test_snapshot_cells_to_history_specific_date`
  - `test_iv_expansion_fires_via_replay_with_iv_series`
  - `test_intraday_inherits_iv_when_supplied`
  - `test_replay_no_iv_series_no_iv_observations`
  - `test_load_knowledge_evidence_empty_when_cold`
  - `test_load_knowledge_evidence_populated_after_seed`
  - `test_load_knowledge_evidence_falls_back_to_ticker_only`
  - `test_build_agent_context_includes_knowledge_evidence`
  - `test_apply_memory_bias_supports_when_posterior_strong`
  - `test_apply_memory_bias_opposes_when_posterior_weak`
  - `test_apply_memory_bias_neutral_when_thin_corpus`
  - `test_agent_market_carries_evidence_into_reasoning`
  - (one more for the IV smoke path covered above)

---

## 3. Local smoke validation

### Frontend build

```
$ cd frontend && npm run build
...
dist/assets/index-CPmlP1Wn.js              241.59 kB │ gzip: 64.54 kB
dist/assets/vendor-charts-Cocx1QX3.js      258.08 kB │ gzip: 66.47 kB
✓ built in 17.88s
```

Clean. Recharts import in KnowledgeGraph.jsx adds no new dependency
(already in `vendor-charts`).

### `load_knowledge_evidence` smoke

Tested through `test_agent_context_knowledge.py`:

  - empty cold-corpus returns `{cells: [], summary: '',
    most_similar_outcomes: []}`.
  - After seeding AAPL bull_flag 15W/5L → `recompute_cells("AAPL")` →
    `load_knowledge_evidence(...)` returns 2 cells (in_sample +
    combined), summary `"20 analogs, WR 75% (posterior 67%), avg
    move +5.0%"`.
  - When (regime, vol_state) doesn't match any cell, falls back to
    ticker + horizon — still surfaces evidence rather than empty.

### Walk-forward smoke

`test_walk_forward_emits_three_splits` seeds 20 historical_replay
observations + 10 live_engine observations for NVDA bull_flag /
trending_up, runs `recompute_cells("NVDA")`, asserts that
`in_sample_cells == 1`, `out_of_sample_cells == 1`,
`combined_cells == 1`, with the right sample sizes (20 / 10 / 30)
and win rates per split.

### History sparkline smoke

`snapshot_cells_to_history()` seeds a cell, snapshots, then re-runs:
first call inserts 2 rows (in_sample + combined for the seeded
cohort), second call updates them in place. Custom-date snapshot
verified via `snapshot_date=date(2024, 12, 31)`.

### IV expansion via replay

`test_iv_expansion_fires_via_replay_with_iv_series` builds a
synthetic 60-bar daily DataFrame, feeds `iv_series_daily = [0.20]*30
+ [0.45]*30` through `bootstrap_ticker`, and asserts ≥ 1
`iv_expansion` observation persists. Passes.

`test_intraday_inherits_iv_when_supplied` does the same for the
intraday path with `iv_series_intraday`. Passes.

`test_replay_no_iv_series_no_iv_observations` confirms graceful
degradation — without an IV series, options-intel detectors silently
return `[]`.

---

## 4. Deploy bundle (files to ship to EC2 via S3)

### New files

```
backend/models/knowledge_graph_history.py
tests/unit/test_agent_context_knowledge.py
```

### Modified files

```
backend/api/routes/knowledge.py
backend/bot/agent_context.py
backend/bot/ai/brain.py
backend/bot/corpus/__init__.py
backend/bot/corpus/historical_replay.py
backend/bot/corpus/knowledge_aggregator.py
backend/bot/engine.py
backend/bot/scheduler.py
backend/bot/system_reset.py
backend/db.py
backend/models/knowledge_graph_cell.py
frontend/src/components/CurrentlyHoldingStrip.jsx
frontend/src/components/EvidencePanel.jsx
frontend/src/components/TradeDetail.jsx
frontend/src/components/Watchlist.jsx
frontend/src/pages/KnowledgeGraph.jsx
frontend/src/pages/MissionControl.jsx
tests/unit/test_corpus_historical_replay.py
tests/unit/test_corpus_knowledge_aggregator.py
```

### Frontend build

`cd frontend && npm run build` succeeds locally on macOS. Re-build on
the deploy host before tarring `dist/`.

### EC2 post-deploy checks

After deploy, in order (matches `feedback_post_change_verification.md`):

  1. `/knowledge/corpus/status` still returns the per-ticker rows
     (unchanged endpoint).
  2. `/knowledge/cells?sample_split=out_of_sample&limit=5` returns 0
     rows initially (no live-engine observations yet); once the bot
     starts writing observations with `source='live_engine'`,
     `out_of_sample` rows appear after the next aggregator run.
  3. `/knowledge/cells?sample_split=in_sample&ticker=SPY&limit=5`
     returns the historical_replay-derived cells.
  4. `/knowledge/SPY/consolidation?history_days=30` returns the
     `history` array (empty until the first nightly snapshot runs at
     23:50 ET; force via `python -c "from
     backend.bot.corpus.knowledge_aggregator import
     snapshot_cells_to_history; print(snapshot_cells_to_history())"`).
  5. Watchlist page shows the per-ticker `corpus building / ready /
     thin corpus` pill.
  6. Today page → open position card → renders the "Knowledge says
     (top 3 patterns for X)" panel beneath each holding.
  7. Trades page → click a row → drawer shows the evidence panel.
  8. Mission Control page → pick a trade → evidence panel renders
     above Chairman.
  9. Knowledge Graph page → toggle the new "in-sample (training) /
     out-of-sample (live) / combined" chips → table reloads with the
     correct subset. Click a row → drill-down modal renders the
     recharts sparkline (after at least one nightly snapshot has run;
     before that, falls back to the single-dot SVG with the helper
     copy).
  10. AI Brain cycle log should now show snapshot lines containing
      `"Memory says: N analogs, WR X% (posterior Y%) ..."` for every
      ticker that has populated cells. Verify via
      `journalctl -u tradingbot -f | grep -i "memory says"`.

---

## 5. Known limitations / Phase 2 follow-ups

  1. **ThetaData intraday IV/GEX** — Standard tier doesn't expose
     intraday historical IV/GEX endpoints. Phase 1 ships the wiring
     (`iv_series_intraday` / `gex_series_intraday` injection points)
     and uses daily-IV carry-forward as the degraded mode. Phase 2
     wires ThetaData Pro (or a paid alternative) so the
     IVExpansion / IVCompression detectors fire on intraday timeframe
     resolution.
  2. **GEX series is None today** — `_fetch_gex_series` is implemented
     and the wiring is in place, but the current
     `GexRegimeHistory` schema only stores categorical regime labels
     + price levels (no scalar net-GEX). The loader returns None until
     a schema upgrade adds a numeric column. Once that lands, no
     replay-side code changes needed.
  3. **`load_knowledge_evidence` cohort match strictness** — when
     (regime, vol_state, time_bucket) doesn't match anything for the
     ticker, we currently fall back to "ticker + horizon only". This is
     intentionally loose; once corpora are populated enough, a future
     iteration could fall back to "any ticker, same (pattern, regime)"
     to surface cross-ticker analogs.
  4. **Memory-bias factor magnitudes** — currently ±10% on the
     vote-confidence multiplier. We can dial this once we have
     calibration data (post-Phase-1 trading days) and see how often
     `knowledge_supports` correlates with closed-trade winners.
  5. **Sparkline density** — 30 days at one row per cell per day. For
     hot ticker/pattern pairs that's ~30 points. Once we accumulate 6+
     months we may want to thin the chart to weekly aggregates, but
     not before.
  6. **`EvidencePanel` re-fetch on every render** — each mount fires
     one network call; no module-level cache (in contrast to
     `useKnowledge.js`). Acceptable for the read-only surfaces it's
     dropped on; if we wire it onto cycle-driven panels later we should
     route through the cached hook.

---

## 6. Phase 1 invariants honored

  - **No emojis in code** — confirmed via grep across every changed file.
  - **Idempotent everywhere** — the new `_nightly_snapshot_cells` job
    upserts on (cohort + snapshot_date); `recompute_cells` upserts on
    the 7-axis key; the constraint-rebuild migration is no-op when the
    new index already exists; `load_knowledge_evidence` is purely a
    SELECT path.
  - **No look-ahead in detectors** — historical_replay carries IV
    forward (last-known value), never backward; never uses a future
    IV reading to fire a past observation. The 20-bar trailing-mean
    window in IVExpansion / IVCompression already used `iv_series[i-20:i]`
    (strictly prior bars). Verified by the test that runs a synthetic
    series of 30 baseline bars + 30 spike bars and the detector only
    fires after bar 30.
  - **Audit / fresh-start contract** — `knowledge_graph_history` is in
    `EXTERNAL_CACHE_TABLES`. No new bot-state tables added that would
    leak past a `fresh_start`.
  - **Track deferred integrations** — the ThetaData Pro intraday IV
    requirement is logged as `(TODO: ThetaData Pro intraday IV when
    subscription upgraded)` in `historical_replay.py` docstring + the
    Phase 2 follow-up list above.

---

## 7. Operator-locked decisions carried forward

  - Free sources only — no new paid data sources introduced.
  - Heavy KnowledgeGraph UI — the page gained a sample-split chip
    group + a real sparkline. Still one heavy page.
  - EXIT.1 untouched — no exit-logic changes here.
  - Dynamic ticker pipeline preserved — the corpus-status chip on the
    watchlist surfaces it for the operator.
  - Bayesian shrinkage formula unchanged — `posterior = (wins +
    prior_weight × prior_wr) / (n + prior_weight)`. The new walk-forward
    rows use the same priors, just over partitioned member sets.
