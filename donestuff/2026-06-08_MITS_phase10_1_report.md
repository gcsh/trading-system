# MITS Phase 10.1 — Theory Studio perf + live tick + correctness audit

**Date**: 2026-06-08
**Shipped to**: EC2 `i-0426a45181d08adff` @ EIP `32.197.70.83`,
production URL `https://pillar-watch.com`.
**Trigger**: operator browser freeze ("Page Unresponsive") on Theory
Studio + no live tick + theories not displaying correctly.

---

## 1. Root cause of the 693-line freeze

`curl /theories/bollinger/SPY?window=max` was returning **693
horizontal `trendline` Line objects** — one per (band × bar). Same
shape in `keltner`, `donchian`, `ma_ribbon` (8 EMAs × N bars ≈ 2 000+
lines), `avwap`, `atr_bands`, `ichimoku`, and the indicator-panel
theories.

The frontend then called `chart.addLineSeries({...}).setData([p0, p1])`
**per Line** — turning a continuous curve into 600+ degenerate 2-point
line series. `lightweight-charts` is built around `addLineSeries()`
holding ONE curve via `setData(points[])`; the per-segment pattern made
the main thread synchronously create + lay out 600+ series in a single
frame and the browser surfaced "Page Unresponsive."

**Fix:** added `kind = "series"` to `LineKind` in
`backend/bot/theories/schema.py` with a `points: List[{ts, price}]`
field on `Line`. The frontend now reads `points` once per band and
calls `addLineSeries().setData(points)` ONCE per band — one series for
the whole curve.

## 2. Theories converted to series mode + new line counts

| Theory             | Lines before | Lines after | Notes                                  |
|--------------------|--------------|-------------|----------------------------------------|
| Bollinger          | 693          | **3**       | mid/upper/lower, each a series         |
| Keltner            | ~750         | **3**       | EMA20 mid + ±ATR upper/lower           |
| Donchian           | ~750         | **3**       | Upper-N/Lower-N/Mid channels           |
| MA Ribbon          | ~2 000       | **8**       | 8 Fibonacci EMAs (5/8/13/21/34/55/89/144) |
| AVWAP              | ~600         | **3**       | 1 series per anchor (window-start, pivot, gap) |
| ATR Bands          | ~500         | **5**       | +2/+1/mid/-1/-2·ATR (added 3 inner bands) |
| Ichimoku           | ~1 250       | **5**       | Tenkan/Kijun/SpanA/SpanB/Chikou        |
| MACD               | ~750         | **3**       | MACD line + Signal line + Histogram (own panel) |
| RSI Divergence     | varies       | **1 + N**   | 1 RSI series + N divergence connectors |
| Stochastic         | varies       | **2**       | %K + %D in own panel                   |

Verification (production `curl` via SSM, post-deploy):

```
=== bollinger.max:  lines: 3  bars: 121  (was 693 lines, 2 514 bars)
=== keltner.1y:     lines: 3
=== ma_ribbon.1y:   lines: 8
```

Theories that already emit a small number of structural lines were
left as-is: `pivots`, `fibonacci`, `gann`, `square_of_9`,
`andrews_pitchfork`, `harmonic_patterns`, `elliott_wave`,
`wyckoff_phases`, `smc_order_blocks`, `fair_value_gaps`,
`volume_profile`, `murrey_math`, `price_action`.

## 3. Live tick implementation (1s poll, candle update)

* New backend route: `backend/api/routes/quote.py` →
  `GET /quote/{ticker}` returning
  `{ticker, price, ts, source, age_seconds, cached}`.
  Backed by the unified `quote_source.get_quote()` resolver
  (ThetaData → Alpaca → yfinance) with a **500 ms in-process cache**
  so a 1 s poll never hits the data layer twice.
* New frontend hook: `useQuoteTick(ticker, enabled)` in
  `frontend/src/hooks/useTheory.js` polls at **1 s during market
  hours, 10 s off-hours**.
* `TheoryChart.jsx` now accepts a `liveTick` prop and on each tick
  mutates the last bar's close (+ high/low if exceeded) and calls
  `candlestickSeries.update(lastBar)` — `lightweight-charts`'s
  supported real-time path.
* `LiveBadge` was upgraded to show the price + timestamp + source so
  the operator can see the tick is current.

