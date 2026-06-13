"""Hand-curated lesson seeds (P2.2 — institutional wisdom).

These rules don't need to be learned — they're the kind of guardrails
desk traders teach juniors on day one. They ride ON TOP of organic
journal lessons (which are derived from the bot's closed-trade corpus),
not instead of. Conflict resolution: most-penalising wins, matching the
organic ``trade_size_multiplier`` behaviour.

Why hand-curate instead of letting the corpus discover them:
  • Many of these are "never do X within 7d of earnings" — a single bad
    earnings move can wipe out hundreds of small wins. The bot would
    learn the rule eventually but the cost of learning it from live
    trades is catastrophic.
  • Some require external state the journal corpus doesn't see well
    (yield-curve inversion, VIX regime, day-of-week).
  • Some need an answer BEFORE the corpus is large enough for organic
    learning. We pay a one-time engineering cost to encode them, then
    they protect every cycle.

Each rule is intentionally simple, auditable, and tagged with a source
citation so future maintainers can challenge the rationale.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.bot.journal import Lesson

logger = logging.getLogger(__name__)


CURATED_SOURCE = "curated"


@dataclass
class CuratedRule:
    """A hand-written guardrail that ships a synthetic Lesson when
    matched.

    The ``match`` callable receives the same kwargs as
    ``applicable_lessons`` and returns True when the rule should fire.
    """
    rule_id: str
    pattern: str
    citation: str
    match: Callable[..., bool]
    suggested_action: str
    size_multiplier: float
    severity: str = "warn"           # info | warn | alert
    condition_keys: Dict[str, Any] = field(default_factory=dict)


def _earnings_within(days: int) -> Callable[..., bool]:
    def _m(*, earnings_days: Optional[float] = None, **_) -> bool:
        if earnings_days is None:
            return False
        try:
            return float(earnings_days) <= days
        except (TypeError, ValueError):
            return False
    return _m


def _vix_above(level: float) -> Callable[..., bool]:
    def _m(*, vix: Optional[float] = None, **_) -> bool:
        if vix is None:
            return False
        try:
            return float(vix) >= level
        except (TypeError, ValueError):
            return False
    return _m


def _iv_rank_above(level: float) -> Callable[..., bool]:
    def _m(*, iv_rank: Optional[float] = None, **_) -> bool:
        if iv_rank is None:
            return False
        try:
            return float(iv_rank) >= level
        except (TypeError, ValueError):
            return False
    return _m


def _iv_rank_below(level: float) -> Callable[..., bool]:
    def _m(*, iv_rank: Optional[float] = None, **_) -> bool:
        if iv_rank is None:
            return False
        try:
            return float(iv_rank) < level
        except (TypeError, ValueError):
            return False
    return _m


def _strategy_in(*names: str) -> Callable[..., bool]:
    name_set = set(names)
    def _m(*, strategy: str, **_) -> bool:
        return strategy in name_set
    return _m


def _and(*matchers: Callable[..., bool]) -> Callable[..., bool]:
    def _m(**ctx) -> bool:
        return all(fn(**ctx) for fn in matchers)
    return _m


# ── catalog ─────────────────────────────────────────────────────────────


CURATED_RULES: List[CuratedRule] = [
    CuratedRule(
        rule_id="csp_earnings_blackout",
        pattern="SELL_CSP within 7d of earnings → forced abstain",
        citation=(
            "Earnings binary risk dwarfs the premium captured on a 30-day "
            "CSP. Pre-earnings IV crush + post-event gap is the largest "
            "single-trade loss bucket in retail short-put data."
        ),
        match=_and(_strategy_in("cash_secured_put"), _earnings_within(7)),
        suggested_action="abstain",
        size_multiplier=0.0,
        severity="alert",
        condition_keys={"strategy": "cash_secured_put", "earnings_band": "near"},
    ),
    CuratedRule(
        rule_id="covered_call_earnings_blackout",
        pattern="SELL_COVERED_CALL within 7d of earnings → forced abstain",
        citation=(
            "Earnings beat caps your upside at the strike while the "
            "premium received doesn't cover the implied move. "
            "Asymmetric loss vs. holding the shares unhedged."
        ),
        match=_and(_strategy_in("covered_call_wheel"), _earnings_within(7)),
        suggested_action="abstain",
        size_multiplier=0.0,
        severity="alert",
        condition_keys={"strategy": "covered_call_wheel", "earnings_band": "near"},
    ),
    CuratedRule(
        rule_id="long_options_vix_spike",
        pattern="BUY_CALL / BUY_PUT when VIX ≥ 30 → cut size 50%",
        citation=(
            "Long-premium strategies pay the IV crush after the spike. "
            "Half-size keeps the directional exposure while reducing "
            "vega bleed if the panic resolves into mean-reversion."
        ),
        match=_and(
            _strategy_in("zero_dte_scalp", "earnings_straddle",
                            "trend_pullback", "rsi_mean_reversion"),
            _vix_above(30.0),
        ),
        suggested_action="reduce_size_50",
        size_multiplier=0.5,
        severity="warn",
        condition_keys={"vix_band": "high"},
    ),
    CuratedRule(
        rule_id="iron_condor_low_iv",
        pattern="IRON_CONDOR when IV rank < 30 → abstain (premium too thin)",
        citation=(
            "Iron condors need elevated IV to clear commissions + slippage. "
            "Below ~30 IV rank, the credit collected is too small to "
            "compensate for the tail risk; expected value goes negative."
        ),
        match=_and(_strategy_in("iron_condor"), _iv_rank_below(30.0)),
        suggested_action="abstain",
        size_multiplier=0.0,
        severity="alert",
        condition_keys={"strategy": "iron_condor", "iv_band": "low"},
    ),
    CuratedRule(
        rule_id="long_options_iv_overpay",
        pattern="BUY_CALL / BUY_PUT when IV rank > 80 → cut size 30%",
        citation=(
            "Buying options when IV is rich (>80 percentile) means you "
            "need a bigger underlying move just to break even on the "
            "premium paid. Stage-of-cycle reduction is small but compounds."
        ),
        match=_and(
            _strategy_in("trend_pullback", "rsi_mean_reversion",
                            "macd_momentum", "news_catalyst_momentum"),
            _iv_rank_above(80.0),
        ),
        suggested_action="reduce_size_25",
        size_multiplier=0.7,
        severity="info",
        condition_keys={"iv_band": "high"},
    ),
    CuratedRule(
        rule_id="mean_reversion_trending_market",
        pattern="RSI mean-reversion when regime_trend=trending → abstain",
        citation=(
            "Mean-reversion strategies have negative expectancy in strong "
            "trending regimes — the oversold condition keeps getting more "
            "oversold. ADX > 30 confirms the trend; abstain instead of "
            "fighting it."
        ),
        match=_and(
            _strategy_in("rsi_mean_reversion", "vwap_reversion"),
            # **_ catches all other ctx kwargs so the matcher doesn't
            # blow up when given keyword args it doesn't care about.
            lambda *, regime_trend, **_: regime_trend == "trending",
        ),
        suggested_action="abstain",
        size_multiplier=0.0,
        severity="warn",
        condition_keys={"strategy": "rsi_mean_reversion", "regime": "trending"},
    ),
    CuratedRule(
        rule_id="short_premium_in_volatility_expanding",
        pattern="Premium-selling when IV is EXPANDING regime → cut size 50%",
        citation=(
            "Selling premium into rising volatility-of-volatility is the "
            "classic short-vol blow-up setup. Half-size protects against "
            "the gamma squeeze."
        ),
        match=_and(
            _strategy_in("cash_secured_put", "covered_call_wheel",
                            "iron_condor"),
            lambda *, iv_regime=None, **_: (
                isinstance(iv_regime, dict)
                and iv_regime.get("regime") == "expanding"
            ),
        ),
        suggested_action="reduce_size_50",
        size_multiplier=0.5,
        severity="warn",
        condition_keys={"iv_regime": "expanding"},
    ),
    CuratedRule(
        rule_id="friday_pm_credit_weekend_risk",
        pattern="Credit spreads opened Friday afternoon → cut size 30% (weekend gamma)",
        citation=(
            "A short-premium position over the weekend collects no "
            "theta on Saturday/Sunday but absorbs all Monday-open gap risk. "
            "TastyTrade studies show -15% expectancy delta on Friday-PM "
            "vs. Monday-AM opens."
        ),
        match=_and(
            _strategy_in("cash_secured_put", "iron_condor",
                            "covered_call_wheel"),
            lambda *, day_of_week=None, **_: day_of_week == "Friday",
        ),
        suggested_action="reduce_size_25",
        size_multiplier=0.7,
        severity="info",
        condition_keys={"day": "Friday"},
    ),
    CuratedRule(
        rule_id="inverted_yield_curve_long_caution",
        pattern="2s10s inverted → cut equity long exposure 30%",
        citation=(
            "Yield-curve inversion historically precedes equity drawdowns "
            "by 6-18 months. Not a stop-trading signal, but a size-down "
            "to acknowledge elevated tail risk."
        ),
        match=_and(
            _strategy_in("trend_pullback", "macd_momentum",
                            "rsi_mean_reversion", "news_catalyst_momentum"),
            lambda *, yield_curve_inverted=None, **_: yield_curve_inverted is True,
        ),
        suggested_action="reduce_size_25",
        size_multiplier=0.7,
        severity="info",
        condition_keys={"macro": "yield_curve_inverted"},
    ),
]


def _rule_to_lesson(rule: CuratedRule) -> Lesson:
    """Synthesize a Lesson object from a curated rule. ``sample_size``
    is set to a sentinel large number so the ``size_multiplier`` doesn't
    get shrunk by Wilson bounds the way organic small-sample lessons do.

    These fields are filled with neutral defaults where the curated rule
    doesn't have a natural value (e.g. ``win_rate`` because the rule
    isn't derived from outcomes)."""
    return Lesson(
        pattern=f"[{CURATED_SOURCE}] {rule.pattern}",
        condition_keys={**rule.condition_keys, "source": CURATED_SOURCE,
                              "rule_id": rule.rule_id,
                              "citation": rule.citation},
        sample_size=999,                 # large so it survives Wilson shrink
        wins=0, losses=0,
        win_rate=0.0,
        baseline_win_rate=0.0,
        expectancy=0.0, expectancy_r=None,
        avg_win=0.0, avg_loss=0.0,
        profit_factor=None,
        delta_pp=0.0,
        confidence_bound_lo=0.0,
        confidence_bound_hi=0.0,
        suggested_action=rule.suggested_action,
        size_multiplier=rule.size_multiplier,
        severity=rule.severity,
    )


