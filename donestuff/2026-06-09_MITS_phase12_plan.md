# MITS Phase 12 — Institutional Detection Layer Rebuild

**Created:** 2026-06-09
**Operator directive:** "Remove ghost-strategy patterns and build something extensively used by major market firms and great strategists. Fix all the gaps. No shortcuts."

## Audit findings driving this rebuild

- 41 detectors registered; baseline 5d win rate on the universe is **68.9%**
- Only **4 detectors** have meaningful edge (>5pp above baseline): `talib_hanging_man`, `vwap_rejection`, `vwap_reclaim`, `breakout`
- **14 detectors** are statistically WORSE than baseline (negative edge): `choch` -5.5pp, `talib_inverted_hammer` -5.0pp, `talib_hammer` -4.8pp, `lvn_rejection` -2.9pp, `bos` -1.8pp, `consolidation` -1.7pp, etc.
- 9 ghost-strategy patterns polluting observations (bull_call_spread, iron_condor, etc.)
- 83% of knowledge_graph cells have N<30 (below operator-spec confidence floor)
- VWAP detectors only fire on 9/40 tickers despite being top-3 most predictive (coverage bug)
- flow_intel family: 6 detectors, 0 historical observations
- options_intel: 3 detectors, barely fire (broken on historical chains)
- Pre-2021 observations reference bar data that no longer exists

## Ship scope — 17 new institutional detectors + cleanup + cohort fix

### Sub-phases

| # | Name | Detectors / Work |
|---|---|---|
| 12.A | Cleanup | Drop 9 ghost patterns; wipe pre-2021 obs; disable 14 below-baseline detectors via DetectorConfig; fix VWAP coverage 9→40 tickers |
| 12.B | Smart Money Concepts | `order_block`, `fair_value_gap`, `liquidity_sweep_v2`, `stop_hunt_v2`, `premium_discount_zone`, `market_structure_shift_v2` — 6 detectors (ICT/Huddleston, peer-validated definitions) |
| 12.C | Wyckoff Method | `wyckoff_accumulation_phase`, `wyckoff_distribution_phase`, `wyckoff_spring`, `wyckoff_sos`, `wyckoff_upthrust` — 5 detectors (Pruden 2007 refinement) |
| 12.D | Volume Profile v2 | `poc_retest`, `value_area_rejection`, `composite_value_area` — 3 detectors (Steidlmayer 1984) |
| 12.E | Catalyst-driven | `pead_drift` (Bernard & Thomas 1989), `insider_cluster` (Lakonishok & Lee 2001), `smart_money_inflow` (Cohen et al 2010), `earnings_revision_shift` — 4 detectors |
| 12.F | Macro regime | `yield_curve_inversion`, `credit_spread_widening`, `dollar_strength_shift`, `composite_macro_regime` — 4 detectors using 46 FRED series |
| 12.G | Quantitative | `cross_sectional_momentum`, `mean_reversion_z`, `sector_dispersion` — 3 detectors (AQR / quant standard) |
| 12.H | Hierarchical cohort fix | Cross-ticker pooling priors + N<30 hard floor at consumer layer + reduce cohort axes |
| 12.I | Full replay | Run all 17 new detectors on the 5y corpus + outcome linker + aggregator |
| 12.J | UI scorecard | Per-detector edge-vs-baseline; auto-suggest disable for new negatives |

### Detector academic citations

All implementations cite the original source in module docstring:

- **SMC**: Huddleston "Inner Circle Trader" lecture series (2017-2024); academic backing in Bulkowski "Encyclopedia of Chart Patterns" (Wiley 2005)
- **Wyckoff**: Pruden "The Three Skills of Top Trading" (Wiley 2007); original Richard Wyckoff "A Course in Stock Market Science" (1931)
- **Volume Profile**: Steidlmayer "Steidlmayer on Markets" (Wiley 1989); Dalton "Mind Over Markets" (Probus 1990)
- **PEAD**: Bernard & Thomas "Post-Earnings-Announcement Drift" (JAR 1989); also Foster, Olsen & Shevlin (1984)
- **Insider clustering**: Lakonishok & Lee "Are Insider Trades Informative?" (RFS 2001)
- **13F smart money**: Cohen, Polk, Silli "Best Ideas" (NBER 2010)
- **Cross-sectional momentum**: Jegadeesh & Titman (JF 1993); AQR "Value and Momentum Everywhere" (Asness 2013)
- **Mean reversion**: De Bondt & Thaler (JF 1985)

## Verification gates (after ship)

- Cleanup: 9 ghost patterns gone; pre-2021 rows wiped; 14 disabled detectors hidden from /detectors
- 17 new detectors registered in /detectors endpoint
- Each new detector produces ≥100 observations on the 5y corpus
- At least 10 of 17 beat baseline by ≥5pp (5d win rate)
- Knowledge graph cells with N≥30 jumps from 17% → ≥40%
- Scorecard UI shows per-detector edge metric
- Phase 7 Opportunity Brain references new detector posteriors

## Status

- **Plan locked:** 2026-06-09
- **Build starting:** immediately
