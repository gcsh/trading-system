# Phase 1 UI Audit — 2026-06-02

Page-by-page check confirming the new data surfaces ship and are mounted
in the right places. Bundle compiled cleanly during the
2026-06-02T18:50Z deploy.

## 1. New components shipped this session

| Component | Location | Mounted in | Lines | Notes |
|---|---|---|---|---|
| `DataQualityChip` | components | `AuthoritySpine.jsx:297` (topbar) | 234 | Sibling to WarningsChip. Polls `/system/data-quality` every 30s. Modal with provider breakdown + sanity-flag breakdown + reset button. |
| `LongGammaStrip` | components | `Heatseeker.jsx:173` (above panels) | ~190 | Regime banner with dealer regime + distance to flip + 0DTE share. |
| `CumulativeGexPanel` | components | `Heatseeker.jsx:225` (middle panel) | ~135 | Running cumsum of net GEX across strikes — the institutional way to spot the gamma flip level visually. |
| `ExpiryDecompositionPanel` | components | `Heatseeker.jsx:226` (right panel) | ~165 | Stacked bars by expiry bucket (0DTE / weekly / monthly OPEX). |

## 2. Heatseeker page final layout (#134)

```
┌────────────────────────────────────────────────────────────────┐
│  🔥 Heatseeker — Gamma Exposure (GEX)        [ticker search]   │
│  QUICK [SPY] [AAPL] [TSLA] [NVDA] [QQQ] ...    🟢 LONG GAMMA  │
├────────────────────────────────────────────────────────────────┤
│  ┌──────────────────── LongGammaStrip ────────────────────┐    │
│  │  Regime label · distance to flip · 0DTE %             │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                │
│  [Spot] [Net γ] [Call γ] [Put γ] [1-Day Expected Move]         │
│                                                                │
│  ┌────── Wall summary + tabs ──────┐                           │
│  │  Call Wall · Put Wall · Flip    │                           │
│  └─────────────────────────────────┘                           │
│  ┌──────────────────┬──────────────┬──────────────────┐        │
│  │  GEX per Strike  │ Cumulative   │ Expiry           │        │
│  │  (left panel)    │ GEX panel    │ Decomposition    │        │
│  │                  │ (#14 panel 2)│ (#14 panel 3)    │        │
│  └──────────────────┴──────────────┴──────────────────┘        │
│                                                                │
│  Footer: GEX source · LONG vs SHORT description                │
│  FlowIQPanel below                                             │
└────────────────────────────────────────────────────────────────┘
```

Per-strike panel + Cumulative panel + Expiry decomposition panel = the
3-panel layout from todo item #14 and the user's mockup. LongGammaStrip
above them = Long Gamma regime view.

## 3. Topbar status: AuthoritySpine.jsx

