# Post-Implementation Validation Audit Report

**Generated:** 2026-06-13 (post perf-fix + F1-F5 ship)
**Scope:** Original pillar-watch app, 18 routes
**Build under test:** `/Users/srikanthparimi/trading-bot/frontend/dist` (built locally)
**Backend under test:** local `uvicorn backend.main:app` on :8000 with `DISABLE_SCHEDULER=1`
**Browser surface:** Playwright Chrome 1.60.0, viewport 1440x900 desktop + 414x896 mobile
**Production:** NOT exercised here (Cloudflare Access blocks unauthenticated curl per mandate)

---

## Headline

- **Tests run:** 30 (25 page tests + 5 cross-page / feature tests)
- **Tests passed (Playwright):** 27 / 30
- **Tests failed:** 3 — but 5 P0 bugs surfaced because some "passing" tests passed against JSON shadows, not the actual UI page
- **18 mandated pages, browser-reachable as React page:** 13 / 18
- **18 mandated pages, returning raw JSON instead of UI (P0):** 5 / 18
- **Fixes applied during audit:** 0 (validation-only mandate honored)

---

## The Big Finding (P0 — affects production identically)

**Backend API prefixes shadow the React client-side routes.** When a browser navigates to `/tomorrow`, FastAPI matches the `/tomorrow` GET handler (added by Phase 3 MITS-P3.3) BEFORE the SPA catch-all in `backend/main.py:419`. The browser receives `application/json`, not the React shell — so the page is unreachable.

This bug applies to BOTH local dist build AND production deploy (same FastAPI catch-all wiring).

### Concrete reproductions (curl proves it; screenshots confirm):
| URL | Expected | Actual (Content-Type) | Backend route that shadows it |
|---|---|---|---|
| `/tomorrow` | Tomorrow Setup page | `application/json` | `backend/api/routes/tomorrow.py:24` `APIRouter(prefix="/tomorrow")` with `@router.get("")` |
| `/analysis/AAPL` | StockAnalysis page | `application/json` | `backend/api/routes/analysis.py:81` `APIRouter(prefix="/analysis")` with `@router.get("/{ticker}")` |
| `/detectors` | DetectorScorecard page | `application/json` | `backend/api/routes/*` registers `/detectors` GET |
| `/trial-scorecard` | TrialScorecard page | `application/json` | backend has `/trial-scorecard` GET |
| `/retrospective` | Retrospective page | `application/json` | backend has `/retrospective` GET |

Screenshots captured (raw JSON visible in browser):
- `p19_screenshots/validation/tomorrow-desktop.png` — shows `{"analysis_date":"2026-06-13","rows":[],"count":0}`
- `p19_screenshots/validation/analysis-aapl-desktop.png` — shows raw OHLC bar data
- `p19_screenshots/validation/detectors-desktop.png` — shows detector config JSON

### Why automated `must_have` passed but the page is broken
Three of these returned 2xx + had text matching the `must_have` regex (e.g. JSON contained "AAPL" or "description"), so the spec didn't flag them. The visual screenshots are the ground truth.

### Recommended fix shape (NOT applied per mandate)
Two options:
1. **Backend side**: change shadowing prefixes to namespaced paths (e.g. `/api/tomorrow`, `/api/analysis`, `/api/detectors`). Smallest blast radius. Update frontend fetches accordingly.
2. **Frontend side**: move client routes to a `/app/*` prefix so they cannot collide.

Option 1 is the conventional fix.

### Verified UNAFFECTED (text/html confirmed):
`/`, `/trades`, `/trade-loop`, `/lake`, `/brain`, `/decision-scorecard`, `/decision-cockpit`, `/decision-cockpit/AAPL`, `/hypothesis-studio`, `/intel`, `/council`, `/lab`, `/settings`, `/knowledge`, `/analysis` (no ticker — but pages without ticker render an empty state).

---

## Per-Page Grid

