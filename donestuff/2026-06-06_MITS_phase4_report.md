# MITS Phase 4 — completion report

**Date:** 2026-06-06
**Scope:** five concrete follow-ups that Phase 3 flagged.
**Status:** All five shipped. 1665 unit tests passing (was 1628 + 37 new).
**Frontend build:** clean.

---

## 1. File-by-file change summary

### New files

| Path                                                            | Purpose                                                                                |
|-----------------------------------------------------------------|----------------------------------------------------------------------------------------|
| `backend/bot/data/bars.py`                                      | Unified ThetaData→yfinance bar fetcher (P4.3).                                         |
| `backend/bot/detectors/pine_custom.py`                          | `PineCustomDetector` runtime + registry builder for Pine-imported scripts (P4.2).      |
| `tests/unit/test_detector_params_propagation.py`                | 5+ detector families honoring `params` overrides (P4.1).                               |
| `tests/unit/test_pine_custom_detector.py`                       | MACD/RSI Pine import firing + registry rebuild + endpoint contract (P4.2).             |
| `tests/unit/test_bars_fetcher.py`                               | ThetaData success / yfinance fallback / shape parity / analysis-route surfacing (P4.3).|
| `tests/unit/test_chain_strike_in_analysis.py`                   | chain_strike integration + snap_fallback tagging on both routes (P4.4).                |
| `tests/unit/test_eod_catchup.py`                                | `most_recent_trading_day`, weekday/holiday walk-back, run-or-skip semantics (P4.5).    |

### Modified files

| Path                                              | Change                                                                                  |
|---------------------------------------------------|-----------------------------------------------------------------------------------------|
| `backend/bot/detectors/__init__.py`               | Added Pine custom family + `rebuild_registry()`. (P4.2)                                 |
| `backend/bot/detectors/price_action.py`           | `default_params()` + `params` kwarg on all 8 detectors. (P4.1)                          |
| `backend/bot/detectors/market_structure.py`       | `default_params()` + `params` kwarg on BOS + CHOCH. (P4.1)                              |
| `backend/bot/detectors/liquidity.py`              | `default_params()` + `params` kwarg on LiquiditySweep + StopHunt. (P4.1)                |
| `backend/bot/detectors/vwap.py`                   | `default_params()` + `params` kwarg on VWAP reclaim + rejection. (P4.1)                 |
| `backend/bot/detectors/volume_profile.py`         | `default_params()` + `params` kwarg on HVN + LVN. (P4.1)                                |
| `backend/bot/detectors/options_intel.py`          | `default_params()` + `params` kwarg on IVExpansion/Compression + GEXAcceleration. (P4.1)|
| `backend/bot/detectors/talib_patterns.py`         | Min-strength tunable, accepts `params` kwarg. (P4.1)                                    |
| `backend/api/routes/detectors.py`                 | Import-pine returns `will_fire_next_cycle`, calls `rebuild_registry()`. (P4.2)          |
| `backend/api/routes/analysis.py`                  | Shared bar helper + `bar_source`; chain_strike for suggested actions; legacy shims kept. (P4.3+P4.4) |
| `backend/bot/eod_analysis.py`                     | Shared bar helper; chain_strike on heuristic + Claude suggested actions. (P4.3+P4.4)    |
| `backend/bot/scheduler.py`                        | Sunday 10:00 / Monday 06:00 ET catch-up jobs + `most_recent_trading_day()` helper. (P4.5) |
| `frontend/src/pages/StockAnalysis.jsx`            | `bar_source` pill near chart header + `strike_source` micro-label inside action card.   |
| `frontend/src/pages/Tomorrow.jsx`                 | `strike_source` micro-label inside action card.                                         |

---

## 2. Test counts

| Phase     | Total passed | Notes                                                                |
|-----------|--------------|----------------------------------------------------------------------|
| Phase 3 (baseline) | 1628 | Shipped 2026-05-29.                                          |
| Phase 4 (new only) | +37  | Across 5 new test files.                                     |
| Phase 4 (total)    | **1665** | Zero regressions in the existing 1628.                   |