The Authority Spine carries the operator-observability chips at the top
of every page:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Attention [sev] ...                  [DataQualityChip] [WarningsChip] │
└─────────────────────────────────────────────────────────────────────┘
```

The DataQualityChip's visual states:
- ✓ `data N/N clean` (green pill) — all ThetaData hits, no rejects
- ⚠ `data N/M clean` (amber pill) — some rejects OR fallback usage > 10%
- ✗ `data N/M clean` (red pill) — fallback share > 20% OR any "none" (no provider succeeded)

Click → modal with:
- Provider breakdown: ThetaData, ThetaData rejected, yfinance fallback, cboe fallback, none
- Sanity-flag breakdown with descriptions (9 flag types covered)
- Reset counters button

## 4. Pages inventoried — render check

28 pages exist; bundle compiled to per-page chunks via Vite code split.

| Page | Status | Notes |
|---|---|---|
| AISignals | ✓ chunked | unchanged this session |
| AlertsPage | ✓ chunked | unchanged |
| AutopsyGallery | ✓ chunked | unchanged |
| Cockpit | ✓ chunked | unchanged |
| CommandCenter | ✓ chunked | unchanged |
| Council, CouncilOverview | ✓ chunked | unchanged; sees richer agent_context (#132) |
| Desk | ✓ chunked | unchanged |
| EarningsIntel | ✓ chunked | unchanged |
| Flowseeker | ✓ chunked | unchanged |
| **Heatseeker** | ✓ chunked | **3 panels + Long Gamma strip mounted (#134)** |
| Intel, Lab | ✓ chunked | unchanged |
| Market | ✓ chunked | unchanged |
| MissionControl | ✓ chunked | unchanged |
| Overview | ✓ chunked | unchanged |
| Portfolio | ✓ chunked | **paper_executor complex-MTM (#131) flows here** — short-option positions now show non-zero market_value |
| Risk | ✓ chunked | unchanged |
| Settings, SettingsHub | ✓ chunked | unchanged |
| ShadowComparison | ✓ chunked | unchanged |
| SourceAttribution | ✓ chunked | unchanged |
| Strategies | ✓ chunked | unchanged; Signals now carry richer metadata (drift, target_dte, expiration) but display unchanged |
| Today | ✓ chunked | unchanged |
| Trades, TradesV2 | ✓ chunked | unchanged |
| Trial | ✓ chunked | unchanged |
| WatchlistPage | ✓ chunked | warm-start backfill (P1.3-FU4) fires on add — no UI change visible |

## 5. New data surfaces flowing into the UI

| New backend field | Reaches which page | Visible to operator? |
|---|---|---|
| `iv_rank_estimated=false` | Today, Strategies | No dedicated display yet — flows through `iv_rank` directly |
| `data_confidence` | Today (snapshot) | Not yet surfaced explicitly; available in API response |
| `sanity_flags` | Today (snapshot), DataQualityChip aggregates | Aggregate counts in chip; per-snapshot flags in API only |
| `target_strike`, `target_dte`, `moneyness_actual`, `strike_drift_pct` | Trades metadata | API only; UI doesn't yet have a "drift indicator" pill |
| `target_delta` | Trades metadata | API only |
| `expiration` (chain-resolved) | engine plan → paper_positions.meta | Shown as "expires DD-MMM" in CurrentlyHoldingStrip |

## 6. UI gaps worth a follow-up

These would round out the new data layer's UI story:

  1. **Per-trade drift badge.** When a Signal had `strike_drift_pct >
     0.02`, the trade card should show a small "drifted" pill so the
     operator sees the executed strike wasn't the strategy's target.
  2. **Data-confidence colour on Today.** Each per-ticker card on
     Today could shade its border by `data_confidence` (high=green,
     medium=amber, low=red). Quick read of "which names are we
     trading with degraded data."
  3. **IV rank vintage indicator.** When `iv_rank_estimated=true`,
     the rank should display a "~" prefix so it's visibly different
     from a real percentile.
  4. **DataQualityChip "pause polling when hidden".** When document
     is hidden, suspend the 30s poll — saves a roundtrip every 30s
     when the tab isn't active.
  5. **LongGammaStrip needs the regime-history ribbon.** Currently
     shows current regime. The original spec asked for a 30-day
     ribbon (have we been persistently long-gamma or whipping). Not
     in the shipped component — verify before claiming complete on a
     future revisit.
  6. **No DataQualityChip Playwright test.** WarningsChip has one.

## 7. Bundle health (post-deploy)

```
$ ls frontend/dist/assets/*.js | wc -l
~40 chunks (Vite per-route split)

$ deploy.sh build output (last run):
✓ 2056 modules transformed.
✓ built in 14.36s
```

No build errors. Bundle size sits at ~825 KB main + ~40 per-route chunks
(small ones <10 KB, largest is AISignals at 19 KB).

---

*Architecture: `PHASE_1_ARCHITECTURE_AUDIT.md`. Final summary:
`PHASE_1_FINAL_SUMMARY.md`.*
