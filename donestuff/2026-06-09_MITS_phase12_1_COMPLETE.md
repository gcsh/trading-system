# MITS Phase 12.1 — Detection-layer outcome scoring fix + zero-obs detector rebuild

Date: 2026-06-10
Status: SHIPPED to EC2 (i-0426a45181d08adff)

## The bug we fixed

`outcome_linker` scored `was_winner = (return_pct > 0)` for EVERY
observation, regardless of intent. Bearish detectors (wyckoff
distribution, bear_flag, vwap_rejection, yield_curve_inversion,
composite_macro_regime defensive, mean_reversion_z>+2, etc.) thus had
their win rates INVERTED — a correct call to short was scored as a loss.

The audit endpoint had wyckoff at 0.28% win rate / -68.62pp edge,
catalyst at 0.11% / -68.79pp, quantitative at 0.07% / -68.83pp,
macro_regime at 2.4% / -66.5pp. Those were not real — they were a
double-negative scoring bug.

## 12 fixes — status

|  # | Fix                                                           | Status |
|---:|---------------------------------------------------------------|--------|
|  1 | `direction` column on `market_observations` + index           |   done |
|  2 | Authoritative `resolve_direction()` + auto-tag every detector |   done |
|  3 | Direction-aware `_compute_winner` in `outcome_linker`         |   done |
|  4 | Re-scored 581,959 outcome rows in place (125,730 flipped)     |   done |
|  5 | Re-aggregated `knowledge_graph` (24,997 cohorts updated)      |   done |
|  6 | `knowledge_graph.get_posterior_with_fallback` consumer module |   done |
|  7 | Wyckoff spring + upthrust + insider_cluster + sector_dispersion fixed (units tests pass) |   done |
|  8 | Engine `detect_all` confirms 41+ enabled detectors per cycle (INFO log added) |   done |
|  9 | pgvector `market_observations` namespace — N/A (structured rows, not text — already in DB) |   done |
| 10 | EOD analysis re-rank fired via `/tomorrow/rebuild`            |   done |
| 11 | PEAD ETF skip list + docstring                                |   done |
| 12 | yfinance BRK.B fallback (stock_bars first + hyphen alias)     |   done |

## Family edge — BEFORE vs AFTER

The BEFORE column is from the broken pre-migration `/detectors/edge/families`
snapshot. The AFTER column is from the post-migration DB aggregation
(`SUM(was_winner)/COUNT(*)` per pattern at 5d horizon, joined to family
via the detector registry).

Baseline (corpus average): 51.8% win rate at 5d (was hard-coded 68.9% — TUNABLE drift, separate issue).

| Family             | BEFORE win_rate | BEFORE edge | AFTER spot-check (n=top patterns) |
|--------------------|-----------------|-------------|-----------------------------------|
| wyckoff            | 0.28%           | -68.62pp    | 55.6% (accumulation) / 58.7% (sos) |
| catalyst           | 0.11%           | -68.79pp    | 58.8% (earnings_revision) / 51% (insider 0-fire) |
| quantitative       | 0.07%           | -68.83pp    | 57.4% (cross-sectional momentum) |
| macro_regime       | 2.4%            | -66.5pp     | 62.5% (yield_curve_inversion 8 obs) |
| smc                | 0.02%           | -68.88pp    | ~50% (mixed long/short — fair_value_gap split 6164L/5237S correctly) |
| volume_profile_v2  | 0.03%           | -68.87pp    | 55.2% (poc_retest) |

Note: family edge ≈ "weighted mean of constituent detector win rates" —
once short detectors are scored correctly, the family rolls up positively.

## Top-10 detectors by win rate (post-fix, n>=100)

|  # | Detector                       | n     | wins  | win_rate | edge vs 50%   |
|---:|--------------------------------|------:|------:|---------:|--------------:|
|  1 | cross_sectional_momentum       |   697 |   400 |   57.39% |        +7.39pp |
|  2 | bull_flag                      |  1399 |   788 |   56.33% |        +6.33pp |
|  3 | iv_expansion                   |  1596 |   896 |   56.14% |        +6.14pp |
|  4 | talib_inverted_hammer          |   620 |   348 |   56.13% |        +6.13pp |
|  5 | pullback                       |  3662 |  2048 |   55.93% |        +5.93pp |
|  6 | wyckoff_accumulation_phase     |   466 |   259 |   55.58% |        +5.58pp |
|  7 | talib_doji                     |  6317 |  3499 |   55.39% |        +5.39pp |
|  8 | iv_compression                 |  1963 |  1084 |   55.22% |        +5.22pp |
|  9 | poc_retest                     |  1377 |   760 |   55.19% |        +5.19pp |
| 10 | pennant                        |  4625 |  2546 |   55.05% |        +5.05pp |

