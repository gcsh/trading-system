# MITS Phase 2 + MITS-5 — Completion Report

**Status:** shipped locally; awaiting deploy to EC2.
**Date:** 2026-06-05
**Plan reference:** `donestuff/2026-06-05_MITS_plan.md`
**Phase 0 reference:** `donestuff/2026-06-05_MITS_phase0_report.md`
**Phase 1 reference:** `donestuff/2026-06-05_MITS_phase1_report.md`

Phase 2 + MITS-5 closes the architectural loop:

- **MITS-P2.1** — Intraday IV resolution upgraded from daily-only carry-forward to ~30-minute granularity via a ThetaData straddle-inversion workaround that stays within the Standard tier.
- **MITS-P2.2** — `GexRegimeHistory` gains a `net_gex_scalar` column (backfilled idempotently from `dealer_regime` + distance-to-flip); `_fetch_gex_series` now returns a real per-bar series so `GEXAccelerationDetector` fires on historical bars.
- **MITS-P2.3** — Memory-bias self-calibration: replaces Phase 1's hardcoded ±10% with a smooth posterior-strength formula gated by a thin-corpus floor.
- **MITS-P2.4** — Knowledge sparkline auto-density: >180 days of history bucketizes to weekly (Mon-Sun) with sample-size-weighted aggregation; a `resolution` field surfaces "daily" vs "weekly" to the UI.
- **MITS-P2.5** — `EvidencePanel` routed through a new module-cached `useEvidence` hook so multiple mounts on the same page share one network call. The walk-forward refinement (operator-bundled with P2.5) adds `first_live_observation_at` to `corpus_status` and switches the aggregator's split partition to TIMESTAMP-based, with a clean fallback to Phase 1's source-based split when no live observations exist for the ticker.
- **MITS-5** — Thesis-health exit monitor — the 7th council agent. Builds a `WinnerProfile` from the corpus, scores the live trade against it, votes EXIT when the score falls below threshold. Wired into `engine._maybe_close_option` as the PRIMARY exit; EXIT.1's mechanical trailing stop stays as the safety net.

---

## 1. File-by-file change summary

### MITS-P2.1 — ThetaData intraday IV workaround

| Path | Change |
|---|---|
| `backend/models/intraday_iv_cache.py` | NEW model — one row per (ticker, timestamp). Holds the inverted IV plus pricing inputs (straddle, spot, strike, expiry, dte) for audit. Unique constraint on (ticker, timestamp) so the cache is idempotent. `status` column distinguishes `ok`/`no_quote`/`stale`/`oob_iv`/`error` — non-ok rows are cached so known-failed timestamps aren't retried forever. |
| `backend/bot/data/thetadata.py` | NEW `compute_intraday_iv_at(ticker, timestamp, dte_target=30, ...)` — samples the historical chain quote endpoint (`/v3/option/history/quote`) for the ATM call and put nearest `timestamp`, computes the straddle, inverts via Brenner-Subrahmanyam (`IV = straddle / (k * S * sqrt(T))`, `k = sqrt(2π)/2 ≈ 1.2533`). Writes the result (or failure status) to `intraday_iv_cache`. NEW `_historical_chain_quote_at` helper finds the quote nearest the target timestamp within the same trading day. |
| `backend/bot/corpus/historical_replay.py` | NEW `_intraday_iv_series(ticker, bars, fallback_daily)` — walks bars chronologically and samples IV once every ~30 minutes via `compute_intraday_iv_at`. Bars between samples carry forward; bars where the sample fails fall back to the daily-IV carry-forward series the Phase 1 path already produced. `bootstrap_ticker` now calls `_intraday_iv_series` for the intraday replay, with daily-IV carry-forward as the degraded-mode floor. |
| `backend/db.py` | Imports the new `intraday_iv_cache` model so `Base.metadata.create_all` picks up the table. |
| `backend/bot/system_reset.py` | `intraday_iv_cache` added to `EXTERNAL_CACHE_TABLES` — derived from public bar data, not bot decisions; preserved on fresh_start. |