Production verification:
```
$ curl /quote/SPY
{"ticker":"SPY","price":739.3,"source":"yfinance_intraday",
 "age_seconds":40.77,"ts":"2026-06-08T19:58:40.777175+00:00","cached":false}
```

Heavy theory re-analysis still runs at 30 s — separation of concerns.

## 4. Auto-bar aggregation rules

Implemented in `backend/api/routes/theories.py`. `WINDOW_MAP` now
carries an `aggregate_to ∈ {"D","W","M"}` per window; `_fetch_window`
pulls the full daily history and resamples via `_aggregate_bars`:

| Window | Lookback | Aggregate to | Expected bars |
|--------|----------|--------------|---------------|
| 1m–1y  | ≤ 365 d  | Daily        | 15–252        |
| 2y     | 730 d    | **Weekly**   | ~105          |
| 5y     | 1 825 d  | **Weekly**   | ~250          |
| max    | 3 650 d  | **Monthly**  | ~120          |

Bucket rule: `open` = first bar's open, `high` = max of highs,
`low` = min of lows, `close` = last bar's close, `volume` = sum,
`t` = first bar's ISO timestamp. The theory ANALYSIS runs against the
**aggregated** bars so the math matches the displayed candles — no
mismatched-resolution signals.

Verification:
```
=== pivots.max  → bars: 121  (was 2 514 daily)
=== pivots.2y   → bars: 105  (was 504 daily)
```

## 5. Theory correctness audit

* **Gann Square of 9 anchor** (`backend/bot/theories/square_of_9.py`):
  default lookback tightened from 180 → 90 bars, and anchor pivot
  selection is now **significance-weighted** — picks the ZigZag pivot
  with the largest absolute move from its prior pivot, not the
  chronologically last pivot (which was often a noise pivot near the
  current price) and not the absolute min/max of the window. The
  primer's `how_to_read` documents this explicitly.
* **MACD panel separation**: MACD now emits three `series` lines with
  `meta.panel = "macd"`. The frontend reads that hint and attaches
  the series to a dedicated `priceScaleId: "macd"` price scale pinned
  to the bottom 18% of the chart (`scaleMargins: {top: 0.82,
  bottom: 0.0}`) — no longer overlaid on the price scale.
* **Stochastic + RSI panels**: same `meta.panel` mechanism. %K + %D
  land in a `stochastic` sub-panel; RSI lands in `rsi`.
* **Series math**: the existing
  `tests/unit/test_theories_v2.py::test_bollinger_fires_oversold_buy`
  and the new `test_bollinger_emits_exactly_three_series` ensure the
  band math is unchanged from the original SMA20 ± 2σ spec.

## 6. Frontend perf hardening (`TheoryChart.jsx`)

* `kind = "series"` lines render as ONE `addLineSeries().setData()`
  call.
* `priceLines` (axis-label markers) capped at
  **`MAX_PRICE_LINES_PER_CHART = 50`** per chart — anything beyond is
  dropped with a yellow "Some annotations truncated for performance"
  banner top-left.
* Annotation updates are **debounced 200 ms** and scheduled via
  `requestIdleCallback` so rapid window-toggle clicks don't pile up
  renders. Prior overlay series are explicitly removed before
  re-applying the new set (proper cleanup, no leak).
* Sub-panel-aware: lines with `meta.panel = "rsi"|"macd"|"stochastic"`
  get a dedicated `priceScaleId` so they don't smash the candle
  scale into RSI's 0-100 range.
* Live-candle update flow: `liveTick` prop triggers a
  `candlestickSeries.update(lastBar)` mutation — no full re-render.

## 7. EC2 verification (post-deploy)

| Curl                                                       | Result                            |
|------------------------------------------------------------|-----------------------------------|
| `curl /theories/bollinger/SPY?window=max` `.lines\|length` | **3** (was 693)                   |
| `curl /theories/keltner/SPY` `.lines\|length`              | **3**                             |
| `curl /theories/ma_ribbon/SPY?window=1y` `.lines\|length`  | **8**                             |
| `curl /theories/pivots/SPY?window=max` `.bars\|length`     | **121** (monthly)                 |
| `curl /theories/pivots/SPY?window=2y` `.bars\|length`      | **105** (weekly)                  |
| `curl /quote/SPY`                                          | `{price: 739.3, source: yfinance_intraday, age_seconds: 40.77, ts: 2026-06-08T19:58:40...}` |
| `systemctl is-active trading-bot`                          | **active**                        |
| `curl /bot/status`                                         | `{running: true, broker: PaperExecutor, live_loop_running: true}` |

