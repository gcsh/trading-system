# MITS Phase 9 — Theory Studio + UX fixes

**Date:** 2026-06-08
**Scope:** Theory engine (5 modules), Knowledge page redesign (editable chart), GEX expiration dropdown, Today equity fallback, Lake health monitor

## 1. File-by-file changes

### Theory engine — `backend/bot/theories/`

  * `__init__.py` — registry mapping (`price_action`, `gann`, `fibonacci`, `ichimoku`, `pivots` → label).
  * `schema.py` — dataclasses for `Line`, `Marker`, `Zone`, `TheoryAnnotation` with `to_dict()` round-tripping.
  * `_zigzag.py` — shared ZigZag pivot detector (Achelis/Murphy spec).
  * `price_action.py` — Bulkowski + Murphy: triangle (asc/desc/symmetric), H&S, double top/bottom, flag, wedge, channel. Includes trailing-pivot trim to handle break-out pivots, wedge-vs-triangle disambiguation (flat-boundary → triangle).
  * `gann.py` — fan angles (1×1 / 1×2 / 1×3 / 1×4 / 2×1 / 3×1 / 4×1 / 1×8 / 8×1) with the exact colour convention from the operator reference image, 12-cycle time bars (30/45/60/90/120/144/180/240/270/360/540/720), 1/8 retracement levels.
  * `fibonacci.py` — Frost & Prechter retracement (0/23.6/38.2/50/61.8/78.6/100) + extension (127.2/138.2/161.8/200/261.8/423.6) grid.
  * `ichimoku.py` — Hosoda 1969 spec (Tenkan 9, Kijun 26, Senkou A/B with 26-bar forward displacement, Chikou 26-back, Kumo cloud); fixed cloud-zone bug pairing Span A/B by source bar index instead of list index.
  * `pivots.py` — floor pivots (PP/R1-R3/S1-S3) for daily/weekly/monthly with Person's exact formulas.

### Schema + tables
  * `backend/models/saved_theory_annotation.py` (new) — operator-edited overlay; `SavedTheoryAnnotation` row keyed on `(theory, ticker, window)`.
  * `backend/models/lake_health_alert.py` (new) — `LakeHealthAlert` with `kind`/`severity`/`detail_json`/`resolved_at` + `resolved_by`.
  * `backend/db.py` — register both new models so `Base.metadata.create_all` picks them up.
  * `backend/bot/system_reset.py` — add `saved_theory_annotations` + `lake_health_alerts` to `EXTERNAL_CACHE_TABLES` (fresh-start contract).
  * `backend/config.py` — add tunables: `theory_zigzag_pct=3.0`, `theory_cache_ttl=300`, `lake_alert_bronze_stale_hours=24`, `lake_alert_gold_stale_hours=48`, `lake_alert_write_failure_threshold=10`, `gex_expiration_buckets=[(0d,0),(1d,1),(5d,5),(7d,7),(14d,14),(30d,30),(60d,60),(all,45)]`.

### Routes
  * `backend/api/routes/theories.py` (new) — `GET /theories`, `GET /theories/{theory}/{ticker}`, `POST /theories/{theory}/{ticker}/save`, `DELETE /theories/{theory}/{ticker}/saved`. 5-min in-process cache keyed on (theory, ticker, window, params).
  * `backend/api/routes/heatseeker.py` — `expiration` query param on `/heatseeker/{ticker}` + `/heatseeker/regime`; new `GET /heatseeker/expirations` lists buckets.
  * `backend/api/routes/portfolio.py` — `equity_curve()` now supports `range=last_session` explicit + auto-fallback when `range=1d` returns zero rows; wrapped response shape includes `dataset_note` ("Showing 2026-06-05 session — markets closed today.").
  * `backend/api/routes/lake_status.py` — `GET /lake/health/alerts` (active or include-resolved) + `POST /lake/health/alerts/{id}/ack`.
  * `backend/main.py` — register `theories_routes.router`.

### GEX chain pipeline
  * `backend/bot/signals/gex.py` — `_clean(rows, max_dte=45)` accepts a configurable cutoff; `gex(ticker, *, max_dte=None)` threads the parameter through and uses a tuple `(ticker, max_dte)` cache key so adjacent buckets don't invalidate each other.

### Scheduler + monitoring
  * `backend/bot/monitoring/lake_health.py` (new) — `run_health_check()`. Four rules (bronze_stale, gold_stale, vector_shrink, write_failures), auto-resolve on cleared condition, `record_bronze_failure()` helper for in-process counter.
  * `backend/bot/scheduler.py` — new hourly `_lake_health_check` cron at minute 7.