Final run: `1665 passed, 653 warnings in 386.86s` (no failures, no skips, no errors).

---

## 3. Local smoke validation per sub-task

| Sub-task | What was checked                                                                     | Result |
|----------|--------------------------------------------------------------------------------------|--------|
| P4.1     | `test_detect_all_passes_param_overrides_to_bull_flag` persists override + asserts the geometry that wouldn't fire on defaults DOES fire after override. | PASS |
| P4.2     | MACD-cross Pine source persisted → registry rebuild → `PineCustomDetector` appears in `DETECTOR_REGISTRY` → `detect_all` invokes it → observation emitted with `family='pine_custom'`. | PASS |
| P4.3     | `fetch_bars` happy/fallback/empty branches; `bar_source` field present in `/analysis/{ticker}` response. | PASS |
| P4.4     | `_resolve_suggested_strike` returns `('chain', K)` when chain_strike differs from snap, `('snap_fallback', K)` when they match or chain raises. Surfaces `direction` + `strike_source` + `dte_target` on the suggested_action dict. | PASS |
| P4.5     | `most_recent_trading_day(date(2026,6,7))==date(2026,6,5)`; `most_recent_trading_day(date(2026,5,26))==date(2026,5,22)` (Memorial Day skipped); job runs when 0 rows / no-ops when ≥1; both Sunday + Monday cron rows registered. | PASS |

---

## 4. Deploy bundle file list

```
backend/api/routes/analysis.py
backend/api/routes/detectors.py
backend/bot/eod_analysis.py
backend/bot/scheduler.py
backend/bot/detectors/__init__.py
backend/bot/detectors/price_action.py
backend/bot/detectors/market_structure.py
backend/bot/detectors/liquidity.py
backend/bot/detectors/vwap.py
backend/bot/detectors/volume_profile.py
backend/bot/detectors/options_intel.py
backend/bot/detectors/talib_patterns.py
backend/bot/detectors/pine_custom.py            # NEW
backend/bot/data/bars.py                         # NEW
frontend/dist/                                   # full built dist (already regenerated locally)
```

Tests (optional but recommended in the bundle for CI parity):
```
tests/unit/test_detector_params_propagation.py
tests/unit/test_pine_custom_detector.py
tests/unit/test_bars_fetcher.py
tests/unit/test_chain_strike_in_analysis.py
tests/unit/test_eod_catchup.py
```

No new SQLAlchemy models. No migrations needed. `system_reset.py`'s `EXTERNAL_CACHE_TABLES` already covers the `detector_config` and `eod_analysis` rows the Pine import + catch-up touch.

---

## 5. EC2 post-deploy verification checklist

1. **Service health:**
   ```
   sudo systemctl status trading-bot --no-pager | head -15
   sudo journalctl -u trading-bot -n 100 --no-pager | grep -E 'pine|catchup|bar_source' | tail -20
   ```
2. **Detector params propagation (P4.1):**
   ```
   curl -s http://127.0.0.1:8000/detectors | jq '.[] | select(.name == "bull_flag") | .default_params'
   # expect: {"min_thrust_pct":0.05, "max_tightness_ratio":0.5, "consolidation_bars":5}
   ```
3. **Pine import → live fire (P4.2):**
   ```
   curl -s -X POST http://127.0.0.1:8000/detectors/import-pine \
     -H 'content-type: application/json' \
     -d '{"name":"p4_smoke","source":"if ta.crossover(macd, signal)\n  strategy.entry(\"long\")"}' \
     | jq '.will_fire_next_cycle, .recognized, .rules'
   # expect: true, recognized list, rules list
   curl -s http://127.0.0.1:8000/detectors | jq '.[] | select(.name == "p4_smoke") | .family'
   # expect: "pine_custom"
   ```
4. **bar_source surfacing (P4.3):**
   ```
   curl -s http://127.0.0.1:8000/analysis/SPY?window=today | jq '.bar_source'
   # expect: "thetadata" (when terminal up) or "yfinance" (fallback) — never null
   ```
