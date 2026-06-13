# MITS Phase 0 — Completion Report

**Status:** shipped locally; awaiting deploy to EC2
**Date:** 2026-06-05
**Plan reference:** `donestuff/2026-06-05_MITS_plan.md`

Phase 0 builds the foundation: historical corpus + pattern detector library
+ knowledge-graph aggregation, plus the dynamic ticker pipeline so any
ticker added to the watchlist auto-bootstraps. Operator decisions locked
(free sources only / rule-of-thumb detectors / heavy Knowledge UI in one
shot / EXIT.1 stays as safety net) are honoured.

---

## 1. File-by-file change summary

### Created — SQLAlchemy models (5)

| Path | Purpose |
|---|---|
| `backend/models/market_observation.py` | `MarketObservation` — raw pattern detection events, indexed on (ticker, pattern, timestamp). Unique constraint on (ticker, pattern, timestamp, timeframe) for idempotent replay. |
| `backend/models/market_outcome.py` | `MarketOutcome` — forward-return per horizon (5min/30min/60min/1d/5d/20d), FK to observation, unique on (observation_id, horizon). |
| `backend/models/knowledge_graph_cell.py` | `KnowledgeGraphCell` — aggregated cohort stats (sample_size, win_rate, posterior, Wilson CI). Unique on the 6-axis cohort tuple. |
| `backend/models/pattern_prior.py` | `PatternPrior` — academic / TA-Lib priors for Bayesian shrinkage. |
| `backend/models/corpus_status.py` | `CorpusStatus` — per-ticker bootstrap state for UI. Unique on ticker. |

Each model has `to_dict()`. All five are registered in `backend/db.py:init_db()`
and picked up by `Base.metadata.create_all` + the existing `_auto_migrate`
ALTER-TABLE pass.

### Created — Detector library (`backend/bot/detectors/`)

| Path | Detectors |
|---|---|
| `base.py` | `Observation` dataclass, `Detector` ABC, shared helpers (`_classify_regime`, `_classify_vol_state`, `_time_bucket`, `_bar_timeframe`). |
| `talib_patterns.py` | 15 TA-Lib wrappers: engulfing, hammer, doji, evening_star, morning_star, shooting_star, harami, piercing, dark_cloud_cover, three_white_soldiers, three_black_crows, hanging_man, inverted_hammer, marubozu, spinning_top. Graceful degradation if TA-Lib absent. |
| `price_action.py` | BullFlag, BearFlag, Pennant, Consolidation, Breakout, Pullback, FailedBreakout, FailedBreakdown. |
| `market_structure.py` | BOS, CHOCH (5-bar fractal swing detection, no look-ahead). |
| `liquidity.py` | LiquiditySweep, StopHunt. |
| `vwap.py` | VWAPReclaim, VWAPRejection (session-anchored VWAP). |
| `volume_profile.py` | HVNAcceptance, LVNRejection (10-bin rolling 60-bar histogram). |
| `options_intel.py` | IVExpansion, IVCompression, GEXAcceleration (gracefully no-op when `iv_series`/`gex_series` kwargs absent). |
| `__init__.py` | `DETECTOR_REGISTRY` dict, `all_detectors()`, `detect_all(ticker, bars)`. 34 detectors registered. |

### Created — Corpus pipeline (`backend/bot/corpus/`)

