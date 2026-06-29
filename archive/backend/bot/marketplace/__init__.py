"""Stage-13.D10 Decision Marketplace.

Today the engine fires signals per-ticker in order: each independent
candidate is evaluated, risk-sized, and executed if it passes gates. This
maximizes *every triggered opportunity* but can't choose between two
competing signals — it just takes both.

The Marketplace flips that around:

  1. Generate every candidate signal first (no execution yet)
  2. Score each on (expected_return × probability) - (expected_risk + cost)
  3. Hand the bag to a portfolio optimizer that picks the **subset** that
     maximizes total score subject to capital + position-count constraints
  4. Execute only the chosen subset

This is the move from "trade what triggers" to "select the best opportunity
set in the cycle". The architectural lift is bounded:

  • Existing per-signal evaluation logic is **unchanged** — it just runs
    inside ``score_candidate`` instead of inline in ``run_cycle``.
  • Selection is greedy by score-per-dollar (knapsack-ish heuristic), not
    a true MIP — fast enough for tens of candidates per cycle.
  • **Flag-gated**: only active when ``config.marketplace_enabled = True``.
    Default off so existing behavior is preserved bit-for-bit.

This module is **read-only over the candidate pool**. Execution happens
back in ``engine.run_cycle`` for the selected candidates.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """One opportunity in the marketplace. Synthesized from a Signal +
    its evaluation context."""
    ticker: str
    action: str
    strategy: str
    expected_return_pct: float       # decimal, e.g. 0.05 = +5%
    expected_risk_pct: float         # decimal, downside cap (stop %)
    probability: float               # 0-1
    capital_required: float          # $ at risk
    liquidity_score: float           # 0-1 (1 = very liquid)
    confidence: float                # 0-1 (model + agents)
    expected_value: float = 0.0      # computed: prob × ret - (1-prob) × risk
    score: float = 0.0               # composite ranking score
    score_per_dollar: float = 0.0    # score / capital_required
    selected: bool = False
    rejection_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _compute_scores(c: Candidate) -> None:
    """Fill ``expected_value``, ``score``, ``score_per_dollar`` in place."""
    p = max(0.0, min(1.0, c.probability))
    ev_pct = p * c.expected_return_pct - (1 - p) * c.expected_risk_pct
    c.expected_value = round(ev_pct * c.capital_required, 2)
    # Score blends EV with confidence + liquidity (both quality multipliers).
    qual = (0.6 + 0.4 * c.confidence) * (0.7 + 0.3 * c.liquidity_score)
    c.score = round(c.expected_value * qual, 4)
    c.score_per_dollar = round(c.score / c.capital_required, 6) \
        if c.capital_required > 0 else 0.0


# ── synthesis helper (used by engine to convert a Signal → Candidate) ────


def candidate_from(*,
                      ticker: str,
                      action: str,
                      strategy: str,
                      stop_pct: Optional[float],
                      take_profit_pct: Optional[float],
                      probability: Optional[float],
                      capital_required: float,
                      liquidity_score: float = 1.0,
                      confidence: float = 0.5,
                      metadata: Optional[Dict[str, Any]] = None,
                      ) -> Candidate:
    """Synthesize a Candidate from per-signal fields. Sensible fallbacks
    for missing inputs so partial information doesn't crash the marketplace."""
    ret = float(take_profit_pct) / 100.0 if take_profit_pct else 0.05
    risk = float(stop_pct) / 100.0 if stop_pct else 0.03
    prob = float(probability) if probability is not None else 0.55
    c = Candidate(
        ticker=ticker, action=action, strategy=strategy or "",
        expected_return_pct=ret,
        expected_risk_pct=risk,
        probability=prob,
        capital_required=max(1.0, float(capital_required or 1.0)),
        liquidity_score=max(0.0, min(1.0, float(liquidity_score))),
        confidence=max(0.0, min(1.0, float(confidence))),
        metadata=metadata or {},
    )
    _compute_scores(c)
    return c


# ── selection ───────────────────────────────────────────────────────────


@dataclass
class SelectionResult:
    selected: List[Candidate] = field(default_factory=list)
    rejected: List[Candidate] = field(default_factory=list)
    total_capital_used: float = 0.0
    total_expected_value: float = 0.0
    capital_available: float = 0.0
    max_positions: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected": [c.to_dict() for c in self.selected],
            "rejected": [c.to_dict() for c in self.rejected],
            "total_capital_used": round(self.total_capital_used, 2),
            "total_expected_value": round(self.total_expected_value, 2),
            "capital_available": round(self.capital_available, 2),
            "max_positions": self.max_positions,
        }


def select(candidates: List[Candidate],
              *,
              capital_available: float,
              max_positions: int = 10,
              min_expected_value: float = 0.0,
              min_score_per_dollar: float = 0.0,
              ) -> SelectionResult:
    """Greedy knapsack: rank by score_per_dollar, fit subject to capital +
    position-count caps. Skips candidates with non-positive expected value
    by default (no point taking trades with negative edge)."""
    # Re-score in case metadata changed since synthesis.
    for c in candidates:
        _compute_scores(c)

    pool = list(candidates)
    pool.sort(key=lambda c: c.score_per_dollar, reverse=True)
    selected: List[Candidate] = []
    rejected: List[Candidate] = []
    spent = 0.0
    for c in pool:
        if c.expected_value <= min_expected_value:
            c.rejection_reason = "negative or zero expected value"
            rejected.append(c)
            continue
        if c.score_per_dollar < min_score_per_dollar:
            c.rejection_reason = (
                f"score/$ {c.score_per_dollar:.4f} < floor {min_score_per_dollar:.4f}"
            )
            rejected.append(c)
            continue
        if len(selected) >= max_positions:
            c.rejection_reason = f"position cap reached ({max_positions})"
            rejected.append(c)
            continue
        if spent + c.capital_required > capital_available:
            c.rejection_reason = (
                f"insufficient capital: needs ${c.capital_required:.0f} "
                f"with ${capital_available - spent:.0f} left"
            )
            rejected.append(c)
            continue
        c.selected = True
        selected.append(c)
        spent += c.capital_required

    return SelectionResult(
        selected=selected,
        rejected=rejected,
        total_capital_used=spent,
        total_expected_value=sum(c.expected_value for c in selected),
        capital_available=capital_available,
        max_positions=max_positions,
    )