### Frontend
  * `frontend/package.json` — add `lightweight-charts ^4.2.0`.
  * `frontend/src/hooks/useTheory.js` (new) — 60s TTL hook + registry hook.
  * `frontend/src/components/TheoryChart.jsx` (new) — TradingView lightweight-charts candlestick + volume + overlays (trendlines / horizontals / verticals / fans / markers / zones).
  * `frontend/src/pages/TheoryStudio.jsx` (new) — ticker × theory × window selectors, params panel, edit-mode toggle, save / reset / export PNG.
  * `frontend/src/pages/Knowledge.jsx` (new) — tabbed wrapper: Theory Studio + existing Knowledge Graph.
  * `frontend/src/main.jsx` — route `/knowledge` now points at `Knowledge`.
  * `frontend/src/pages/Heatseeker.jsx` — DTE-bucket dropdown (default `all`), URL-synced; re-fetches on change.
  * `frontend/src/pages/LakeStatus.jsx` — `HealthAlertsBanner` (red, active-count, per-alert "Acknowledge"); polls `/lake/health/alerts` every 30s.
  * `frontend/src/components/EquityCurve.jsx` — accept both legacy list and new wrapped `{snapshots, dataset_note}` shape; surface dataset_note under the title.

### Tests (4 new files)
  * `tests/unit/test_theories.py` — 15 tests: registry, ZigZag, descending triangle detection, double top, Gann unit + time-cycles, Fibonacci ratios, Ichimoku Tenkan formula + cloud colour, floor pivot exact formulas, schema round-trip, tunable default.
  * `tests/unit/test_gex_expiration.py` — 4 tests: label-to-DTE mapping, raw integer parsing, `_clean` DTE filtering, undated-row fallback.
  * `tests/unit/test_portfolio_last_session.py` — 3 tests: explicit `range=last_session` wrapping, `range=1d` fallback, legacy 1d list shape when intraday present.
  * `tests/unit/test_lake_health.py` — 5 tests: bronze_stale fires + auto-resolves, first-pass-no-shrink, shrink-on-drop, 24h trim, write-failures-on-threshold.
  * `tests/integration/test_theories_routes.py` — 3 tests: registry listing, Gann annotation through HTTP, save/restore overlay round-trip.

## 2. Theory math citations

| Module          | Citation in docstring |
| --------------- | --------------------- |
| `price_action`  | Bulkowski, "Encyclopedia of Chart Patterns" (3rd ed., Wiley 2021); Murphy, "Technical Analysis of the Financial Markets" (NYIF 1999). |
| `gann`          | W. D. Gann, "How to Make Profits in Commodities" (1942); "45 Years in Wall Street" (1949); Krausz, "A W. D. Gann Treasure Discovered" (1998). |
| `fibonacci`     | Frost & Prechter, "Elliott Wave Principle" (10th ed., New Classics 2005); Fischer, "Fibonacci Applications and Strategies for Traders" (Wiley 1993). |
| `ichimoku`      | Goichi Hosoda, "一目均衡表" (1969); Patel, "Trading with Ichimoku Clouds" (Wiley 2010). |
| `pivots`        | Person, "A Complete Guide to Technical Trading Tactics" (Wiley 2004), Chapter 4 (Floor Pivot Point Indicator). |
| `_zigzag`       | Murphy (NYIF 1999) Ch. 14; Achelis, "Technical Analysis from A to Z" (McGraw-Hill 2nd ed.). |

## 3. Test counts

  * **Before Phase 9:** 1846 unit + 6 integration.
  * **After Phase 9:** 1846 baseline + 27 new = **1873 unit** (15 theory + 4 GEX + 3 portfolio + 5 lake) + 6 + 3 new integration = **9 integration**. 30 new tests total.
  * Targeted run of new + critical files: **52 passed** in 14.85s.
  * Theory unit suite: **15/15** pass.
  * Integration tests: **3/3** pass (60s — `create_app` start-up is slow because it boots every router).
  * **Caveat (pre-existing):** `test_corpus_knowledge_aggregator::test_idempotent_recompute` fails when run after some other tests due to a UNIQUE-constraint on `market_outcomes` from leftover DB state. Reproduces on `main` before my changes — confirmed not introduced here. Tracked for a Phase 10 fix.

## 4. Local verification

| Endpoint                                                     | Local response (summary)                                                     |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `GET /theories`                                              | `{theories:[5 entries], windows:[1y,2y,5y,max,1m,3m,6m], zigzag_pct_default:3.0}` |
| `GET /theories/gann/AAPL?window=5y` (mocked bars)            | `{bars:[120], annotation:{theory:'gann', lines:[…fan rays + cycles + retraces…], markers, zones, citation:'W. D. Gann…'}, saved:null}` |
| `POST /theories/fibonacci/TSLA/save?window=1y` body `{annotation:{…}}` | `{ok:true, saved:{id, theory, ticker, window, annotation:{…}, …}}` |
| `DELETE /theories/fibonacci/TSLA/saved?window=1y`            | `{ok:true, removed:true}` |
| `GET /heatseeker/expirations`                                | `{buckets:[{label:'0d',max_dte:0}, ..., {label:'all',max_dte:null}], default:'all'}` |
| `GET /heatseeker/SPY?expiration=5d`                          | Filtered to ≤5-day chain, response includes `expiration:"5d"` and `max_dte:5`. |
| `GET /portfolio/equity?range=last_session` (with seeded snapshots) | `{snapshots:[…], dataset_note:'Showing 2026-06-05 session — markets closed today.', range:'last_session'}` |
| `GET /portfolio/equity?range=1d` (empty 24h) | Wrapped fallback `{snapshots, dataset_note, fallback_from:'1d'}` |
| `GET /lake/health/alerts`                                    | `{alerts:[], active_count:0}` (mock test confirms active rows surface) |

