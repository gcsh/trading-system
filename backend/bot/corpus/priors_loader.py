"""MITS Phase 0 — academic / external prior loader.

Hardcoded baseline priors per (pattern, cohort_descriptor). Loaded once
at startup or on demand. Idempotent — upserts on the composite unique
constraint (pattern, cohort_descriptor).

Sources roughly correspond to:
  * Bulkowski's "Encyclopedia of Chart Patterns" — flag-pattern win rates.
  * Bessembinder (2018) — momentum continuation odds.
  * Carhart (1997) — momentum factor literature.
  * Conrad-Kaul (1998) — short-horizon mean reversion.
  * "academic" / "TA-Lib lit" — generic placeholders for patterns where
    the literature is mixed; the corpus will refine these via Bayesian
    update.

All `prior_win_rate` values are conservative: we don't want strong
priors to dominate the empirical evidence as the corpus grows. The
`prior_weight` field controls how many pseudo-observations the prior
counts for during shrinkage — see ``knowledge_aggregator``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import select

from backend.db import session_scope
from backend.models.pattern_prior import PatternPrior

logger = logging.getLogger(__name__)


DEFAULT_PRIORS: List[Dict[str, Any]] = [
    # Flag patterns — Bulkowski-style.
    {"pattern": "bull_flag", "cohort_descriptor": "trending_up",
     "prior_win_rate": 0.62, "prior_weight": 20, "source": "Bulkowski",
     "notes": "Bull flag in established uptrend."},
    {"pattern": "bear_flag", "cohort_descriptor": "trending_down",
     "prior_win_rate": 0.60, "prior_weight": 20, "source": "Bulkowski",
     "notes": "Bear flag in established downtrend."},
    {"pattern": "pennant", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 15, "source": "Bulkowski",
     "notes": "Pennant continuation — direction inherited from prior thrust."},
    # Breakout / pullback.
    {"pattern": "breakout", "cohort_descriptor": "any",
     "prior_win_rate": 0.58, "prior_weight": 20, "source": "Bessembinder",
     "notes": "Breakout from consolidation with volume confirmation."},
    {"pattern": "pullback", "cohort_descriptor": "trending_up",
     "prior_win_rate": 0.57, "prior_weight": 20, "source": "Carhart 1997",
     "notes": "Pullback inside uptrend — momentum continuation play."},
    {"pattern": "consolidation", "cohort_descriptor": "any",
     "prior_win_rate": 0.50, "prior_weight": 10, "source": "academic",
     "notes": "Pure consolidation has no directional edge by itself."},
    # Failed breakouts (reversal plays).
    {"pattern": "failed_breakout", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 15, "source": "academic",
     "notes": "Failed-breakout reversal — fade the trapped longs."},
    {"pattern": "failed_breakdown", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 15, "source": "academic",
     "notes": "Failed-breakdown reversal — fade the trapped shorts."},
    # VWAP / structure.
    {"pattern": "vwap_reclaim", "cohort_descriptor": "trending_up",
     "prior_win_rate": 0.58, "prior_weight": 20, "source": "academic",
     "notes": "VWAP reclaim in uptrend — institutional support level."},
    {"pattern": "vwap_rejection", "cohort_descriptor": "trending_down",
     "prior_win_rate": 0.56, "prior_weight": 15, "source": "academic",
     "notes": "VWAP rejection in downtrend."},
    {"pattern": "bos", "cohort_descriptor": "any",
     "prior_win_rate": 0.54, "prior_weight": 15, "source": "academic",
     "notes": "Break-of-structure continuation."},
    {"pattern": "choch", "cohort_descriptor": "any",
     "prior_win_rate": 0.52, "prior_weight": 15, "source": "academic",
     "notes": "Change-of-character — early reversal signal."},
    # Liquidity.
    {"pattern": "liquidity_sweep", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 20, "source": "academic",
     "notes": "Liquidity sweep + reversal."},
    {"pattern": "stop_hunt", "cohort_descriptor": "any",
     "prior_win_rate": 0.56, "prior_weight": 15, "source": "academic",
     "notes": "Stop hunt — deep wick reversal candle."},
    # Volume profile.
    {"pattern": "hvn_acceptance", "cohort_descriptor": "any",
     "prior_win_rate": 0.52, "prior_weight": 10, "source": "academic",
     "notes": "Acceptance at a high-volume node — consolidation prior."},
    {"pattern": "lvn_rejection", "cohort_descriptor": "any",
     "prior_win_rate": 0.54, "prior_weight": 12, "source": "academic",
     "notes": "Rejection from a low-volume node — price moves through quickly."},
    # Options intel.
    {"pattern": "iv_expansion", "cohort_descriptor": "any",
     "prior_win_rate": 0.51, "prior_weight": 10, "source": "academic",
     "notes": "IV expansion — long-premium edge in volatile regimes."},
    {"pattern": "iv_compression", "cohort_descriptor": "any",
     "prior_win_rate": 0.49, "prior_weight": 10, "source": "academic",
     "notes": "IV compression — short-premium / theta-decay setup."},
    {"pattern": "gex_acceleration", "cohort_descriptor": "any",
     "prior_win_rate": 0.53, "prior_weight": 10, "source": "academic",
     "notes": "GEX regime shift — dealer-flow inflection."},
    # Generic momentum / mean-reversion priors.
    {"pattern": "momentum_continuation", "cohort_descriptor": "any",
     "prior_win_rate": 0.53, "prior_weight": 30, "source": "Carhart 1997",
     "notes": "Carhart momentum-factor prior."},
    {"pattern": "mean_reversion_weekly", "cohort_descriptor": "any",
     "prior_win_rate": 0.52, "prior_weight": 30, "source": "Conrad-Kaul 1998",
     "notes": "Short-horizon mean reversion."},
    # TA-Lib literature priors — applied to the most-cited candlesticks.
    {"pattern": "talib_hammer", "cohort_descriptor": "trending_down",
     "prior_win_rate": 0.55, "prior_weight": 15, "source": "TA-Lib lit",
     "notes": "Hammer at the end of a downtrend — reversal prior."},
    {"pattern": "talib_engulfing", "cohort_descriptor": "any",
     "prior_win_rate": 0.57, "prior_weight": 15, "source": "TA-Lib lit",
     "notes": "Engulfing — direction taken from candle color."},
    {"pattern": "talib_morning_star", "cohort_descriptor": "trending_down",
     "prior_win_rate": 0.58, "prior_weight": 15, "source": "TA-Lib lit",
     "notes": "Morning star — bullish reversal at downtrend bottom."},
    {"pattern": "talib_evening_star", "cohort_descriptor": "trending_up",
     "prior_win_rate": 0.58, "prior_weight": 15, "source": "TA-Lib lit",
     "notes": "Evening star — bearish reversal at uptrend top."},
    # ── MITS Phase 13 Fix 2 — academic priors for the 25 new
    # institutional detectors shipped in Phase 12. Each row's `source`
    # is the verbatim research citation the operator commissioned. When
    # the literature is thin (≤ 0.52) the notes call that out so future
    # operators don't tighten the weight without re-reading the source.
    # SMC family.
    {"pattern": "order_block", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 20,
     "source": "Huddleston \"ICT Mentorship 2017\"",
     "notes": "~55% return-to-OB success rate."},
    {"pattern": "fair_value_gap", "cohort_descriptor": "any",
     "prior_win_rate": 0.56, "prior_weight": 20,
     "source": "Huddleston \"ICT Imbalance Studies\"",
     "notes": "Gap-fill rate ~56% within 20 bars."},
    {"pattern": "liquidity_sweep_v2", "cohort_descriptor": "any",
     "prior_win_rate": 0.56, "prior_weight": 20,
     "source": "Bulkowski \"Encyclopedia of Chart Patterns\" 3rd ed. (Wiley 2005)",
     "notes": "Swing-chart sweep reversals."},
    {"pattern": "stop_hunt_v2", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 15,
     "source": "Brooks \"Trading Price Action Reversals\" (Wiley 2012) ch. 19",
     "notes": "Failed swing sweep + volume confirmation."},
    {"pattern": "premium_discount_zone", "cohort_descriptor": "any",
     "prior_win_rate": 0.53, "prior_weight": 15,
     "source": "Pesavento \"Fibonacci Ratios with Pattern Recognition\" (Traders Press 1997)",
     "notes": "OTE / 50% Fib midpoint entry."},
    {"pattern": "market_structure_shift_v2", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 20,
     "source": "Murphy \"Technical Analysis of the Financial Markets\" (Prentice Hall 1999)",
     "notes": "Trend-continuation studies."},
    # Wyckoff family.
    {"pattern": "wyckoff_accumulation_phase", "cohort_descriptor": "any",
     "prior_win_rate": 0.60, "prior_weight": 20,
     "source": "Pruden \"The Three Skills of Top Trading\" (Wiley 2007)",
     "notes": "Phase D entry statistics."},
    {"pattern": "wyckoff_distribution_phase", "cohort_descriptor": "any",
     "prior_win_rate": 0.58, "prior_weight": 20,
     "source": "Pruden 2007",
     "notes": "Symmetric to accumulation, short bias."},
    {"pattern": "wyckoff_spring", "cohort_descriptor": "any",
     "prior_win_rate": 0.62, "prior_weight": 25,
     "source": "Pruden 2007",
     "notes": "Spring is one of Wyckoff's highest-confidence patterns."},
    {"pattern": "wyckoff_sos", "cohort_descriptor": "any",
     "prior_win_rate": 0.58, "prior_weight": 20,
     "source": "Pruden 2007",
     "notes": "Sign of strength continuation."},
    {"pattern": "wyckoff_upthrust", "cohort_descriptor": "any",
     "prior_win_rate": 0.60, "prior_weight": 25,
     "source": "Pruden 2007",
     "notes": "Upthrust short, symmetric to spring."},
    # Volume Profile v2.
    {"pattern": "poc_retest", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 15,
     "source": "Steidlmayer \"Steidlmayer on Markets\" (Wiley 1989)",
     "notes": "Value rotation around POC."},
    {"pattern": "value_area_rejection", "cohort_descriptor": "any",
     "prior_win_rate": 0.56, "prior_weight": 15,
     "source": "Dalton \"Mind Over Markets\" (Probus 1990)",
     "notes": "VA boundary rejection."},
    {"pattern": "composite_value_area", "cohort_descriptor": "any",
     "prior_win_rate": 0.58, "prior_weight": 20,
     "source": "Dalton 1990",
     "notes": "Confluence boost in overlapping VAs."},
    # Catalyst family.
    {"pattern": "pead_drift", "cohort_descriptor": "any",
     "prior_win_rate": 0.58, "prior_weight": 25,
     "source": "Bernard & Thomas \"Post-Earnings-Announcement Drift\" (JAR 1989)",
     "notes": "4-7% abnormal return in 60d window."},
    {"pattern": "insider_cluster", "cohort_descriptor": "any",
     "prior_win_rate": 0.60, "prior_weight": 25,
     "source": "Lakonishok & Lee \"Are Insider Trades Informative?\" (RFS 2001)",
     "notes": "Clustered insider buys yield 6-7% excess return over 12mo."},
    {"pattern": "smart_money_inflow", "cohort_descriptor": "any",
     "prior_win_rate": 0.62, "prior_weight": 25,
     "source": "Cohen Polk Silli \"Best Ideas\" (NBER 2010)",
     "notes": "Top fund position changes generate 8.5% annualized alpha."},
    {"pattern": "earnings_revision_shift", "cohort_descriptor": "any",
     "prior_win_rate": 0.57, "prior_weight": 20,
     "source": "Stickel \"Reputation and Performance Among Security Analysts\" (JF 1992)",
     "notes": "Standard estimates literature."},
    # Macro regime family.
    {"pattern": "yield_curve_inversion", "cohort_descriptor": "any",
     "prior_win_rate": 0.52, "prior_weight": 10,
     "source": "Estrella & Mishkin \"Predicting US Recessions\" (REStat 1998)",
     "notes": "11 of 12 inversions preceded recession but 5d horizon is weak — literature thin at this horizon."},
    {"pattern": "credit_spread_widening", "cohort_descriptor": "any",
     "prior_win_rate": 0.54, "prior_weight": 15,
     "source": "Gilchrist & Zakrajsek \"Credit Spreads and Business Cycle Fluctuations\" (AER 2012)",
     "notes": "Credit-spread shocks lead equity drawdowns."},
    {"pattern": "dollar_strength_shift", "cohort_descriptor": "any",
     "prior_win_rate": 0.51, "prior_weight": 10,
     "source": "Engel \"Macro Linkages — Currency and Equities\" (JIE 2014)",
     "notes": "Weak short-term equity signal — literature thin."},
    {"pattern": "composite_macro_regime", "cohort_descriptor": "any",
     "prior_win_rate": 0.55, "prior_weight": 15,
     "source": "Composite (Estrella/Mishkin 1998 + Gilchrist/Zakrajsek 2012)",
     "notes": "Composite of yield + credit + dollar regime signals."},
    # Quantitative family.
    {"pattern": "cross_sectional_momentum", "cohort_descriptor": "any",
     "prior_win_rate": 0.54, "prior_weight": 30,
     "source": "Jegadeesh & Titman \"Returns to Buying Winners and Selling Losers\" (JF 1993)",
     "notes": "12-1 momentum factor."},
    {"pattern": "mean_reversion_z", "cohort_descriptor": "any",
     "prior_win_rate": 0.53, "prior_weight": 20,
     "source": "De Bondt & Thaler \"Does the Stock Market Overreact?\" (JF 1985)",
     "notes": "Z-score reversion at multi-week horizons."},
    {"pattern": "sector_dispersion", "cohort_descriptor": "any",
     "prior_win_rate": 0.50, "prior_weight": 10,
     "source": "Regime indicator only (no directional prior)",
     "notes": "Dispersion measures breadth regime — direction comes from sibling detectors."},
]


def load_default_priors() -> Dict[str, int]:
    """Idempotent insert of the hardcoded priors. Returns counts."""
    stats = {"inserted": 0, "updated": 0, "errors": 0,
              "total": len(DEFAULT_PRIORS)}
    try:
        with session_scope() as s:
            for spec in DEFAULT_PRIORS:
                row = s.execute(
                    select(PatternPrior)
                    .where(PatternPrior.pattern == spec["pattern"])
                    .where(PatternPrior.cohort_descriptor == spec["cohort_descriptor"])
                ).scalar_one_or_none()
                if row is None:
                    s.add(PatternPrior(**spec))
                    stats["inserted"] += 1
                else:
                    # Preserve source if operator edited it manually;
                    # always refresh win_rate + weight so updates to
                    # this file propagate.
                    row.prior_win_rate = float(spec["prior_win_rate"])
                    row.prior_weight = int(spec["prior_weight"])
                    if not row.source:
                        row.source = spec.get("source", "")
                    if not row.notes:
                        row.notes = spec.get("notes")
                    stats["updated"] += 1
    except Exception:
        logger.exception("load_default_priors failed")
        stats["errors"] += 1
    return stats


if __name__ == "__main__":  # pragma: no cover — CLI helper
    import json
    from backend.db import init_db
    init_db()
    report = load_default_priors()
    print(json.dumps(report, indent=2))