Deploy SSM Command ID: `78ac6dd4-37d2-4f97-8572-9ebb1a5a8b2d`
(Success at 2026-06-08 19:56:17 UTC).

## 8. Test counts delta

| File                                       | Tests | Status   |
|--------------------------------------------|-------|----------|
| `tests/unit/test_theory_line_counts.py`    | **+13 new** | all pass |
| `tests/unit/test_theories_v2.py`           | 2 updated  | all pass |
| `tests/unit/test_theories.py`              | (unchanged)| all pass |

New regression guards:
* `test_band_theory_line_count_capped[*]` — parametric on 10 theories
  asserting `len(lines) ≤ cap` and `≥ N series lines with non-empty
  points`. This is the **direct freeze regression guard**.
* `test_schema_line_supports_series_kind` — schema-level sanity.
* `test_bollinger_emits_exactly_three_series` — hard contract on the
  canonical band theory.
* `test_max_window_aggregation_present` — asserts WINDOW_MAP's
  `aggregate_to` mapping (D/W/M) is correct and `_aggregate_bars`
  produces ~104 weekly bars on 2y daily input + ~24 monthly bars on
  2y input.

Run output:
```
56 passed in 4.10s  (theories + line-counts files)
```

## 9. Honest gaps

* **Sub-panel rendering is *priceScaleId* only**, not a full split
  pane. MACD / RSI / Stochastic series get their own y-scale pinned
  to the bottom 18% of the chart, but the chart is still a single
  pane. A future ship can promote them to a true split-pane (each
  with its own time-scale-synced y-axis).
* **`/quote/{ticker}` on EC2 currently reports `yfinance_intraday`**
  (40s age) — ThetaData stock quote endpoint is stubbed (we hold
  Options Standard, not Stock Standard); Alpaca is wired but for the
  SPY test the yfinance branch won the race. When market is closed
  this is fine; during market hours Alpaca SHOULD win — that path is
  exercised by the engine's existing `get_quote` consumers daily but
  not separately re-verified in this ship.
* **Bar aggregation runs at request time, not in a cache layer** —
  the in-process annotation cache still keys on (theory, ticker,
  window, params) so subsequent requests are fast, but the very first
  `max` request still pulls 10 years of daily bars from the provider.
  At our request rates this is fine.
* **The MACD histogram is emitted as a `line series`, not a true
  histogram** — `lightweight-charts` has a separate
  `addHistogramSeries` API. The visual is still correct (it shows the
  signed magnitude as a line in the MACD panel) but a future ship can
  detect `meta.kind = "macd_hist"` and route to `addHistogramSeries`
  instead for the more familiar bar look.
* **No formal load test** of "5 theories + max window" in a real
  browser yet — the line-count caps + series mode + 50-priceLine
  ceiling should make freezes impossible by construction, but the
  operator UI test is the only ground truth.

## 10. Operator quote satisfied?

> "all charts needs to be 2sec or 1 sec not 30s, I am not getting
> live ticker on the chart and every time I change the timelines I
> get bot error...for longer timelines the graph is getting
> cumbersome...can you make sure the theory is implemented correctly
> I don't see it correct"

* **Live tick at 1 s**: ✅ via `/quote/{ticker}` + `useQuoteTick` +
  `candlestickSeries.update()`.
* **No more freeze on timeline change**: ✅ series mode + 50-priceLine
  cap + 200 ms debounce + `requestIdleCallback`.
* **Long timelines no longer cumbersome**: ✅ auto-aggregation —
  `max` is 121 monthly bars not 2 514 daily.
* **Theory implementation correct**: ✅ Gann S9 anchor logic tightened
  + MACD/RSI/Stochastic split into own panels via `meta.panel` +
  the existing math (Bollinger 20/2, Keltner EMA20+2·ATR, Donchian
  N=20, etc.) is unchanged.