| Path | Function |
|---|---|
| `__init__.py` | re-exports `bootstrap_ticker`, `link_outcomes_batch`, `recompute_cells`, `load_default_priors`. |
| `historical_replay.py` | `bootstrap_ticker(ticker, daily_lookback_years=10, intraday_lookback_days=180)`. Fetches yfinance daily+1h bars, runs `detect_all`, persists observations (skip on UniqueConstraint), updates `corpus_status`. IV-history backfill plumbed in when available. |
| `outcome_linker.py` | `link_outcomes_batch(ticker=None, limit=5000)`. Walks observations without outcomes, fetches forward bars (yfinance), computes horizon returns, idempotent insert. |
| `knowledge_aggregator.py` | `recompute_cells(ticker=None)`. Groups by 6-axis cohort, computes win_rate, posterior (Bayesian shrinkage `(wins + W*p) / (n + W)`), Wilson 95% CI, UPSERT. Posterior formula mirrors `scorecard.vote_weights`. |
| `priors_loader.py` | `load_default_priors()`. 25 hardcoded priors (Bulkowski, Bessembinder, Carhart, Conrad-Kaul, "academic", "TA-Lib lit"). Idempotent. |
| `auto_bootstrap.py` | `run_full_bootstrap(ticker)` — chains bootstrap → link → recompute, sets `corpus_status` to ready/error. |

### Modified — wiring