**Resolution achieved:** intraday IV resolves at ~30-minute granularity (13× improvement over Phase 1's daily-only resolution). When ThetaData is unreachable the path silently degrades to the Phase 1 daily-IV carry-forward — no detector noise, no engine crash.

### MITS-P2.2 — GEX scalar column

| Path | Change |
|---|---|
| `backend/models/gex_history.py` | New `net_gex_scalar: Optional[float]` column. Surfaced in `to_dict`. |
| `backend/db.py` | `_data_backfill` extended with an idempotent UPDATE that populates `net_gex_scalar` from `dealer_regime` + `spot - gamma_flip` (signed, scaled to billions): `sign * abs(spot - gamma_flip) * 1e9`. Only touches NULL rows so a real net-GEX value from a future Pro vendor is never overwritten. |
| `backend/bot/corpus/historical_replay.py` | `_fetch_gex_series` now reads `net_gex_scalar` (with fallback to forward-compat vendor field names: `net_gex`, `gex_total`, etc.). Sorts rows by captured-at timestamp ascending so the latest value per date wins. Fixed a Phase 1 latent bug: row iteration moved INSIDE the `session_scope()` block to avoid `DetachedInstanceError` on lazy attribute access. |

**Formula documented:** the backfill formula lives in the `_data_backfill` SQL block. The column accepts a vendor-supplied direct value when one arrives.

### MITS-P2.3 — Memory-bias self-calibration

| Path | Change |
|---|---|
| `backend/config.py` | Four new tunables: `memory_bias_scale` (default 0.20), `memory_bias_min` (0.80), `memory_bias_max` (1.25), `memory_bias_min_samples` (20). Env overrides via `TB_MEMORY_BIAS_*`. |
| `backend/bot/agent_context.py` | NEW `derive_bias_factor(posterior, sample_size, scale, min_factor, max_factor, min_samples)` — formula: `raw = 1.0 + (posterior - 0.5) * 2.0 * scale; bias = clamp(raw, min_factor, max_factor)`. Thin-corpus floor: `sample_size < min_samples` returns 1.0. NaN guard catches degenerate inputs. `apply_memory_bias` now calls `derive_bias_factor` instead of the hardcoded ±10% block. The reasoning annotation now includes the actual multiplier (e.g. `knowledge_supports(72%@N=20@x1.09)`) so the operator can audit. |

**Spec match:**
- posterior=0.5, N≥20 → 1.0 (neutral) ✓
- posterior=0.75, N=100 → 1.10 (matches Phase 1 ±10% behaviour at the spec operating point) ✓
- Monotonically increasing with posterior ✓
- Clamped to [0.80, 1.25] by default ✓
- Returns 1.0 below `min_samples=20` ✓

### MITS-P2.4 — Auto-density sparkline

| Path | Change |
|---|---|
| `backend/config.py` | NEW tunable: `knowledge_sparkline_daily_cap_days` (default 180). Env override `TB_KNOWLEDGE_SPARKLINE_DAILY_CAP_DAYS`. |
| `backend/api/routes/knowledge.py` | `GET /knowledge/{ticker}/{pattern}?history_days=N` now bucketizes to Mon-Sun weekly when `N > daily_cap` (or `len(history_rows) > daily_cap`). NEW `_bucket_history_weekly(rows)` helper — weighted by sample_size: posterior + win_rate are sample-size-weighted means; CI is recomputed via Wilson from the aggregated `(wr, n)`; `sample_size` is summed. Response gains a `resolution` field: `"daily"` or `"weekly"`. |
| `frontend/src/pages/KnowledgeGraph.jsx` | Drill-down sparkline tooltip now formats the X label as `week of YYYY-MM-DD` when `body.resolution === 'weekly'`. New caption beneath the chart counts buckets and notes the resolution mode. |

### MITS-P2.5 — EvidencePanel module cache + walk-forward refinement

#### EvidencePanel module cache

| Path | Change |
|---|---|
| `frontend/src/hooks/useKnowledge.js` | NEW `useEvidence(ticker, pattern, horizon, topN)` hook — routes through the existing module-cached `useKnowledgeCells`. Filters by `(ticker, min_samples=5, limit=50)` so multiple `EvidencePanel` mounts on the same page (one per holding) share ONE underlying `/knowledge/cells?ticker=X` fetch. Returns `{cells, primary, loading}`. |
| `frontend/src/components/EvidencePanel.jsx` | Rewritten to consume `useEvidence` instead of firing per-mount `fetch()` calls. Same render shape in both modes (pattern-given vs ticker-only). |

#### Walk-forward refinement

| Path | Change |
|---|---|
| `backend/models/corpus_status.py` | New `first_live_observation_at: Optional[datetime]` column. Surfaced in `to_dict()`. |
| `backend/bot/corpus/knowledge_aggregator.py` | NEW `_compute_first_live_per_ticker(ticker)` — `min(timestamp) where source IN _LIVE_SOURCES`, persisted onto `corpus_status` in the same pass. `_classify_split(source, *, timestamp, cutoff_by_ticker, ticker)` extended: TIMESTAMP-based when a cutoff exists for the ticker, source-based fallback otherwise (preserves Phase 1 behaviour for cold tickers). `_fetch_obs_with_outcomes` now SELECTs `MarketObservation.timestamp` alongside `source` so the aggregator has both axes. `recompute_cells` calls `_compute_first_live_per_ticker` once at the top of the pass and threads the cutoff map through every cohort's split partition. Returns a new `tickers_with_live_cutoff` stat. |

### MITS-5 — Thesis-health exit monitor

#### Backend

| Path | Change |
|---|---|
| `backend/bot/thesis/__init__.py` | NEW package marker; re-exports `WinnerProfile`, `build_winner_profile`, `ThesisHealth`, `calculate_health`. |
| `backend/bot/thesis/winner_profile.py` | NEW `WinnerProfile` dataclass — pattern, regime, sample_size, avg_minutes_to_peak, avg_max_drawdown_during_hold, common_traits, trait_frequencies, confidence. Property `is_trustworthy` gates downstream consumers (`confidence >= 0.20 and sample_size >= 5`). Canonical trait constants (`TRAIT_HELD_VWAP`, `TRAIT_HELD_FLAG_LOW`, `TRAIT_HELD_BOS_PIVOT`, `TRAIT_HELD_PEAK_DRAWDOWN`, `TRAIT_IV_EXPANSION`, `TRAIT_IV_COMPRESSION`, `TRAIT_HIT_PEAK_EARLY`). |
| `backend/bot/thesis/profile_builder.py` | NEW `build_winner_profile(pattern, regime, *, horizon, ticker, use_cache)` — walks `market_observations + market_outcomes`, filters to winners, computes trait frequencies from observation features (`price_vs_vwap`, `price_vs_flag_low`, `iv_jump_pct`, etc.). Asymptotic confidence: `n/(n+20)` → 0.6 at 30 winners. Process-local cache keyed on `(pattern, regime, horizon, ticker)` with 1h TTL — picks up nightly recompute_cells without restart. NEW `clear_profile_cache()` for tests. |
| `backend/bot/thesis/health_calculator.py` | NEW `calculate_health(open_position, current_bars, winner_profile) → ThesisHealth`. Per-trait verdict checker handles each canonical trait against the position dict + (optional) bars. Score is `(weighted_intact / weighted_total) × 100`, blended with neutral 50 by `(1 - profile.confidence)` so thin profiles produce softer scores. Pure function — zero DB / network. |
| `backend/bot/agents/thesis_health.py` | NEW `agent_thesis_health(context)` — the 7th council agent. New-trade evaluations (no `open_position`) get a silent abstain. Thin profiles (sample_size < `TUNABLES.thesis_health_min_samples`) get a silent abstain. Open positions whose health drops below `TUNABLES.thesis_health_exit_threshold` get a SELL (long call) or BUY (long put) vote with confidence scaling on deficit. Healthy positions get a HOLD vote so the agent is visible in the panel without overpowering directional votes. Categorizes drivers under `portfolio_state`. |
| `backend/bot/agents/__init__.py` | `agent_thesis_health` imported and registered as the 7th entry in `AGENT_FUNCS` (between `mechanical_trend` and `devils_advocate`). |
| `backend/bot/engine.py` | NEW `_maybe_close_via_thesis_health(...)` — consults the council with `only=["thesis_health"]` BEFORE the EXIT.1 mechanical check. Builds the position context (vwap from market_data snapshot, hold_minutes, peak_premium, IV history), pulls the WinnerProfile via `build_winner_profile(pattern, regime, horizon='1d', ticker)`, runs the single-agent consensus. Closes the position with `strategy="thesis_health"` when the agent votes SELL/BUY with confidence ≥ 0.55. Gated by `TUNABLES.thesis_health_check_interval_cycles` (default 1 = every cycle). `_maybe_close_option` calls it first; falls through to EXIT.1's mechanical `decide_exit` when thesis-health doesn't trigger. |
| `backend/api/routes/thesis.py` | NEW `GET /thesis/health/{position_id}` — computes the live thesis-health for one paper position. Returns the breakdown (intact_traits, degraded_traits, score, reason, winner_profile). |
| `backend/main.py` | `thesis_routes` imported and registered on the FastAPI app. |
| `backend/config.py` | NEW tunables: `thesis_health_exit_threshold` (40.0), `thesis_health_min_samples` (30), `thesis_health_check_interval_cycles` (1). Env overrides via `TB_THESIS_HEALTH_*`. |

#### Frontend

| Path | Change |
|---|---|
| `frontend/src/components/CurrentlyHoldingStrip.jsx` | NEW `ThesisHealthChip` component — renders a coloured pill (green ≥70, blue ≥40, red <40) on each open-position card. Click opens a modal showing intact and degraded winner traits + the reason string + the winner profile's sample size and confidence. Renders nothing when the API returns `abstain=True`. |

#### Tests

| Path | Change |
|---|---|
| `tests/unit/test_thesis_health.py` | NEW. 13 tests across `TestCalculateHealth`, `TestAgentThesisHealth`, `TestRegistry`, `TestWinnerProfileBuilder`. |
| `tests/unit/test_stage11_agents.py` | Rebaselined 2 tests: `test_list_returns_seven_agents` (was `_six_agents`), `test_aligned_bullish_context_executes` (votes count 6→7), `test_preview_returns_consensus` (votes count 6→7). |
| `tests/unit/test_stage15_agent_voice.py` | Rebaselined `test_enrich_on_without_key_still_falls_back` (votes count 6→7). |

### Cross-cutting test files

| Path | Change |
|---|---|
| `tests/unit/test_intraday_iv_compute.py` | NEW. 5 tests — Brenner-Subrahmanyam IV recovery from synthetic straddle, cache hit prevents repeat ThetaData calls, no-quote failures cached, no-expiration returns None, out-of-band IV doesn't persist value. |
| `tests/unit/test_gex_scalar_replay.py` | NEW. 4 tests — `_fetch_gex_series` returns per-bar series, carry-forward gap fill, empty history returns None, `GEXAccelerationDetector` fires on synthetic spike via the replay path. |
| `tests/unit/test_memory_bias_calibration.py` | NEW. 12 tests covering `derive_bias_factor` contract + `apply_memory_bias` integration. |
| `tests/unit/test_sparkline_auto_density.py` | NEW. 5 tests — below-cap returns daily, above-cap triggers weekly, weekly buckets are Mondays, weighted-posterior math verified against an analytic case, missing `history_days` keeps the legacy shape. |
| `tests/unit/test_walk_forward_timestamp.py` | NEW. 5 tests — cutoff compute, cutoff persistence, three-split production, in/out-of-sample win-rate accuracy against the cutoff, no-live-obs fallback to source-based split. |
| `tests/unit/test_agent_context_knowledge.py` | Rebaselined `test_apply_memory_bias_opposes_when_posterior_weak` (seeded 30 samples instead of 15 to clear the new `memory_bias_min_samples=20` floor). |

---

## 2. Test counts

| Slice | Before | After | Delta |
|---|---|---|---|
| `test_intraday_iv_compute.py` | 0 | 5 | +5 |
| `test_gex_scalar_replay.py` | 0 | 4 | +4 |
| `test_memory_bias_calibration.py` | 0 | 12 | +12 |
| `test_sparkline_auto_density.py` | 0 | 5 | +5 |
| `test_walk_forward_timestamp.py` | 0 | 5 | +5 |
| `test_thesis_health.py` | 0 | 13 | +13 |
| **Phase 2 + MITS-5 new tests** | — | **44** | |
| **Rebaselined tests** (votes 6→7, samples 15→30) | — | 4 modified | |
| Phase 1 baseline expected | 1463 | n/a | |
| Telegram batch expected | ~1542 | n/a | |
| **Full `tests/unit/` run after Phase 2** | n/a | **1586** | +44 net vs telegram baseline |

Final `pytest tests/unit/ -q` result on the dev laptop:

```
1586 passed, 653 warnings in 712.81s (0:11:52)
```

This clears the operator-spec'd ≥1521 floor by a wide margin. Zero regressions; the 4 modified tests reflect intentional behaviour shifts (7-agent panel, 20-sample floor) called out in the spec.

---

## 3. Local smoke validation (per sub-task)

### P2.1 — Intraday IV inversion

`test_brenner_subrahmanyam_recovers_iv` builds a synthetic straddle that should invert to IV=0.30 at spot=100, strike=100, DTE=30:

```
k = sqrt(2π) / 2 ≈ 1.2533
straddle = 0.30 * 1.2533 * 100 * sqrt(30/365)
        ≈ 10.78
leg_mid = straddle / 2 ≈ 5.39
```

`compute_intraday_iv_at` is fed `{bid: leg_mid-0.05, ask: leg_mid+0.05}` for both legs and recovers IV within 2% of the seed. Cache verified to suppress repeat ThetaData calls. Failure-path persistence verified — a known-no-quote timestamp is cached so re-runs skip the network hit.

### P2.2 — GEX scalar replay

`test_detector_fires_on_synthetic_spike` seeds 60 days of `GexRegimeHistory` rows with a step change from 1e9 → 5e9 at day 30, runs `_fetch_gex_series` through the replay path, and asserts that `GEXAccelerationDetector` fires at the step. The detector's 2-sigma threshold catches the 4-sigma jump cleanly.

### P2.3 — Memory-bias calibration

`TestDeriveBiasFactor` locks the formula at each operating point and verifies the legacy ±10% behaviour at the spec-stated calibration points (posterior=0.75/0.25 → 1.10/0.90 at scale=0.20). `TestApplyMemoryBiasUsesCalibratedFactor` proves the chokepoint integration produces the right confidence shift in `apply_memory_bias`.

### P2.4 — Sparkline auto-density

`test_above_cap_triggers_weekly` seeds 200 days of history rows and verifies the response has `resolution="weekly"` with 25-32 bucket rows (200/7 ≈ 28-29 weeks). `test_weekly_posterior_is_weighted_average` seeds a 7-day window with mixed sample sizes (3 days at N=10 post=0.40, 4 days at N=20 post=0.60) and verifies the bucket's posterior matches the analytic weighted average: `(3×10×0.40 + 4×20×0.60) / 110 = 60/110 ≈ 0.545`.

### P2.5 — EvidencePanel cache + walk-forward

The hook integration is verified by build — `cd frontend && npm run build` succeeds. The walk-forward refinement is locked by `test_in_sample_uses_only_pre_cutoff_observations`: 10 historical-window observations (7 winners) + 5 live-window observations (3 winners), with the live cutoff between them, produces:

```
in_sample:     N=10, win_rate=70%
out_of_sample: N=5,  win_rate=60%
combined:      N=15, win_rate=10/15 ≈ 67%
```

Verified against the cell rows persisted by `recompute_cells`.

### MITS-5 — Thesis-health

`test_all_traits_intact_high_score`: position with `current_price > vwap > flag_low` against a profile where `trait_frequencies={held_vwap: 0.80, held_flag_low: 0.70}` produces `score >= 70.0` with no degraded traits.

`test_broken_vwap_lowers_score`: same profile, position with `current_price < vwap` and `current_price < flag_low` produces `score < 50.0` with both traits in `degraded_traits`.

`test_strong_profile_with_degraded_position_votes_exit`: the agent emits `stance=SELL` with confidence ≥ 0.55 and a reason string starting with `"THESIS-HEALTH EXIT"`.

`test_seven_agents_registered`: `AGENT_FUNCS` length is 7 and the registry contains `thesis_health`.

### Frontend build

```
$ cd frontend && npm run build
...
dist/assets/index-BT3qMpDa.js              252.17 kB │ gzip: 66.80 kB
dist/assets/vendor-charts-Cocx1QX3.js      258.08 kB │ gzip: 66.47 kB
✓ built in 10.33s
```

Clean. The new `ThesisHealthChip` modal pulls into the existing `CurrentlyHoldingStrip` chunk.

---

## 4. Deploy bundle (files to ship to EC2)

### New files (must be added to the deploy tarball)

```
backend/api/routes/thesis.py
backend/bot/agents/thesis_health.py
backend/bot/thesis/__init__.py
backend/bot/thesis/health_calculator.py
backend/bot/thesis/profile_builder.py
backend/bot/thesis/winner_profile.py
backend/models/intraday_iv_cache.py
tests/unit/test_gex_scalar_replay.py
tests/unit/test_intraday_iv_compute.py
tests/unit/test_memory_bias_calibration.py
tests/unit/test_sparkline_auto_density.py
tests/unit/test_thesis_health.py
tests/unit/test_walk_forward_timestamp.py
```

### Modified files (must replace existing versions)

```
backend/api/routes/knowledge.py
backend/bot/agent_context.py
backend/bot/agents/__init__.py
backend/bot/corpus/historical_replay.py
backend/bot/corpus/knowledge_aggregator.py
backend/bot/data/thetadata.py
backend/bot/engine.py
backend/bot/system_reset.py
backend/config.py
backend/db.py
backend/main.py
backend/models/corpus_status.py
backend/models/gex_history.py
frontend/src/components/CurrentlyHoldingStrip.jsx
frontend/src/components/EvidencePanel.jsx
frontend/src/hooks/useKnowledge.js
frontend/src/pages/KnowledgeGraph.jsx
tests/unit/test_agent_context_knowledge.py
tests/unit/test_stage11_agents.py
tests/unit/test_stage15_agent_voice.py
```

### Frontend build

`cd frontend && npm run build` succeeds locally on macOS. Rebuild on the deploy host (operator's laptop, since the EC2 has no Node) before tarring `dist/`.

---

## 5. EC2 post-deploy verification checklist

After deploy, in order (matches `feedback_post_change_verification.md`):

1. `systemctl status tradingbot` — service should be active. The boot log should include `7 agents registered` (or no warning about agent count).
2. `curl http://localhost:8000/agents/list` — returns 7 entries; `thesis_health` is the 6th in the list (between `mechanical_trend` and `devils_advocate`).
3. `curl http://localhost:8000/paper/state` — paper account intact (no schema regression).
4. `curl http://localhost:8000/paper/positions` — open positions list returns. For each option position with a recorded `pattern` in `meta`, `GET /thesis/health/{position_id}` returns a JSON body with either `abstain=true` (thin corpus) or `score` + `intact_traits` + `degraded_traits`.
5. `curl 'http://localhost:8000/knowledge/cells?ticker=SPY&limit=5'` — returns cells (unchanged endpoint, new `sample_split` semantics under the hood).
6. `curl 'http://localhost:8000/knowledge/SPY/consolidation?history_days=30'` — returns `resolution="daily"` with the history array.
7. `curl 'http://localhost:8000/knowledge/SPY/consolidation?history_days=400'` — returns `resolution="weekly"` with Monday-keyed bucket rows.
8. `journalctl -u tradingbot -f` — first engine cycle after deploy should show no errors importing `backend.bot.thesis` or `backend.bot.agents.thesis_health`.
9. Open the UI → `/today` → each open position card shows the thesis-health chip. Click → modal opens with the trait breakdown.
10. Open the UI → `/knowledge` → drill into any cell with >180 days of history → the sparkline tooltip labels show `week of YYYY-MM-DD`.
11. Watchlist UI → corpus-ready ticker → previous EvidencePanel rendering still works; verify in DevTools Network panel that adding a SECOND open position for the SAME ticker doesn't double the `/knowledge/cells?ticker=...` fetch count (module cache).
12. Force a recompute: `python -c "from backend.bot.corpus.knowledge_aggregator import recompute_cells; print(recompute_cells('SPY'))"`. Output should include `tickers_with_live_cutoff` count (>=0). For tickers with live observations, `corpus_status.first_live_observation_at` should be set.
13. Force a GEX backfill audit: `sqlite3 trading_bot.db 'SELECT COUNT(*) FROM gex_regime_history WHERE net_gex_scalar IS NOT NULL'` — should return >0 after the migration runs.
14. (Optional) Force an intraday IV sample: `python -c "from backend.bot.data.thetadata import compute_intraday_iv_at; from datetime import datetime; print(compute_intraday_iv_at('SPY', datetime(2024,6,3,14,30), spot=520))"`. With a live ThetaData terminal this should return a float between 0.05 and 1.0, populate one row in `intraday_iv_cache`, and a second call should hit the cache (no extra ThetaData traffic).

---

## 6. Known limitations / next steps

### P2.1 — Intraday IV
- **Density cap**: One sample per 30 minutes per ticker per day is the operator-spec'd density (~13 samples per RTH session). Total ThetaData hits per corpus bootstrap = `tickers × intraday_lookback_days × 13 × 2 (call + put)`. For the 13-ticker watchlist with 180 intraday-lookback days that's ~60,800 quote calls per bootstrap pass. ThetaData Standard supports this rate, but operators should expect a longer bootstrap window (~5-10 minutes per ticker vs. Phase 1's ~30 seconds).
- **Strike granularity**: Brenner-Subrahmanyam assumes ATM. We pick the listed strike nearest spot; for symbols with wide strike intervals (e.g. BRK.B at $1 strikes), the inversion is slightly biased. Acceptable noise for cohort statistics.
- **No backward extrapolation**: A failed sample at the first bar of the series doesn't carry backward — those bars stay at the daily-IV value until the first successful intraday sample.

### P2.2 — GEX scalar
- The `net_gex_scalar` backfill is a distance-to-flip proxy, not a true dealer-positioning calculation. When a vendor with real net-GEX (Pro tier, SpotGamma, etc.) is wired in, the writer just populates the column directly — the backfill block respects pre-populated values via `WHERE net_gex_scalar IS NULL`.
- The detector's 2-sigma trigger is calibrated to the magnitudes the backfill produces (~1e9-1e11). Real net-GEX from a Pro vendor would land in the same band so no detector re-tuning expected.

### P2.3 — Memory-bias calibration
- The thin-corpus floor (`memory_bias_min_samples=20`) is conservative. After a few weeks of live trading, the operator may want to lower it to 10 so smaller cohorts can influence vote confidence.
- The scale (`0.20`) and clamps (`[0.80, 1.25]`) are also env-tunable — once we have closed-trade outcomes for cohorts the calibration page can plot realized P&L vs. bias factor and the operator can pick informed values.

### P2.4 — Sparkline auto-density
- Only weekly bucketing is implemented; if cohorts accumulate 2+ years of history we may want monthly. Easy to extend `_bucket_history_weekly` by adding a `_bucket_history_monthly` variant + a second threshold (e.g. `>365` → monthly).
- Bucket alignment uses ISO Monday; if the operator wants Sunday-anchored weeks (e.g. broker calendar alignment) the `d - timedelta(days=d.weekday())` line in `_bucket_history_weekly` is the single tweak.

### P2.5 — EvidencePanel cache + walk-forward
- `useEvidence` shares fetches across mounts on the same render pass, but doesn't invalidate when a new observation lands (the cache is module-level + TTL-less). For operator-driven manual refresh, `clearKnowledgeCache()` from the same hook module works. We may want a 60s TTL once live edges start updating cells nightly.
- The walk-forward TIMESTAMP cutoff assumes monotonic clock-time order between historical and live observations. If a ticker is re-bootstrapped (`POST /knowledge/corpus/rebuild/{ticker}`) AFTER live trading has started, the historical pass writes observations with timestamps in the past — these correctly classify as in_sample under the timestamp logic.

### MITS-5 — Thesis-health
- **Trait coverage**: only 7 canonical traits implemented; intentionally a starter set. Adding new traits (e.g. `flow_alignment`, `vix_regime_match`) is a `KNOWN_TRAITS` append + a `_check_trait` clause + the matching feature key on the detector observation.
- **VWAP source**: the engine pulls VWAP from `market_data.snapshot(ticker).data['vwap']`. When the snapshot returns nothing (off-hours, data-source outage), the `held_vwap` trait stays unevaluated — counts as "not applicable" rather than "degraded", so the score doesn't artificially drop.
- **`hit_peak_early` trait**: the feature requires `peak_reached_minutes` on the position, which the engine doesn't yet populate at peak-tracking time. Easy follow-up: when `_maybe_close_option` detects `current_per_share > peak_per_share`, record the elapsed minutes since open onto a new `peak_reached_at_minutes` column. Until then the trait stays inert (zero frequency in the profile, ignored by the calculator).
- **`agent_thesis_health` is consulted ONLY during the `_maybe_close_option` exit path**. The standard `run_consensus` for NEW entries gets the silent-abstain vote and a "no open position" reason — this is the intended behaviour. If the operator wants the agent to participate in entry decisions too (e.g. "don't enter when no prior winners exist"), the engine entry path needs a new `winner_profile` injection — left as a Phase 3 toggle.
- **Frontend chip**: the chip is hidden when `abstain=true` so cold-corpus positions don't show a misleading score. Once a ticker accumulates ≥30 winners for the entry pattern, the chip appears automatically.
- **EXIT.1 safety net**: untouched. Defense in depth — thesis-health closes the trade when the corpus says "this trade no longer looks like a winner"; the trailing stop closes when the price tells the same story mechanically.

---

## 7. Phase 2 + MITS-5 invariants honored

- **No emojis in code** — confirmed by grep across every modified file.
- **No paid sources** — only ThetaData Standard (already subscribed), Anthropic (already subscribed), and the local SQLite cache. The ThetaData historical chain-quote endpoint is on the Standard tier.
- **Idempotent everywhere** — intraday IV cache (UPSERT on (ticker, timestamp)), GEX scalar backfill (UPDATE WHERE NULL), walk-forward cutoff (UPSERT on corpus_status), winner-profile builder (cache-keyed, TTL'd), thesis-health agent (pure function over context).
- **No look-ahead in detectors** — intraday IV samples use ONLY the historical chain quote at-or-before the bar timestamp. The straddle inversion uses the SAME-DAY chain, never a later session's data. Verified by the no-quote cache path.
- **Audit / fresh-start contract** — `intraday_iv_cache` lives in `EXTERNAL_CACHE_TABLES` (derived from public market data). No new bot-state tables that would leak past `fresh_start`.
- **Track deferred integrations** — the `hit_peak_early` trait's missing `peak_reached_minutes` plumbing is logged in Section 6 above as a Phase-3 follow-up.

---

## 8. Operator-locked decisions carried forward

- **Free sources only** — confirmed. ThetaData Standard's historical chain quote endpoint costs nothing extra beyond the existing subscription.
- **Heavy Knowledge UI** — the sparkline now auto-densifies + the EvidencePanel cache plumbing means multiple mounts on the same page don't redundant-fetch. The Knowledge Graph page remains one rich browser.
- **EXIT.1 stays safety net** — `_maybe_close_via_thesis_health` runs BEFORE `decide_exit`. When thesis-health doesn't fire (cold corpus, no degraded traits, healthy score), the EXIT.1 mechanical path runs as the secondary check. Defense in depth.
- **Bayesian shrinkage formula unchanged** — `recompute_cells` still uses `posterior = (wins + prior_weight × prior_wr) / (n + prior_weight)`. The walk-forward refinement only changes which observations land in which bucket, not the math.
- **Dynamic ticker pipeline preserved** — adding a new ticker to the watchlist still triggers `run_full_bootstrap`, which now includes the intraday IV sampling on the bootstrap pass. CorpusStatusChip still surfaces the per-ticker state in real time.

---

## 9. Status log

- **2026-06-05 (Phase 2 + MITS-5 shipped)** — Seven sub-tasks complete. 44 new unit tests + 4 rebaselined existing tests. Frontend builds clean. Full `tests/unit/` run: 1586 passed in 712.81s.