| # | Page | Path | Renders | Data populated | Console | Server | Vendor 4xx | Notes |
|---|------|------|---------|----------------|---------|--------|------------|-------|
| 1 | Today | `/` | YES | YES | CLEAN | CLEAN | none | $5,000 equity + 70 trades + 42.9% win-rate + P&L pillars visible |
| 2 | Trades | `/trades` | YES | YES | CLEAN | CLEAN | none | Trades table populated |
| 3 | Intel | `/intel` | YES | YES | CLEAN | CLEAN | none | Default tab loads |
| 3a | Intel/GEX | `/intel?tab=gex` | YES | YES | CLEAN | CLEAN | none | F2 multi-expiry heatmap path |
| 3b | Intel/Flow | `/intel?tab=flow` | YES | YES | CLEAN | CLEAN | none | |
| 3c | Intel/Earnings | `/intel?tab=earnings` | YES | EMPTY | CLEAN | CLEAN | none | No earnings rows (scheduler off) |
| 3d | Intel/Sources | `/intel?tab=sources` | YES | YES | CLEAN | CLEAN | none | |
| 3e | Intel/AI | `/intel?tab=ai` | YES | YES | CLEAN | CLEAN | none | |
| 4 | Council | `/council` | YES | YES | CLEAN | CLEAN | none | Slow load (35s observed) |
| 5 | Lab | `/lab` | YES | YES | CLEAN | CLEAN | none | |
| 6 | Settings | `/settings` | YES | YES | CLEAN | CLEAN | none | |
| 7 | Knowledge | `/knowledge` | YES | YES | CLEAN | CLEAN | none | |
| 8 | **Tomorrow** | `/tomorrow` | **NO — JSON shadow** | n/a | n/a | n/a | n/a | **P0 backend shadow** |
| 9 | Trade Loop | `/trade-loop` | YES | YES | CLEAN | CLEAN | none | |
| 10 | Analysis (default) | `/analysis` | YES | YES | CLEAN | CLEAN | none | Loads SPY default |
| 10a | **Analysis/AAPL** | `/analysis/AAPL` | **NO — JSON shadow** | n/a | n/a | n/a | n/a | **P0 backend shadow** |
| 11 | **Trial Scorecard** | `/trial-scorecard` | **NO — JSON shadow** | n/a | n/a | n/a | n/a | **P0 backend shadow** |
| 12 | **Retrospective** | `/retrospective` | **NO — JSON shadow** | n/a | n/a | n/a | n/a | **P0 backend shadow** |
| 13 | Lake | `/lake` | YES | YES | CLEAN | CLEAN | none | |
| 14 | **Detectors** | `/detectors` | **NO — JSON shadow** | n/a | n/a | n/a | n/a | **P0 backend shadow** |
| 15 | Brain | `/brain` | YES | YES | CLEAN | CLEAN | none | Calibration + scorecard visible |
| 16 | Decision Scorecard | `/decision-scorecard` | YES | YES | CLEAN | CLEAN | none | F5 — composite + bins + funnel |
| 17 | Decision Cockpit | `/decision-cockpit` | YES | YES | CLEAN | CLEAN | none | Picker working |
| 17a | Decision Cockpit/AAPL | `/decision-cockpit/AAPL` | YES | YES | CLEAN | CLEAN | none | F3 — abstained decision rendered |
| 18 | Hypothesis Studio | `/hypothesis-studio` | YES | YES | CLEAN | CLEAN | none | 18.A-D surfaces visible |

**Console / server cleanliness:** every visited React page produced 0 uncaught JS errors, 0 console.error, 0 server 5xx, and 0 vendor 4xx. F1-F3 pages and Today panel all stayed clean.

---

## Performance Metrics (Playwright Performance API, local backend)

Approximate per-page timings observed across the run (chrome cold-cache → warm-cache mix). DOMContentLoaded and LoadEvent often coincide because the React shell is small (single index.html + lazy chunks).

| Page | Total wall ms (observed) | Notes |
|------|--------------------------|-------|
| Today | ~11,000 | Heavy panels (funnel + portfolio + activity) |
| Trades | ~11,500 | |
| Intel (all tabs) | ~10,500–11,700 | |
| Council | **~35,300** | Slowest — chairman memo + dissent + agent panels |
| Lab | ~13,200 | |
| Settings | ~10,700 | |
| Knowledge | ~10,600 | |
| Trade Loop | ~10,900 | |
| Analysis (default) | ~10,600 | |
| Lake | ~6,500 | |
| Brain | ~7,400 | |
| Decision Scorecard | ~7,300 | |
| Decision Cockpit (picker) | ~8,500 | |
| Decision Cockpit/AAPL | ~11,000 | |
| Hypothesis Studio | ~8,300 | |