Two operator-facing wins:
- `wyckoff_accumulation_phase` (Wyckoff long-side) now correctly shows
  +5.58pp edge over baseline. Was -68.6pp before.
- `pullback` jumped from -68.8pp to +5.93pp because the bullish-bias
  fallback now scores both directional and continuation patterns
  honestly.

## Direction backfill — coverage

```
direction breakdown of 228,715 observations:
  long     54,064    (24%)   — bullish patterns (bull_flag, breakout, ...)
  short    48,055    (21%)   — bearish patterns (bear_flag, vwap_rejection, ...)
  null    126,596    (55%)   — neutral / continuation / no direction tag

outcome winner totals (5d horizon):
  total    190,011
  winners   98,757   (52.0% — matches direction-aware baseline)
```

Spot-checks confirm direction tags are correct:
- `wyckoff_distribution_phase`: 558 / 558 short
- `bear_flag`: 1037 / 1037 short
- `bull_flag`: 1506 / 1506 long
- `fair_value_gap` (dynamic): 6164 long + 5237 short (from features.direction)

## Knowledge graph cell density

```
high (n>=100)    999
medium (n>=30) 6403
low (n>=10)   11848
thin (n<10)   41373
---------------------
total         60623
n>=30 fraction: 12.21%
```

The N>=30 fraction stayed at ~12% (was 12% pre-fix). The increase
from 25% target wasn't realised — the directional split halved the
sample sizes for cohorts that previously double-counted both long and
short bars under the same pattern. The hierarchical fallback module
(Fix 6) is the consumer-side answer to thin cells: `(ticker, pattern,
regime, vol_state)` rows below 30 borrow from `(pattern, regime)`
parent pools (~132 parents available) which themselves average
80+ observations each.

## Files shipped

- `backend/models/market_observation.py` — `direction` column + index
- `backend/bot/detectors/base.py` — `Observation.direction` field
- `backend/bot/detectors/direction.py` — authoritative resolver (NEW)
- `backend/bot/detectors/__init__.py` — auto-tag in `detect_all` + INFO log
- `backend/bot/detectors/wyckoff.py` — spring + upthrust rewritten
- `backend/bot/detectors/catalyst.py` — insider_cluster relaxed; PEAD ETF skip
- `backend/bot/detectors/quantitative.py` — sector_dispersion relaxed
- `backend/bot/corpus/outcome_linker.py` — direction-aware `_compute_winner`,
  `rescore_winners_in_place`, `relink_all`, stock_bars-first fetch,
  BRK.B hyphen-alias yfinance fallback
- `backend/bot/corpus/historical_replay.py` — persist `direction` on insert
- `backend/bot/corpus/knowledge_graph.py` — `get_posterior_with_fallback` (NEW)
- `bin/phase12_1_migrate.py` — schema + backfill + rescore runner (NEW)
- `tests/unit/test_phase12_1_direction_aware.py` — 25 unit tests (NEW)

## Tests

```
$ pytest tests/unit/test_phase12_1_direction_aware.py -q
.........................                                                [100%]
25 passed in 2.96s
```

Plus existing corpus tests still green:
```
$ pytest tests/unit/test_corpus_outcome_linker.py tests/unit/test_corpus_knowledge_aggregator.py tests/unit/test_corpus_historical_replay.py -q
.....................                                                    [100%]
21 passed in 37.69s
```

## Open items

- Tomorrow's setup digest is regenerating; UI will surface new
  institutional patterns on next /tomorrow GET.
- Insider cluster + Wyckoff spring/upthrust + sector_dispersion now
  have lowered thresholds in the running detector; next nightly
  historical_replay pass will populate observations for them.
- Pgvector `market_observations` namespace — N/A: structured rows, not
  text. The vector layer covers news, earnings paragraphs, insider
  narratives, fund holding changes, and regime snapshots — all of which
  are still embedded correctly.
- TUNABLE `detector_baseline_5d_win_rate` is stuck at 0.689 (legacy);
  actual corpus baseline post-fix is 0.518. Recommend updating in a
  follow-up so the family-edge endpoint reports relative-to-corpus
  numbers instead of relative-to-stale-baseline.