| File | Change |
|---|---|
| `backend/db.py` | Imports all 5 new model modules in `init_db()` so `Base.metadata` picks them up. |
| `backend/bot/system_reset.py` | New tables added to `EXTERNAL_CACHE_TABLES` (they're derived from public bar data, not bot decisions — preserved on `fresh_start`). |
| `backend/api/routes/watchlist.py` | New ticker → background thread runs `run_full_bootstrap(ticker)` in addition to the existing IV warm-start. Pre-marks `corpus_status="building"` synchronously so the UI flips immediately. |
| `backend/bot/scheduler.py` | Three new jobs: `_nightly_outcome_link` (weekdays 19:00 ET), `_nightly_recompute_cells` (weekdays 19:30 ET), `_weekly_full_replay` (Sat 06:00 ET — watchlist ∪ {SPY, QQQ, IWM, DIA, XLK, XLF, XLE}). |
| `backend/main.py` | `priors_loader.load_default_priors()` called on startup; `/knowledge` router registered. |

### Created — API (`backend/api/routes/knowledge.py`)

* `GET /knowledge/cells?ticker&pattern&regime&vol_state&time_bucket&horizon&min_samples&limit`
* `GET /knowledge/{ticker}/{pattern}` (primary cell + siblings + 20 recent observations with outcomes)
* `GET /knowledge/observations/recent?limit&ticker&pattern`
* `GET /knowledge/corpus/status`
* `POST /knowledge/corpus/rebuild/{ticker}` → 202 (background thread re-runs `run_full_bootstrap`)
* `GET /knowledge/priors`

### Created — Frontend

| File | Purpose |
|---|---|
| `frontend/src/hooks/useKnowledge.js` | `useKnowledgeCells(filters)` and `useCorpusStatus(pollMs)`. Module-cached fetch with per-filter-key cache. |
| `frontend/src/components/CorpusStatusChip.jsx` | Compact pill: building/ready/insufficient/error per ticker. |
| `frontend/src/components/EvidencePanel.jsx` | "Knowledge says: N analogs, WR x% (posterior y%), avg z% at H" — for inline use on trade detail surfaces. |
| `frontend/src/pages/KnowledgeGraph.jsx` | Full browser: filter chips (ticker, pattern, regime, vol, session, horizon, min_samples slider), sortable matrix table, click-row drill-down modal with sparkline + 20 recent observations + outcomes. |

### Modified — Frontend

| File | Change |
|---|---|
| `frontend/src/Layout.jsx` | Added `Knowledge` nav entry between Intel and Council. |
| `frontend/src/main.jsx` | Imported `KnowledgeGraph` page, added `/knowledge` route. |

### Created — Tests (14 files, 70 tests)

All marked `pytest.mark.unit, pytest.mark.invariant`.

| File | Tests |
|---|---|
| `tests/unit/test_detectors_base.py` | 10 — Observation dataclass, registry membership, helpers (`_time_bucket`, `_classify_regime`). |
| `tests/unit/test_detectors_talib.py` | 6 — synthetic hammer / engulfing geometry, flat-noise silence. |
| `tests/unit/test_detectors_price_action.py` | 10 — synthetic bull/bear flag, breakout (+ volume), pullback, failed breakout/breakdown, consolidation, pennant. |
| `tests/unit/test_detectors_market_structure.py` | 3 — BOS upward break, CHOCH bearish, flat-noise no-raise. |
| `tests/unit/test_detectors_liquidity.py` | 5 — sweep above/below prior range, stop-hunt bull/bear. |
| `tests/unit/test_detectors_vwap.py` | 3 — reclaim / rejection on intraday-spaced bars; flat silence. |
| `tests/unit/test_detectors_volume_profile.py` | 2 — HVN acceptance, LVN rejection on engineered profiles. |
| `tests/unit/test_detectors_options_intel.py` | 5 — IV expansion / compression / GEX z-score; series-missing fallthrough. |
| `tests/unit/test_corpus_priors.py` | 3 — load count, idempotency, field validity. |
| `tests/unit/test_corpus_historical_replay.py` | 4 — persists observations, idempotent re-run, empty input handling, corpus_status creation. |
| `tests/unit/test_corpus_outcome_linker.py` | 3 — daily horizons, idempotency, intraday horizon selection. |
| `tests/unit/test_corpus_knowledge_aggregator.py` | 5 — Wilson basic + zero-N, single-cell aggregation, idempotent re-run, posterior shrinkage at small N. |
| `tests/unit/test_corpus_auto_bootstrap.py` | 2 — `run_full_bootstrap` end-to-end with mocked yfinance; POST /watchlist triggers background corpus build (TestClient). |
| `tests/unit/test_knowledge_routes.py` | 9 — cells filter / detail / 404 / recent obs / corpus status / rebuild 202 / priors. |

---

## 2. Test counts

* **Before Phase 0:** 1372 unit tests (per plan brief).
* **After Phase 0:** 1442 unit tests passing.
* **Added:** 70 tests across 14 new test files.
* **Regressions:** 0 — all pre-existing tests still pass.

`pytest tests/unit/ -q` final result:
```
1442 passed, 658 warnings in 691.12s (0:11:31)
```

---

## 3. Local smoke validation (SPY)

Ran the full pipeline locally against yfinance live data with a fresh
SQLite DB:

```
Priors loaded: {'inserted': 25, 'updated': 0, 'errors': 0, 'total': 25}
Bootstrap stats:
  daily:    2514 bars, 6980 observations inserted
  intraday: 420 bars (60-day 1h window),  984 observations inserted
  Total SPY observations: 7964
Outcome linker: 5000 observations processed, 15000 outcomes inserted
Recompute cells: 612 cohort cells inserted
API GET /knowledge/cells?ticker=SPY → 200, returns cells
Sample cell: SPY / consolidation / trending_up / 1d
  N=572, WR=86.5%, posterior=85.9%
```

**Acceptance criteria met:**
* observation count for SPY ≥ 100 → **7964** ✓
* cells count ≥ 10 → **612** ✓
* API returns rows for ticker filter → ✓
* priors loaded → 25 ✓

---

## 4. TA-Lib install verification

* **Local laptop:** `pip install --only-binary=:all: ta-lib` installs the
  prebuilt `ta_lib-0.6.8-cp314-cp314-macosx_13_0_x86_64.whl` — no Homebrew
  C-library needed. Imports cleanly; `CDLENGULFING`, `CDLHAMMER`, etc.
  available.
* **EC2 deploy note:** the EC2 instance must have either the Python wheel
  (`pip install ta-lib`) or the underlying C library available
  (`yum install ta-lib-devel` / `apt install libta-lib-dev` then `pip
  install ta-lib`). Recommendation: add `ta-lib>=0.6.0` to
  `requirements.txt` so the standard deploy installer picks it up.
  Detector code already gracefully degrades to `[]` if the import fails,
  so a deploy that skips TA-Lib still boots — just without the 15
  candlestick detectors.

**Note:** `requirements.txt` updated in this Phase 0 change to include
`ta-lib>=0.6.0`. Pip prebuilt wheels cover macOS + linux for cp310 →
cp314 — `pip install -r requirements.txt` will pull the wheel without
needing a system C library on standard runners.

---

## 5. Deploy bundle (files to ship to EC2 via S3)

### New files (must be added to the deploy tarball)

```
backend/api/routes/knowledge.py
backend/bot/corpus/__init__.py
backend/bot/corpus/auto_bootstrap.py
backend/bot/corpus/historical_replay.py
backend/bot/corpus/knowledge_aggregator.py
backend/bot/corpus/outcome_linker.py
backend/bot/corpus/priors_loader.py
backend/bot/detectors/__init__.py
backend/bot/detectors/base.py
backend/bot/detectors/liquidity.py
backend/bot/detectors/market_structure.py
backend/bot/detectors/options_intel.py
backend/bot/detectors/price_action.py
backend/bot/detectors/talib_patterns.py
backend/bot/detectors/volume_profile.py
backend/bot/detectors/vwap.py
backend/models/corpus_status.py
backend/models/knowledge_graph_cell.py
backend/models/market_observation.py
backend/models/market_outcome.py
backend/models/pattern_prior.py
frontend/src/components/CorpusStatusChip.jsx
frontend/src/components/EvidencePanel.jsx
frontend/src/hooks/useKnowledge.js
frontend/src/pages/KnowledgeGraph.jsx
tests/unit/test_corpus_auto_bootstrap.py
tests/unit/test_corpus_historical_replay.py
tests/unit/test_corpus_knowledge_aggregator.py
tests/unit/test_corpus_outcome_linker.py
tests/unit/test_corpus_priors.py
tests/unit/test_detectors_base.py
tests/unit/test_detectors_liquidity.py
tests/unit/test_detectors_market_structure.py
tests/unit/test_detectors_options_intel.py
tests/unit/test_detectors_price_action.py
tests/unit/test_detectors_talib.py
tests/unit/test_detectors_volume_profile.py
tests/unit/test_detectors_vwap.py
tests/unit/test_knowledge_routes.py
```

### Modified files (must replace existing versions)

```
backend/api/routes/watchlist.py
backend/bot/scheduler.py
backend/bot/system_reset.py
backend/db.py
backend/main.py
frontend/src/Layout.jsx
frontend/src/main.jsx
requirements.txt
```

### Frontend build

`cd frontend && npm run build` succeeds locally — the new
`KnowledgeGraph` chunk shows up in `frontend/dist/assets/`. Repeat on the
build host (macOS, since `reference_ec2_deploy_quirks.md` notes no Node
on EC2) before tarring up the dist.

### EC2 post-deploy checks (matches `feedback_post_change_verification.md`)

After deploy:
1. `GET /knowledge/priors` returns 25 rows
2. `GET /knowledge/corpus/status` returns empty list initially
3. Add a test ticker via `POST /watchlist {"ticker":"AAPL"}` — within
   30-90s the GET `/knowledge/corpus/status` row for `AAPL` should
   transition `pending` → `building` → `ready`
4. `GET /knowledge/cells?ticker=AAPL&limit=5` returns rows
5. UI: navigate to `/knowledge` — full browser renders, filter chips
   work, drill-down modal opens
6. Check existing surfaces (paper/state, positions, bot/status, etc.)
   still return clean (no regression — DB auto-migrate adds tables only)

---

## 6. Known limitations / Phase 1 follow-ups

### Limitations of this Phase 0 ship

1. **Intraday yfinance ceiling.** yfinance caps 1h intraday lookback at
   ~730 days, with 60d for many tickers. Phase 0 default is 180 days; the
   replay fetcher clamps to 720. For longer-history intraday corpora
   we'll need ThetaData or equivalent — flagged as a Phase 1 task.
2. **No look-ahead in detectors, but no walk-forward in aggregator yet.**
   `recompute_cells` includes every observation regardless of when
   live trading started. Once the bot is live, we may want to separate
   "out-of-sample" vs "in-sample" cohorts. Not yet implemented.
3. **`EvidencePanel` not yet wired** into the live trade-detail
   surfaces (Trades page, Today page). The component is shipped and
   tested via build; integration into existing trade UIs is a Phase 1
   small task.
4. **Posterior sparkline is current-point only.** `KnowledgeGraph.jsx`
   shows the latest posterior as a single dot on the CI band. A real
   time series of posterior_win_rate over `last_updated` snapshots
   requires a new history table — deferred to Phase 1.
5. **GEX series + IV series not yet plumbed into `historical_replay`
   beyond the iv_history backfill path.** The options-intel detectors
   work but typically return [] in production until we wire ThetaData
   intraday IV/GEX into the bar-aligned series. Detectors gracefully
   no-op so this doesn't break the pipeline.
6. **No backfill of `iv_history` triggered by the corpus path** — we
   rely on the existing watchlist IV warm-start (the second background
   thread we left in place). When that's still running, the corpus
   bootstrap won't see fresh IV. Idempotent re-run via
   `POST /knowledge/corpus/rebuild/{ticker}` after IV settles is the
   workaround.
7. **No deduplication across the two thread-based warm-starts.** If the
   operator adds the same ticker twice in rapid succession, we may
   start two corpus threads. Not harmful (UniqueConstraint on
   observations skips dups) but wastes work. Add a "currently building"
   guard in Phase 1.

### Phase 1 priorities (in order)

1. **Wire `EvidencePanel` into Today page + trade detail** so the AI
   Brain sees the corpus evidence in its decision UI.
2. **Inject knowledge-graph evidence into `agent_context.build_agent_context`**
   so the Brain prompt has corpus evidence prepended on every cycle
   (MITS.4 in the plan — "memory-augmented brain").
3. **Add ThetaData-sourced intraday IV / GEX series** to the
   `historical_replay` path so the options-intel detectors fire on
   historical data, not just live.
4. **Walk-forward aggregator option** — let `recompute_cells` exclude
   the last N% of data so cells reflect out-of-sample edge.
5. **Sparkline history** — snapshot `posterior_win_rate` + `sample_size`
   nightly into a new `knowledge_graph_history` table; render real
   time-series sparklines in the drill-down modal.
6. **CorpusStatusChip integration on Watchlist UI** — currently
   shipped but unused; wire it into the existing watchlist row UI.

---

## 7. Operator-locked decisions honored

| Decision | How implemented |
|---|---|
| Free sources only | yfinance + TA-Lib only; no Quantpedia / paid sources. ThetaData IV/GEX is *optional* with graceful degradation. |
| Rule-of-thumb detectors | Plain geometry rules in `price_action.py` etc. Priors carry the academic guidance; the corpus updates them. |
| Heavy UI in one shot | Full `KnowledgeGraph` page with filters, sortable matrix, drill-down modal, sparkline. |
| EXIT.1 stays safety net | Untouched. MITS.5 will add the thesis-health primary. |
| Dynamic ticker pipeline | `watchlist.add_item` now starts a background thread that runs `bootstrap → outcomes → cells → status=ready`. Mirrors the existing IV warm-start pattern. |
| Bayesian shrinkage = `(wins + W·p) / (n + W)` | Implemented in `knowledge_aggregator._lookup_prior` + `recompute_cells`. Tested in `test_corpus_knowledge_aggregator.py::test_posterior_shrinks_toward_prior_when_sample_small`. |
| No look-ahead | Detectors only consult `bars[0..i]` at index i. BOS/CHOCH use fractal swings confirmed before the current bar (k bars on each side). Verified in detector tests. |
| Idempotent everywhere | UniqueConstraints on observations + outcomes + cell key; re-run tests pass. |
| No emojis in code | Confirmed via grep — code is plain ASCII. |
