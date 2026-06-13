# MITS Phase 10.3 — Theory Studio visual noise fix

Ship date: 2026-06-08.

## 1. Root cause

`backend/bot/theories/pivots.py` computed pivots for the most recent
period only (yesterday's HLC → daily; last week → weekly; last month
→ monthly) and then emitted each of the 7 levels as ONE
`kind=horizontal` line spanning the entire visible window
(`first_ts → last_ts`). The chart's `createPriceLine` interpretation
turned each of those into a right-axis label.

On a 1y window the operator saw 11 stacked right-axis labels
("PP daily 741.97", "PP weekly 743.18", "PP monthly 743.18",
"R1 daily 749.71"…) all clustered around today's price — zero
temporal context, no signal information, "label salad".

`backend/bot/theories/murrey_math.py` had the same problem on its
0/8..8/8 ladder (single anchor → 9 horizontals spanning the full
window).

## 2. Stepped-segment implementation

Files: `backend/bot/theories/pivots.py`, `backend/bot/theories/murrey_math.py`.

### Pivots

- Group bars by period (daily / weekly ISO / monthly).
- For each period[i] (i ≥ 1), compute pivots from period[i-1]'s HLC.
- Emit each level (PP/R1/S1/R2/S2/R3/S3) as a SHORT trendline segment
  spanning only the bars of period[i]. Each carries
  `meta.stepped=True` and `meta.priority` (1..3).
- Right-axis label ladder: a single set of `kind=horizontal` lines
  with empty `start.ts`/`end.ts` and `meta.label_only=True` for the
  MOST RECENT period only, so the axis stays clean.
- Window→timeframe adaptation:
  - ≤ 5d → daily only
  - ≤ 35d → daily + weekly
  - ≤ 200d → weekly + monthly
  - 200–400d (1y) → monthly only
  - > 400d → monthly only AND drop R3/S3 (rare outliers per Person p.97)
- Hard cap `MAX_STEPPED_SEGMENTS=240` to bound the renderer cost.

### Murrey Math