5. **chain_strike surfacing (P4.4):**
   ```
   curl -s http://127.0.0.1:8000/analysis/SPY?window=today \
     | jq '.theses | to_entries[] | .value.suggested_action | select(. != null) | {strike, strike_source, direction, dte_target}'
   # expect at least: strike_source in {"chain","snap_fallback"}, direction in {"long_call","long_put"}, dte_target=30
   ```
6. **Catch-up cron registration (P4.5):**
   ```
   sudo journalctl -u trading-bot --since '5 minutes ago' --no-pager | grep -iE 'cron|catchup'
   # On any Sunday 10:00 ET or Monday 06:00 ET reboot you should see "eod catchup: ... rows" or "running pass" lines.
   ```

---

## 6. Known limitations / Phase 5 follow-ups

* **Pine translator coverage stays narrow.** Only MACD cross (signal + zero), RSI threshold, MA cross, price-vs-MA cross are recognised. Anything else surfaces `will_fire_next_cycle: false` with the limitations message. (TODO: extend translator to cover Bollinger Bands, Stochastic, ATR-based stops.)
* **ThetaData bar paths are best-effort.** The v3 `/stock/history/eod` and `/stock/history/ohlc` endpoints work on Standard tier; the helper falls through cleanly when the terminal is down, but it doesn't yet differentiate "wrong tier" from "terminal unreachable" in the surfaced source — both look like `yfinance` fallback. (TODO: add a `bar_source_reason` field.)
* **`chain_strike` target_delta is fixed at 0.40.** This matches "buy a moderately ITM call" convention but is not yet operator-tunable per detector. (TODO: thread `target_delta` through detector-level config so a pinning play can use 0.50 and an OTM lottery can use 0.20.)
* **Catch-up doesn't replay the Tomorrow's Setup digest.** Only `EodAnalysis` rows are filled. Per the operator rule, the digest scheduler is the silent-no-op job and is left untouched. (TODO: wire a digest-aware re-trigger if/when the digest pipeline gets real consumers.)
* **Test for Pine RSI dip** is permissive (the synthetic series may not produce a textbook cross under threshold); it only confirms the detector doesn't raise. Tighten with a deterministic seed when possible.

---

## 7. Phase 4 invariants honored

* **No magic numbers in logic.** Every threshold a detector applies comes from `default_params()` and is overridable via `DetectorConfig.params_json`. The catch-up cron times are explicit hh/mm in the scheduler (intentional, not tunable today).
* **No synthetic data in paper DB tables.** Detector observations live in `market_observations`; EOD rows live in `eod_analysis`. Neither is in `PAPER_STATE_TABLES`.
* **Fresh-start contract.** No new models added, so `EXTERNAL_CACHE_TABLES` is unchanged. `detector_config` and `eod_analysis` already documented there.
* **Data-blame principle.** Suggested actions still gate on posterior ≥ 0.60 AND sample_size ≥ 30. chain_strike returns `snap_fallback` rather than fabricating a strike when the chain is unreachable.
* **Audit invariants.** No trade/position writes from any Phase 4 path. The catch-up pass only writes to `eod_analysis`.
* **No mention of Telegram / messaging digests / push notifications in any new code.** The Tomorrow's Setup notifier path is left untouched.

---

## 8. Status log entry

```
2026-06-06 — MITS Phase 4 ship. Five sub-tasks closed:
  P4.1  Detector params propagation across all 8 families (24+ detectors)
  P4.2  Pine custom detector runtime (MACD/RSI/MA/price-vs-MA cross) + registry rebuild on import
  P4.3  Shared ThetaData→yfinance bar fetcher (backend/bot/data/bars.py); /analysis carries bar_source
  P4.4  chain_strike integration in suggested actions, both /analysis + EOD; strike_source tag
  P4.5  Sunday 10:00 ET + Monday 06:00 ET EOD catch-up cron; most_recent_trading_day helper

Tests: 1628 → 1665 (+37 new, 0 regressions).
Frontend: clean build (`npm run build`).
Deploy: pending operator-side flow.
```