P-Perf shipped React.lazy() + manualChunks split — confirmed in `dist/assets/` (per-page chunks present, vendor-react/router/charts split out). Wall times above include 1.2s fixed waits inside the spec — actual DOM-ready times are smaller.

---

## Single-Source-of-Truth Cross-Page Consistency

### Funnel (F1 — `useFunnel` hook)
- API `/learning/funnel` returned: `watchlist_evaluated=86, submitted=86, closed_with_pnl=70` (14-day window, on-demand fallback — nightly job not run yet on this fresh DB)
- Hook usage verified via source grep (3 components, all under `src/components/`):
  - `ThroughputAlertBanner.jsx:19`
  - `FunnelSummaryPanel.jsx:19`
  - `FullDecisionFunnelChart.jsx:25`
- Cross-page numeric match: the SoT spec attempted to find the `submitted=86` digit on Today, Decision Scorecard, and Decision Cockpit page text. Spec passed without assertion failure but the digit match through text scraping is unreliable in SVG/canvas; the source-level guarantee (single hook) is the more important consistency proof.

### Heatseeker multi-expiry (F2 — `useHeatseekerMulti` hook)
- Hook usage: `MultiExpiryGexHeatmap.jsx:2` (the only canonical consumer)
- F2 test: `/intel?tab=gex` rendered with either heatmap text OR empty-state text — PASS
- 0 console errors during render

### Decision Cockpit (F3 — `useDecisionCockpit` hook)
- Hook usage: `DecisionCockpit.jsx:48`
- F3 test: visiting `/decision-cockpit/AAPL` rendered the cockpit and showed counterfactual-related text — PASS
- `/decision/cockpit/AAPL` API returns `event_status=abstained` for AAPL (most recent decision) — the page should show the abstained-state panels

### Chart bars (F4 — `useAnalysisBars` hook)
- Hook usage: `StockAnalysis.jsx:531`
- F4 test FAILED — `getByRole('button', { name: /^1D$/i })` matched 0 buttons because:
  1. The buttons render with `role="tab"` (override of default button role) — `getByRole('button')` skips them
  2. Visiting `/analysis/AAPL` returned JSON, so the chart never mounted
- Direct verification via source: `TimeframeSelector.jsx:36-55` renders all 10 buttons with `data-testid="tf-1D"` etc. The component IS wired correctly.
- The screenshot `analysis-aapl-fullscreen.png` is identical to `analysis-aapl-desktop.png` — both are the raw JSON page

### Portfolio Equity
- API `/paper/state` returned: `portfolio_value=5000.0`
- "$5,000.00" was visible on the Today page header (see `today-desktop.png` top-right) — PASS

---

## Issues (numbered, severity-tagged)

### P0 (production-impacting — pages unreachable in browser)

1. **`/tomorrow` returns JSON instead of React page**
   - File: `backend/api/routes/tomorrow.py:24` (router prefix `"/tomorrow"`)
   - Reproduction: `curl -sI http://127.0.0.1:8000/tomorrow` → `application/json`
   - Impact: Tomorrow Setup page is unreachable in the browser
   - User-visible: navigates from sidebar Tomorrow icon → raw JSON dump

2. **`/analysis/{ticker}` returns JSON instead of React page**
   - File: `backend/api/routes/analysis.py:81` (router prefix `"/analysis"`) + `analysis.py:845` (`@router.get("/{ticker}")`)
   - Reproduction: `curl -sI http://127.0.0.1:8000/analysis/AAPL` → `application/json`
   - Impact: ALL per-stock analysis pages (`/analysis/AAPL`, `/analysis/MSFT`, etc.) are unreachable. This is the core "build a thesis" page in the operator's trading intelligence narrative
   - Note: bare `/analysis` works (catch-all kicks in since no GET route at exact match)

3. **`/detectors` returns JSON instead of React page**
   - File: `backend/api/routes/*` exposes a `/detectors` top-level GET
   - Reproduction: `curl -sI http://127.0.0.1:8000/detectors` → `application/json`
   - Impact: Detector Edge Scorecard page is unreachable
   - Note: `/detectors/some_name` works (HTML), so individual detector deep-links function — only the index page is broken

4. **`/trial-scorecard` returns JSON instead of React page**
   - File: backend has a top-level `/trial-scorecard` GET handler
   - Reproduction: `curl -sI http://127.0.0.1:8000/trial-scorecard` → `application/json`
   - Impact: $5k Trial Scorecard page is unreachable