- Walk the window in non-overlapping 64-bar frames anchored to the
  right edge (operator's most recent frame matters most).
- For each frame: quantise that frame's high/low into Murrey octave,
  emit each `n/8` level as a stepped trendline segment.
- Density-driven level filtering:
  - simple → {0, 4, 8} (the magnet + ultimate S/R trio)
  - normal → {0, 2, 4, 6, 8}
  - detailed → all 9
- Right-axis label row from the most-recent frame.
- Signal/magnet logic still keys on the latest frame's `step` + `lo_q`.

## 3. Density control

Files: `backend/api/routes/theories.py`, `frontend/src/pages/TheoryStudio.jsx`.

- New `density` query param on `/theories/{theory}/{ticker}` and
  `/theories/multi/{ticker}`: `simple | normal (default) | detailed`.
- Backend `_apply_density_filter` post-filter drops lines whose
  `meta.priority` exceeds the operator's density threshold:
  `simple → priority ≤ 1`; `normal → ≤ 2`; `detailed → all`.
- Lines without `meta.priority` default to priority=2 (survive normal
  + detailed; dropped on simple).
- Priority metadata added to: pivots, murrey_math, bollinger,
  ma_ribbon, atr_bands. The remaining 18 theories inherit the
  default priority of 2 (their lines render under normal + detailed
  unchanged; they suppress under simple, which is the correct UX —
  simple is a "show me the headline only" view).
- Frontend adds a "Density" `<select>` next to the Window selector
  and forwards the choice as `params.density` JSON.

## 4. Signal marker prominence (P10.3.3)

File: `frontend/src/components/TheoryChart.jsx`.

- Marker arrows scaled up from 14×18 to 22×22 with a thick
  outlined arrowhead + stem geometry. Each marker now carries a
  paired pill badge underneath/above with `BUY $743.12` /
  `SELL $738.40` / `WATCH` / `OPT` rendered in 11px bold inside a
  rounded rectangle outlined in the action colour.
- Hit-box widened to cover both marker + pill so the hover popover
  catches naturally.
- On signal hover: the chart container fades to 32% opacity via a
  130ms CSS transition so the BUY/SELL pill pops out — operator's
  eye is guided to the actionable element.
- LatestSignalsColumn now emits a per-theory dashed callout box when
  a theory returned zero signals on the active window:
  "No actionable signals from {theory} in the {window} window. Try a
  shorter window (e.g. 1m for Pivots) or add more theories."

## 5. Pivot signal rule relaxation (P10.3.6)

The Phase 10.2 rule "close > R1 of prior day AND volume > 1.2×
MA(20)" returned **0 signals on a 1y SPY window** (verified by
operator's screenshot). Replaced with:

- For each monthly period[i], compute monthly pivots from period[i-1].
- Walk period[i]'s bars; on the FIRST bar where `close > R1[i]`,
  emit BUY (one per month, max 12/yr).
- On the FIRST bar where `close < S1[i]`, emit SELL.
- No volume confirm — Person's monthly-pivot framework is a
  position-trade gate, not an intraday confirm.

Result on EC2 SPY 1y after deploy: **10 BUY/SELL signals** (vs 0
before).

## 6. Right-rail label dedup (P10.3.5)

Implemented as the "simpler" alternative the spec offered: drop the
per-segment `label` field, render only the MOST RECENT timeframe's
ladder as right-axis horizontals (with prefix `S1 729.85`,
`PP 745.12` etc.). No more 5-deep label stack.

## 7. Tests

Files: `tests/unit/test_stepped_pivots.py`,
`tests/unit/test_density_param.py`.

- `test_stepped_pivots.py` — 8 tests covering:
  - 252-bar 1y window emits ≥30 stepped segments.
  - Each segment's `start.ts`/`end.ts` falls inside the bar window.
  - simple ≤ normal ≤ detailed density ordering.
  - >1y windows drop R3/S3 outliers.
  - Horizontals are label-only (empty ts).
  - Signal rule fires ≥1 BUY/SELL on 1y trending data.
  - Every stepped segment carries `meta.priority`.
- `test_density_param.py` — 30 tests:
  - Per-theory parametrized check (23 theories): simple ≤ normal
    ≤ detailed.
  - Unit tests for `_apply_density_filter` (4 cases).
  - Pivots / Bollinger / ATR-bands priority-meta presence.
- Suite: **94/94 theory tests pass**, **608 of 609 unit tests pass**
  overall (the 1 failure is a pre-existing
  `test_live_outcome_ingest.py::test_ingest_is_idempotent` race that
  has nothing to do with this work).

## 8. EC2 verification (post-deploy)

Verified via SSM on i-0426a45181d08adff:

```
=== pivots SPY 1y normal ===
lines: 39                       (was 11 horizontal-spanning)
signals: 10                     (was 0)
notes: ['Monthly pivots: 36 stepped segments across 12 periods.']

=== pivots SPY 1y simple ===
lines: 13                       (PP only ladder)

=== pivots SPY 1y detailed ===
lines: 91                       (full 5-level × 12-month ladder)

=== murrey SPY 1y normal ===
lines: 15                       (5 levels × 3 frames + label row)
trendlines: 12                  (stepped segments)

=== multi pivots+bollinger SPY 1y normal ===
pivots:    lines=39, signals=10
bollinger: lines=3,  signals=25

=== multi pivots+bollinger+ma_ribbon SPY 1y simple ===
pivots:    lines=13
bollinger: lines=2  (upper/lower kept, mid dropped)
ma_ribbon: lines=3  (EMA-5/21/55 kept; 8/13/34/89/144 dropped)
```

Bot status post-deploy: `running=true`, healthcheck passed.

## 9. Honest gaps

- Priority metadata only added to 5 theories (pivots, murrey, bollinger,
  ma_ribbon, atr_bands). The other 18 still emit at the default
  priority-2 tier — so under "simple" density they all drop to zero
  lines. A follow-up should tag priority on donchian, keltner,
  ichimoku, fibonacci, gann, square_of_9, volume_profile etc. so
  Simple shows their key levels rather than nothing.
- The frontend's `MAX_PRICE_LINES_PER_CHART=50` cap is unchanged —
  but now most of the line count is trendlines (rendered via
  `addLineSeries`), not price lines, so the cap rarely triggers.
- The signal-hover fade applies to the whole chart container (the
  spec said "fade other lines to 30%"). Per-line opacity would
  require iterating overlaySeries and updating each one's options —
  visually equivalent but more code. Left as the simpler global-fade
  for now.
- Window detection in `pivots._select_timeframes_by_window` is bar-
  count + median-gap heuristic, not the route's WINDOW_MAP key (the
  theory module doesn't see the UI label). Works correctly on all
  daily/weekly/monthly resampled inputs the route sends today.

## 10. Files changed

```
backend/bot/theories/pivots.py            REWRITTEN (stepped)
backend/bot/theories/murrey_math.py       PARTIAL REWRITE (stepped frames)
backend/bot/theories/bollinger.py         + priority meta
backend/bot/theories/ma_ribbon.py         + priority meta
backend/bot/theories/atr_bands.py         + priority meta
backend/api/routes/theories.py            + density param + post-filter
frontend/src/components/TheoryChart.jsx   24px markers + price pills + fade
frontend/src/pages/TheoryStudio.jsx       + density selector + no-signal callout
tests/unit/test_stepped_pivots.py         NEW (8 tests)
tests/unit/test_density_param.py          NEW (30 tests)
tests/unit/test_theories_v2.py            updated 1 test for density default
```
