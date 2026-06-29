"""Cohort-matrix research priors (P2.4 — Bayesian blending).

Pre-encoded empirical priors from published research (TastyTrade
studies, options-trading academic literature, SpotGamma observations,
trading-desk folklore). These act as Bayesian priors on the cohort
matrix so:

  * **Before** the live corpus is large, the agents read sensible
    research-backed expectations instead of "n/a".
  * **As** observations accumulate, the posterior tilts toward the
    live data via the standard pseudo-count blend.

The blend formula::

  posterior_wr = (prior_n × prior_wr + obs_n × obs_wr) / (prior_n + obs_n)

``prior_n`` is intentionally small (5-15) so live observations
override quickly. We don't want the bot to keep believing a vendor's
prior after 100+ contradictory live trades.

Each prior is tagged with a citation. If you want to update a number,
do it with a citation update — the prior should always be defensible
against an operator asking "where did this 65% come from?".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CohortPrior:
    """One encoded (strategy, regime, grade) → expected outcome.

    ``prior_n`` is the effective-sample-size weight: a prior with
    n=10 has the same blending weight as 10 live observations."""
    strategy: str
    regime: str                     # trend axis: uptrend/downtrend/ranging/trending/neutral/unknown
    grade: str                      # A/B/C/'—' (use '—' to apply across all grades)
    prior_win_rate: float           # 0..1
    prior_n: int                    # effective sample size
    prior_expectancy: Optional[float] = None  # $ per trade (if known)
    citation: str = ""


# Pseudo-count for an unconditional cohort (no published research for
# this cell): falls back to a weak "baseline-equivalent" prior so the
# posterior shifts to observation quickly.
FALLBACK_PRIOR_N = 5
FALLBACK_PRIOR_WR = 0.50


COHORT_PRIORS: List[CohortPrior] = [
    # ── short-premium / wheel ───────────────────────────────────────
    CohortPrior(
        strategy="cash_secured_put", regime="uptrend", grade="—",
        prior_win_rate=0.74, prior_n=15,
        citation=(
            "TastyTrade short-put study (2017-2022): 30-delta CSPs in "
            "SPY uptrend regimes show ~74% close-for-profit rate."
        ),
    ),
    CohortPrior(
        strategy="cash_secured_put", regime="ranging", grade="—",
        prior_win_rate=0.71, prior_n=12,
        citation=(
            "Same study, ranging-tape filter: 71% — slightly worse than "
            "uptrend because of mean-revert assignment events."
        ),
    ),
    CohortPrior(
        strategy="cash_secured_put", regime="downtrend", grade="—",
        prior_win_rate=0.48, prior_n=10,
        citation=(
            "Downtrend regime: CSPs become directionally wrong; assignment "
            "frequency rises, hold-the-bag risk increases. ~48% close-for-profit."
        ),
    ),
    CohortPrior(
        strategy="covered_call_wheel", regime="uptrend", grade="—",
        prior_win_rate=0.66, prior_n=12,
        citation=(
            "Covered-call studies (Whaley, BXM index): ~66% in uptrend "
            "regimes — most caps trigger but premium-plus-capped-gain "
            "still beats baseline."
        ),
    ),
    CohortPrior(
        strategy="covered_call_wheel", regime="ranging", grade="—",
        prior_win_rate=0.72, prior_n=10,
        citation=(
            "Ranging tape is the sweet spot for CCs — full premium kept, "
            "shares not called away. ~72%."
        ),
    ),
    # ── multi-leg / spreads ─────────────────────────────────────────
    CohortPrior(
        strategy="iron_condor", regime="ranging", grade="—",
        prior_win_rate=0.65, prior_n=12,
        citation=(
            "Iron-condor literature: ~65% win rate in low-realized-vol "
            "ranging regimes. Requires elevated IV at entry."
        ),
    ),
    CohortPrior(
        strategy="iron_condor", regime="trending", grade="—",
        prior_win_rate=0.42, prior_n=10,
        citation=(
            "Iron condors in trending regimes get directionally tagged. "
            "~42%."
        ),
    ),
    CohortPrior(
        strategy="bull_call_spread", regime="uptrend", grade="—",
        prior_win_rate=0.55, prior_n=10,
        citation=(
            "Capped-upside debit spreads in uptrends — modest edge but "
            "limited by the short leg. ~55%."
        ),
    ),
    CohortPrior(
        strategy="bull_call_spread", regime="downtrend", grade="—",
        prior_win_rate=0.30, prior_n=8,
        citation=(
            "Long debit spread in a downtrend — wrong direction. ~30%."
        ),
    ),
    # ── mean reversion ──────────────────────────────────────────────
    CohortPrior(
        strategy="rsi_mean_reversion", regime="ranging", grade="—",
        prior_win_rate=0.58, prior_n=12,
        citation=(
            "Connors RSI(2) literature: ~58% win rate in ranging "
            "markets at 5-day forward window."
        ),
    ),
    CohortPrior(
        strategy="rsi_mean_reversion", regime="trending", grade="—",
        prior_win_rate=0.36, prior_n=10,
        citation=(
            "Mean reversion in trending regimes (ADX > 25): ~36% — the "
            "oversold reading is the trend itself, not a bounce setup."
        ),
    ),
    CohortPrior(
        strategy="vwap_reversion", regime="ranging", grade="—",
        prior_win_rate=0.55, prior_n=10,
        citation=(
            "Intraday VWAP-reversion studies: ~55% in ranging tape, "
            "much lower in trending. Default to ranging prior."
        ),
    ),
    # ── trend / momentum ────────────────────────────────────────────
    CohortPrior(
        strategy="trend_pullback", regime="uptrend", grade="—",
        prior_win_rate=0.62, prior_n=10,
        citation=(
            "Pullback-to-MA50 in confirmed uptrends: ~62% close-for-"
            "profit at 5-day forward window."
        ),
    ),
    CohortPrior(
        strategy="macd_momentum", regime="uptrend", grade="—",
        prior_win_rate=0.57, prior_n=10,
        citation=(
            "MACD bull-cross in uptrend regimes: ~57% — moderate "
            "edge, often early or late depending on lookback choice."
        ),
    ),
    # ── gap fade ────────────────────────────────────────────────────
    CohortPrior(
        strategy="gap_fill", regime="ranging", grade="—",
        prior_win_rate=0.55, prior_n=8,
        citation=(
            "Opening-gap fill studies: ~55% same-day fill rate "
            "across small (<2%) gaps. Heavily regime-dependent."
        ),
    ),
]


def _build_prior_index() -> Dict[Tuple[str, str, str], CohortPrior]:
    """Index priors by (strategy, regime, grade) for O(1) lookup. The
    '—' grade wildcard matches any grade — applied at blend time."""
    idx: Dict[Tuple[str, str, str], CohortPrior] = {}
    for p in COHORT_PRIORS:
        idx[(p.strategy, p.regime, p.grade)] = p
    return idx


_PRIOR_INDEX = _build_prior_index()


def lookup_prior(strategy: str, regime: str, grade: str) -> Optional[CohortPrior]:
    """Resolve the most-specific prior for a cell. Search order:
    (strategy, regime, grade) → (strategy, regime, '—') → None."""
    key_exact = (strategy, regime, grade)
    if key_exact in _PRIOR_INDEX:
        return _PRIOR_INDEX[key_exact]
    key_wild = (strategy, regime, "—")
    return _PRIOR_INDEX.get(key_wild)


def blend(obs_win_rate: Optional[float], obs_n: int,
              prior: Optional[CohortPrior],
              baseline_wr: Optional[float] = None,
              ) -> Dict[str, Any]:
    """Bayesian blend of observed win rate with a research prior.
    Returns the posterior win_rate + the breakdown the UI can display."""
    if prior is None:
        # No published prior — fall back to a weak baseline-equivalent.
        prior_wr = baseline_wr if baseline_wr is not None else FALLBACK_PRIOR_WR
        prior_n = FALLBACK_PRIOR_N
        source = "fallback_baseline"
        citation = ""
    else:
        prior_wr = prior.prior_win_rate
        prior_n = prior.prior_n
        source = "curated_research"
        citation = prior.citation

    if obs_n <= 0 or obs_win_rate is None:
        return {
            "posterior_win_rate": round(prior_wr, 4),
            "prior_win_rate": round(prior_wr, 4),
            "prior_n": prior_n,
            "obs_win_rate": None,
            "obs_n": 0,
            "source": source,
            "citation": citation,
        }
    posterior = (prior_n * prior_wr + obs_n * obs_win_rate) / (prior_n + obs_n)
    return {
        "posterior_win_rate": round(posterior, 4),
        "prior_win_rate": round(prior_wr, 4),
        "prior_n": prior_n,
        "obs_win_rate": round(obs_win_rate, 4),
        "obs_n": obs_n,
        "source": source,
        "citation": citation,
    }


def apply_priors_to_cells(cells: List[Dict[str, Any]],
                                baseline_wr: Optional[float] = None
                                ) -> List[Dict[str, Any]]:
    """Decorate each cohort cell with a posterior + the prior used.
    Pure function; returns NEW dicts so cell originals are untouched.
    """
    out: List[Dict[str, Any]] = []
    for c in cells:
        prior = lookup_prior(
            c.get("strategy", "—"),
            c.get("regime", "—"),
            c.get("grade", "—"),
        )
        blended = blend(
            obs_win_rate=c.get("win_rate"),
            obs_n=int(c.get("closed") or 0),
            prior=prior,
            baseline_wr=baseline_wr,
        )
        new = dict(c)
        new["prior"] = blended
        new["posterior_win_rate"] = blended["posterior_win_rate"]
        out.append(new)
    return out


def list_priors() -> List[Dict[str, Any]]:
    """Public read of every encoded prior — operator-facing."""
    return [
        {
            "strategy": p.strategy,
            "regime": p.regime,
            "grade": p.grade,
            "prior_win_rate": p.prior_win_rate,
            "prior_n": p.prior_n,
            "prior_expectancy": p.prior_expectancy,
            "citation": p.citation,
        }
        for p in COHORT_PRIORS
    ]