5. **`/retrospective` returns JSON instead of React page**
   - File: backend has a top-level `/retrospective` GET handler
   - Reproduction: `curl -sI http://127.0.0.1:8000/retrospective` → `application/json`
   - Impact: Weekly Retrospective page is unreachable

**Common cause:** `backend/main.py:419` SPA catch-all `@app.get("/{path:path}")` is registered AFTER all API routers, so any exact-prefix collision wins.

### P1 (functional bug not blocking page render)

_None found beyond the P0 shadow class above._

### P2 (must-have content didn't match — empty state or UI mismatch)

6. **Intel/Earnings tab shows empty content**
   - URL: `/intel?tab=earnings`
   - Reproduction: navigate, observe blank tab body
   - Root cause: with `DISABLE_SCHEDULER=1` no earnings data is fetched. Page renders, but earnings panel is empty
   - Severity P2: not a code bug; live-data dependency. Production with scheduler on will populate this

### P3 (cosmetic / informational)

7. **Council page slow load (~35s observed in test)**
   - URL: `/council`
   - Likely cause: heavy network panel load + multiple polling subscribers
   - Severity P3: did not break, but felt sluggish

8. **TimeframeSelector buttons use `role="tab"` not `role="button"`**
   - File: `frontend/src/components/TimeframeSelector.jsx:40`
   - Not a UX bug per se — but it caused this audit's F4 test to be a false-negative. Future tests should select via `[data-testid^="tf-"]`
   - Severity P3: testing affordance only

---

## Feature Validation (F1-F5)

### F1 — Funnel SoT (`useFunnel` hook)
- ✅ Hook canonical: 3 components use it; no duplicated funnel fetches
- ✅ Backend endpoint returned full report with all 10 stages + confidence histograms + top_surgical_change_candidate
- ✅ Today + Decision Scorecard + Decision Cockpit render without errors (F1 wired into Today banner via ThroughputAlertBanner + Today funnel summary via FunnelSummaryPanel)

### F2 — Heatseeker Multi-Expiry (`useHeatseekerMulti` hook)
- ✅ Hook canonical: 1 component (MultiExpiryGexHeatmap)
- ✅ Visible on `/intel?tab=gex`
- ⚠️ Backend GEX data depends on ThetaData / Alpaca being live; with DISABLE_SCHEDULER=1, response is best-effort

### F3 — Decision Cockpit (`useDecisionCockpit` hook)
- ✅ Hook canonical: 1 page (DecisionCockpit)
- ✅ Renders on `/decision-cockpit` (picker) + `/decision-cockpit/:identifier`
- ✅ HOLD/abstained `decision_cockpit/AAPL` shape verified — includes `learning_insights.attribution_summary`, `active_policy_recommendations`, `active_weight_proposals`, `funnel_snapshot` keys

### F4 — Chart Timeframes + Fullscreen (`useAnalysisBars` hook)
- ❌ **Cannot validate live behavior** — `/analysis/AAPL` is shadowed by the backend, so the chart never mounts in the browser
- ✅ Source verified: TimeframeSelector.jsx renders 10 buttons with stable `data-testid`s + ChartFullscreenWrapper.jsx implements ⛶ expand + ESC exit + long-press mobile
- ⚠️ Cannot test interactivity until the P0 shadowing is fixed; reach `/analysis` (no ticker) instead — that page renders, but defaults to a different ticker selection flow

### F5 — Decision Scorecard
- ✅ Page renders at `/decision-scorecard`
- ✅ Backend endpoint `/decision/scorecard` returned full structured response (composite_distribution, by_sub_score, calibration_bins x 10, expectancy_by_bin x 10)
- ⚠️ All values null because no closed trades carry composite quality scores yet (waiting for n_closed to grow under the new accounting)

---

## Trading Intelligence Narrative

| Operator question | Page(s) needed | Reachable? | Verdict |
|---|---|---|---|
| Learning — "What setup is the system seeing?" | `/knowledge` + `/detectors` | knowledge=YES, detectors=**NO (P0)** | PARTIAL — operator can browse general patterns on /knowledge but cannot see the per-detector edge scorecard |
| Analysis — "Build a thesis" | `/analysis/AAPL` | **NO (P0)** | BROKEN — the per-stock analysis page is unreachable in the browser |
| Decision — "Risk/reward + composite score" | `/decision-scorecard` + `/brain` | YES + YES | OK |
| Execution — "Execute with confidence" | `/decision-cockpit/AAPL` | YES | OK — abstained decision shows reasons; would-have-been + counterfactual surfaces present |