**EC2 deploy:** _Not executed in this session_ — I prepared the deployable artifact tree locally, but did not have an authenticated `lm-arbiter-poc` AWS profile inside this sandbox to drive `aws s3 cp` + SSM. The operator should run the existing `bin/deploy_p8.sh`-style pattern from their workstation; the new code requires no infra changes (no new buckets / IAM / secrets) beyond what Phase 8 already provisioned. Honest call: this is the one place this batch did not ship end-to-end. Hand-off command:

```
cd /Users/srikanthparimi/trading-bot
tar --exclude='node_modules' --exclude='.venv' --exclude='__pycache__' \
    --exclude='._*' -czf /tmp/mits_p9.tgz backend frontend/dist tests requirements.txt
AWS_PROFILE=lm-arbiter-poc /usr/local/bin/aws s3 cp /tmp/mits_p9.tgz \
    s3://tradingbot-artifacts-157320905163/hotfix/mits_p9.tgz
# Then run the standard SSM unpack-and-restart from /tmp/deploy_p8.sh.
```

## 5. Frontend build size

```
dist/assets/vendor-charts-BEm_DMGP.js      412.59 kB │ gzip: 116.61 kB
```

The `vendor-charts` chunk now includes both recharts + lightweight-charts. **116KB gzipped is below the operator's ≤200KB target.** Top-level `index.js` is 86KB gzipped (no change vs baseline). Total dist size: ~1.3MB raw / ~370KB gzipped.

## 6. Operator-visible changes

  * **Knowledge page** now opens on "Theory Studio" tab. Pick a stock + theory + window, see canonical annotations on a TradingView candle chart, tweak parameters in the right rail, save the result.
  * **Heatseeker** shows a `DTE:` dropdown beside the quick-ticker chips (0d/1d/5d/7d/14d/30d/60d/all). URL deep-links via `?expiration=14d`.
  * **Today / Portfolio chart** never goes blank on weekends or holidays — the backend falls back to the most-recent session and the chart shows a small yellow note "Showing YYYY-MM-DD session — markets closed today."
  * **Lake Status page** carries a red banner whenever the hourly health monitor finds bronze/gold staleness, vector-namespace shrinkage, or excess bronze-write failures. Each row has an "Acknowledge" button; auto-resolves on the next cron pass once the underlying condition clears.

## 7. Phase 9 invariants honoured

  * **No assumptions on theory math** — every module cites its primary source in the docstring; ratios + formulas pasted verbatim from Bulkowski / Gann / Fibonacci / Hosoda / Person.
  * **Config-driven** — `theory_zigzag_pct`, `theory_cache_ttl`, `lake_alert_*`, `gex_expiration_buckets` all in `TUNABLES`. Zero new magic numbers in logic.
  * **Fresh-start contract** — `saved_theory_annotations` + `lake_health_alerts` added to `EXTERNAL_CACHE_TABLES`. Operator drawings survive paper-reset.
  * **Audit invariants** — no Trade-write changes. No DecisionLog / ExecutionLog touched.
  * **Real UI library** — TradingView's `lightweight-charts` 4.2 (open-source, ~110KB) for the editable candle chart. Recharts unchanged.
  * **No Telegram / messaging mentions** in new code/comments/docs. The notifier module was untouched.

## 8. Known limitations (ruthless honesty)

  * **Drag-to-edit on the chart canvas is partial.** `lightweight-charts` open-source does NOT ship a built-in shape-handle UI; we provide an "Edit mode" toggle plus the right-rail params panel (drag pivot date / zigzag / window) which re-runs the analysis. True direct-on-canvas drag of line endpoints is a Phase 10 follow-up (TradingView paid lightweight-charts plus build-out is roughly 1 day of work).
  * **Gann fan auto-detection** picks the latest significant swing pivot. The annotation includes a `Swing low pivot` marker so the operator can visually verify. If the auto pick is wrong, the right-rail "Pivot bar index" override re-anchors immediately.
  * **Price-action confidence labelling.** Patterns scoring < 0.65 are tagged "auto-detected, please verify" in the annotation notes. Confidence in the side panel.
  * **No EC2 deploy this session** — see §4. The artifact is ready; the operator drives the SSM step.

## 9. Status log entry

```
2026-06-08  MITS Phase 9 (Theory Studio + UX) shipped local: 5-theory engine
            with primary-source citations, /theories CRUD endpoint, editable
            lightweight-charts Theory Studio tab on Knowledge page, GEX
            expiration dropdown (0d/1d/5d/7d/14d/30d/60d/all), Today chart
            last-session fallback, hourly Lake Health monitor + alert banner.
            +27 unit tests, +3 integration. Frontend build clean, vendor-
            charts chunk 116KB gzipped (target ≤200KB). EC2 deploy deferred
            to operator (no infra change required).
```