def applicable_curated_lessons(*, strategy: str, regime_trend: str,
                                  volatility: str, gamma: str,
                                  iv_regime: Optional[Dict[str, Any]] = None,
                                  yield_curve_inverted: Optional[bool] = None,
                                  earnings_days: Optional[float] = None,
                                  iv_rank: Optional[float] = None,
                                  vix: Optional[float] = None,
                                  day_of_week: Optional[str] = None,
                                  **_: Any) -> List[Lesson]:
    """Return Lesson objects for every curated rule that matches the
    given context. Mirrors the signature of ``applicable_lessons`` so
    the merged caller can just pass through kwargs."""
    out: List[Lesson] = []
    ctx = dict(
        strategy=strategy,
        regime_trend=regime_trend,
        volatility=volatility,
        gamma=gamma,
        iv_regime=iv_regime,
        yield_curve_inverted=yield_curve_inverted,
        earnings_days=earnings_days,
        iv_rank=iv_rank,
        vix=vix,
        day_of_week=day_of_week,
    )
    for rule in CURATED_RULES:
        try:
            if rule.match(**ctx):
                out.append(_rule_to_lesson(rule))
        except TypeError:
            # Rule matcher rejected an unexpected kwarg — log and skip.
            logger.debug("curated rule %s match raised TypeError",
                              rule.rule_id, exc_info=True)
        except Exception:
            logger.debug("curated rule %s match raised",
                              rule.rule_id, exc_info=True)
    return out