**Net:** 2 of 4 operator-narrative questions are blocked by the same backend-shadowing class of bug. Fixing it (single backend refactor) unblocks Tomorrow, Analysis/:ticker, Detectors, Trial Scorecard, AND Retrospective in one change.

---

## Information Quality Scan

Grepped `src/pages/**.jsx` + `src/components/**.jsx` for `TODO`, `FIXME`, `placeholder`, `lorem`, `console.log`. Findings:

- All `placeholder=` matches are legitimate `<input>` field placeholders (TickerSearch, decision picker, watchlist add, etc.) — not lorem-ipsum copy
- No `// FIXME` or `// TODO` left in pages/components
- No `console.log` left in pages/components (verified)
- No "xxx", "lorem", or debug strings in the bundle

**Verdict:** clean — no placeholder/debug content shipped.

---

## Console Error Audit

| Category | Count across all 13 reachable pages | Examples |
|---|---|---|
| Real bug (pageerror) | 0 | none |
| Console.error (real app) | 0 | none |
| Server 5xx | 0 | none |
| Vendor 4xx (yfinance/alpaca/etc) | 0 | none (suppressed by DISABLE_SCHEDULER=1) |
| Network race / abort | not observed | n/a |

Excellent cleanliness on reachable pages.

---

## Mobile Responsive

Each reachable page captured at 414x896 (iPhone-ish). Screenshots saved as `{id}-mobile.png`. Quick scan:
- Today: sidebar collapsed to icons only — readable
- Decision Cockpit: panels stack vertically — readable
- Hypothesis Studio: tab strip wraps — usable but cramped
- Intel: tab strip wraps — OK

No layout-breaking overflow / cropping detected in the 13 reachable pages. **Full visual review requires a human pass** — Playwright can flag truncated text but not aesthetic issues.

---

## Honest Gap Report

What this audit could NOT validate:

1. **Production parity** — Cloudflare Access blocked direct browser access; validation was against local dist + local backend only. Same FastAPI catch-all is shipped to EC2, so the P0 backend-shadow class WILL affect production identically (verified mechanically).
2. **Live data freshness timestamps** — backend ran with `DISABLE_SCHEDULER=1` to keep the audit deterministic. Live freshness pills + age indicators on the Today page need a separate pass with the engine loop active.
3. **Mobile gesture interactions** — Playwright doesn't simulate pinch-zoom or two-finger pan; chart zoom/pan tested only via timeframe-button clicks (which also failed for the shadow reason).
4. **Counterfactual recompute under multiple sizing multipliers** — backend route confirmed reachable but not exercised across a range.
5. **Visual aesthetic review** — desktop + mobile screenshots saved per page; a human reviewer should eyeball alignment, padding, typography, and theme consistency.
6. **5 of 18 mandated pages couldn't be interacted with at all** — they returned JSON. Full UI validation of those pages must wait until the P0 backend-shadow class is fixed.

---

## Screenshot Inventory

All saved under `frontend/p19_screenshots/validation/`:

- 25 `{id}-desktop.png` (1440x900)
- 25 `{id}-mobile.png` (414x896)
- `analysis-aapl-fullscreen.png` (captured during F4 attempt — identical to the JSON-shadow desktop shot)
- `cockpit-hold.png` (captured during F3 test — shows the abstained AAPL cockpit panels)

Key evidence:
- `today-desktop.png` — shows the real Today page (equity $5,000, 70 trades, 42.9% WR, P&L pillars, portfolio chart placeholder, Decision Pipeline)
- `tomorrow-desktop.png` — shows raw JSON in browser (P0 evidence)
- `analysis-aapl-desktop.png` — shows raw JSON in browser (P0 evidence)
- `detectors-desktop.png` — shows raw JSON in browser (P0 evidence)
- `intel-earnings-desktop.png` — shows empty earnings panels (P2 — data dependency)

---

## Validation Spec

`frontend/e2e/p19_validation_all_pages.spec.js` — 30 tests covering 25 page navigations + 5 cross-page / feature tests. Re-runnable via:
```
cd frontend && node ./node_modules/.bin/playwright test e2e/p19_validation_all_pages.spec.js --reporter=list
```
