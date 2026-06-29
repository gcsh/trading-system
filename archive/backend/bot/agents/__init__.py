"""Stage-11.3 / 20a / 20b / 20c — Master Agent Council.

Parallel to the two Claude-backed reasoners (``bot/ai/brain.py`` and
``bot/meta_ai/``), this module is a panel of cheap, deterministic
specialists. Each agent reads the per-decision context and produces an
``AgentVote``; the ``ConsensusEngine`` aggregates votes into a
``Consensus`` and the ``Chairman`` reconciles them into a
``ChairmanReport`` with a decision the engine can act on.

================ MASTER AGENT CONTRACT (locked at Stage 20c) ================

These ten rules are the contract every council component must obey.
They exist because correlated agents counted as independent
confirmations, silent agents counted as confident abstainers, and the
synthesis layer invented reasoning the council never produced. Each
rule closes one of those leaks.

  1. **One vote, one shape.** Every production agent emits an
     ``AgentVote`` with: ``stance, confidence, weight, reasoning,
     reasoning_type, expected_edge, risk_level, invalidators,
     key_drivers``. Tests and pre-20a code may use the legacy
     positional signature; those default to
     ``reasoning_type = "legacy"`` and bypass invariant checks.

  2. **Three reasoning states, not two.** ``reasoning_type ∈
     {contributing, dissenting, insufficient_signal}``. Silence is a
     first-class state — distinct from a confident abstain.

  3. **Evidence accompanies conviction.** ``confidence ≥
     TUNABLES.min_confidence_for_contribution AND key_drivers == []
     ⇒ ContractViolation``. No empty-but-confident votes.

  4. **Silence has no drivers.** ``insufficient_signal ⇒
     key_drivers == [] AND stance == abstain``. Silent agents look
     identical from the outside but are distinguishable via
     ``reasoning_type`` and the ``silent_agents`` list on the Consensus.

  5. **Active votes carry evidence.** ``contributing`` or ``dissenting``
     ⇒ ``len(key_drivers) ≥ 1``.

  6. **Drivers are categorized.** Every ``KeyDriver`` cites one of 10
     ``SOURCE_CATEGORIES`` (the 9 market categories +
     ``portfolio_state``). Categories are how the Chairman counts
     independent signals — five agents citing the same category are
     NOT five confirmations.

  7. **One market view, shared.** The ``MarketInternalsScore`` is
     computed ONCE per consensus run inside ``run_consensus`` and
     threaded into the context under ``market_internals_obj``.
     Macro/market/risk agents read from it rather than independently
     re-interpreting the FRED panel.

  8. **Quorum gate first.** ``≥ TUNABLES.agent_quorum_min`` non-silent
     agents required before any recommendation other than ``abstain``.
     Below quorum, the consensus is forced to abstain with reason
     ``insufficient_council_quorum`` — even if every speaker is loud.

  9. **Chairman is lossless.** The Chairman MAY: reweight votes,
     reconcile via Jaccard overlap on category sets, surface dissent
     (primary_dissenter + dissent_weight + dissent_share), summarize
     by concatenating agent inputs. The Chairman MAY NOT: invent new
     signals, write narrative beyond agent strings, change stances or
     confidences, re-classify ``reasoning_type``. Every Chairman
     output sentence is a quote of an agent input.

 10. **Authority is opt-in.** The Chairman emits a ``decision`` ∈
     ``{EXECUTE, SIZE_DOWN, MONITOR, ABSTAIN}`` attached to every
     Consensus. By default (``ai.chairman_authoritative=False``) the
     engine consumes the legacy ``recommendation`` so the Chairman
     runs in shadow. Flip the flag once empirical trust is established
     and the engine will respect ``chairman_report["decision"]``
     instead.

================ DATA FLOW ================

  context ──► run_consensus ──► compute_market_internals (rule 7)
                              ──► every agent in AGENT_FUNCS (rules 1-6)
                              ──► aggregate (rule 8: quorum)
                              ──► chairman_review (rule 9)
                              ──► Consensus { recommendation, chairman_report }
                              ──► engine (rule 10: which one is consumed)

Persistence: the Consensus dict is attached to ``Trade.detail_json``
under the ``"consensus"`` key by ``engine._persist_trade``, so the
``/lineage/trade/{id}`` endpoint surfaces both the legacy
``recommendation`` and ``chairman_report`` for every trade.

================ EXTENDING ================

  • New agent: append to ``AGENT_FUNCS``. Must emit a structured
    ``AgentVote`` with reasoning_type + categorized key_drivers.
  • New source category: add to ``SOURCE_CATEGORIES`` in
    ``contract.py``. If it's a market-level signal, add a scorer to
    ``market_internals.py`` and include it in
    ``MARKET_INTERNAL_CATEGORIES``.
  • Adjusting decision thresholds: edit ``chairman_review`` in
    ``chairman.py``. Adjust ``TUNABLES.min_confidence_for_contribution``
    and ``TUNABLES.agent_quorum_min`` via env vars
    (``TB_AGENTS_MIN_CONFIDENCE_FOR_CONTRIBUTION``,
    ``TB_AGENT_QUORUM_MIN``).
  • A Claude chairman (Stage 21) must obey rule 9: it sees the same
    structured votes the heuristic does and is prompted with the
    lossless constraint. Activation requires the eval gate documented
    in the Stage 20a/20b project plan.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.bot.agents.contract import (
    DIRECTION_ABSTAIN,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
    REASONING_LEGACY,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    SOURCE_CATEGORIES,
    STRUCTURED_REASONING_TYPES,
    enforce_vote_contract,
)
from backend.bot.agents.chairman import (
    ChairmanReport,
    chairman_review,
)
from backend.bot.agents.market_internals import (
    MarketInternalsScore,
    compute_market_internals,
)
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── data types ───────────────────────────────────────────────────────────


# Stances each agent can take.
STANCE_BUY = "buy"
STANCE_SELL = "sell"
STANCE_ABSTAIN = "abstain"
STANCE_HOLD = "hold"

STANCES = (STANCE_BUY, STANCE_SELL, STANCE_ABSTAIN, STANCE_HOLD)


@dataclass
class AgentVote:
    agent: str
    role: str                  # e.g. "Market Regime", "Options Flow"
    stance: str                # one of STANCES
    confidence: float          # 0-1
    weight: float = 1.0        # consensus weight
    reasoning: str = ""        # one-line plain-english

    # Stage-20a — structured contract fields. Default to legacy so old
    # callers (tests, pre-20a fixtures) keep working unchanged. The
    # production agents in this module set these explicitly; the
    # ``__post_init__`` invariants only fire for structured types.
    reasoning_type: str = REASONING_LEGACY
    expected_edge: float = 0.0           # net expected edge in bps; 0 for abstains
    risk_level: str = RISK_UNKNOWN
    invalidators: List[str] = field(default_factory=list)
    key_drivers: List[KeyDriver] = field(default_factory=list)
    # MITS Phase 16.B — standardized projection of this vote populated
    # by ``run_consensus`` after the council has voted. None on legacy /
    # direct test paths; populated dict on the live engine path so the
    # decision_provenance ledger has a stable shape to replay from.
    agent_output: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        # Invariant enforcement is opt-in. Legacy votes (reasoning_type
        # left at the default) skip checks; structured votes must meet
        # the four 20a contract rules.
        enforce_vote_contract(
            reasoning_type=self.reasoning_type,
            stance=self.stance,
            confidence=float(self.confidence),
            key_drivers=self.key_drivers,
            abstain_stance=STANCE_ABSTAIN,
            min_confidence_for_contribution=TUNABLES.min_confidence_for_contribution,
        )

    def is_structured(self) -> bool:
        return self.reasoning_type in STRUCTURED_REASONING_TYPES

    def is_silent(self) -> bool:
        """Stage-20a third state: contributing / dissenting / silent."""
        return self.reasoning_type == REASONING_INSUFFICIENT_SIGNAL

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Surface key_drivers as plain dicts for JSON serialization.
        d["key_drivers"] = [kd.to_dict() if isinstance(kd, KeyDriver) else kd
                                for kd in self.key_drivers]
        return d


@dataclass
class Consensus:
    stance: str                          # buy | sell | abstain | hold
    confidence: float                    # weighted mean among supporters
    disagreement_score: float            # 0 = unanimous, 1 = chaos
    recommendation: str                  # execute | size_down | abstain
    size_multiplier: float               # final size scaling [0.0-1.0]
    abstain_count: int
    supporters: List[str]                # agent names backing the stance
    dissenters: List[str]                # agent names voting opposite
    # Stage-12.C7 — three-way calibrated probability head. Always sums to 1.0.
    # Lets callers compare P(long) vs P(short) vs P(abstain) directly without
    # post-hoc filtering.
    probs: Dict[str, float] = field(default_factory=dict)
    votes: List[Dict[str, Any]] = field(default_factory=list)
    # Stage-20a — silent agents (reasoning_type == insufficient_signal).
    # Surfaced so the UI can show "macro abstained: insufficient_signal"
    # rather than counting it as a dissent. Empty when no agent went silent.
    silent_agents: List[str] = field(default_factory=list)
    # Stage-20a — quorum diagnostic. True when at least
    # ``agent_quorum_min`` agents were non-silent (contributing OR
    # dissenting). Below quorum, the recommendation is forced to
    # ``abstain`` with ``recommendation_reason == "insufficient_council_quorum"``.
    quorum_met: bool = True
    quorum_required: int = 0
    quorum_count: int = 0
    recommendation_reason: str = ""      # short tag for non-execute paths
    # Stage-20a — the shared MarketInternalsScore computed once for this
    # consensus run. Attached so downstream consumers (memo, lineage,
    # UI) can show the panel's shared view. Empty dict when no inputs
    # were available.
    market_internals: Dict[str, Any] = field(default_factory=dict)
    # Stage-20b — Chairman's lossless reconciliation. Empty dict when
    # the panel had no structured votes (legacy-only). The
    # ``decision`` inside is finer-grained than ``recommendation``:
    # ``EXECUTE | SIZE_DOWN | MONITOR | ABSTAIN``. The Chairman never
    # overrides ``recommendation`` in 20b — both surfaces coexist so
    # operators can A/B compare. Stage 20c onward consumers SHOULD
    # read ``chairman_report["decision"]`` over ``recommendation``.
    chairman_report: Dict[str, Any] = field(default_factory=dict)
    # MITS Phase 14.C — Forward-payoff simulator verdict. Populated by
    # ``run_consensus`` after the simulator council agent runs;
    # carries ``reject_reason`` when ``p_max_loss`` crosses the veto
    # threshold so the engine can short-circuit the cycle.
    simulator_verdict: Dict[str, Any] = field(default_factory=dict)
    # MITS Phase 15.D — multi-axis confidence breakdown. Computed post
    # ``aggregate`` by ``_compute_confidence_breakdown`` so the
    # ``aggregate`` signature stays stable. Empty dict for legacy
    # consensus rows persisted before 15.D.
    confidence_breakdown: Dict[str, Any] = field(default_factory=dict)
    # MITS Phase 16.B — typed envelope + per-vote projection of this
    # council run. Populated by ``run_consensus``. Empty for the
    # ``aggregate()``-only path (tests / lineage replays).
    agent_input: Dict[str, Any] = field(default_factory=dict)
    agent_outputs: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConfidenceBreakdown:
    """MITS Phase 15.D — per-axis decomposition of council confidence.

    Six analytical axes plus a composite score; ``axis_health`` flags
    each axis green/yellow/red by vote count so the UI can show which
    pillars actually contributed evidence vs. which are operating blind.
    """
    market_structure: float
    technical: float
    options: float
    historical_analog: float
    simulator: float
    macro: float
    composite: float
    axis_health: Dict[str, str] = field(default_factory=dict)
    axis_n: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── agent helpers ────────────────────────────────────────────────────────


def _direction_from_action(action: Optional[str]) -> str:
    """Convert a trading action into a side: long/short/neutral."""
    if not action:
        return "neutral"
    a = action.upper()
    if "PUT" in a or a.startswith("SELL"):
        return "short"
    if a.startswith("BUY"):
        return "long"
    return "neutral"


def _stance_for_direction(direction: str, support: bool) -> str:
    """Map (direction, supportive?) → buy/sell/hold."""
    if direction == "long":
        return STANCE_BUY if support else STANCE_SELL
    if direction == "short":
        return STANCE_SELL if support else STANCE_BUY
    return STANCE_HOLD


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _driver_direction(stance: str, vote_direction: str) -> str:
    """Map a stance + intended trade direction to a KeyDriver direction tag.

    A buy-stance on a long trade ⇒ supports_long. A sell-stance on a
    long trade ⇒ supports_short (i.e., evidence against the trade).
    Abstain ⇒ supports_abstain regardless of direction.
    """
    if stance == STANCE_ABSTAIN:
        return DIRECTION_ABSTAIN
    if stance == STANCE_HOLD:
        return DIRECTION_ABSTAIN
    if stance == STANCE_BUY:
        return DIRECTION_LONG
    if stance == STANCE_SELL:
        return DIRECTION_SHORT
    return DIRECTION_ABSTAIN


def _reasoning_type_for(stance: str, direction: str) -> str:
    """Map (stance, direction) → contributing | dissenting.

    A vote aligned with the proposed trade is "contributing"; a vote
    against is "dissenting". Abstain votes route through
    insufficient_signal separately (handled at the call site).
    """
    if stance == STANCE_HOLD:
        return REASONING_CONTRIBUTING
    if direction == "long":
        return REASONING_CONTRIBUTING if stance == STANCE_BUY else REASONING_DISSENTING
    if direction == "short":
        return REASONING_CONTRIBUTING if stance == STANCE_SELL else REASONING_DISSENTING
    return REASONING_CONTRIBUTING


def _expected_edge_bps(confidence: float, weight: float,
                          *, sign: int = 1) -> float:
    """Crude pre-Chairman edge estimate. The Chairman in 20b will replace
    this; for 20a it gives each vote a non-zero expected-edge so the
    Trade Memo + lineage screens have something to show. ``sign``
    optionally flips polarity for dissenting votes.
    """
    return round(sign * float(confidence) * float(weight) * 100.0, 1)


# ── seven specialist agents ──────────────────────────────────────────────


def agent_market(context: Dict[str, Any]) -> AgentVote:
    """Market regime + trend alignment.

    Buys longs in bullish trending tape, shorts in bearish; abstains in
    choppy / mean-revert regimes where momentum is unreliable. Reads
    the shared ``MarketInternalsScore.price_structure`` for the panel
    view of trend.
    """
    direction = _direction_from_action(context.get("action"))
    analytics = context.get("analytics") or {}
    regime = analytics.get("regime") or {}
    features = (analytics.get("features") or context.get("features") or {})
    trend = (regime.get("trend") or "unknown").lower()
    momentum = (regime.get("momentum") or "neutral").lower()
    trend_bias = float(features.get("trend_bias") or 0.0)
    has_any_signal = bool(regime) or trend_bias != 0.0

    # Silent: no regime data at all.
    if not has_any_signal:
        return AgentVote(
            agent="market", role="Market Regime",
            stance=STANCE_ABSTAIN, confidence=0.20, weight=1.0,
            reasoning="no regime data available",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    # Choppy: not enough directional conviction to back either side.
    if trend in ("choppy", "ranging", "unknown") and abs(trend_bias) < 0.3:
        drivers = [
            KeyDriver(
                description=f"regime {trend}, bias {trend_bias:+.2f}",
                source_category="price_structure",
                direction=DIRECTION_ABSTAIN,
                weight=0.6,
            ),
        ]
        return AgentVote(
            agent="market", role="Market Regime",
            stance=STANCE_ABSTAIN, confidence=0.40, weight=1.0,
            reasoning=f"regime '{trend}' is unreliable for directional bets",
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_MEDIUM,
            key_drivers=drivers,
            invalidators=["trend bias breaks > 0.5 in either direction"],
            expected_edge=_expected_edge_bps(0.40, 1.0, sign=0),
        )

    aligned = (
        (direction == "long" and (trend == "bullish" or trend_bias > 0.2))
        or (direction == "short" and (trend == "bearish" or trend_bias < -0.2))
    )
    conf = _clamp(0.55 + abs(trend_bias) * 0.4 + (0.15 if momentum == "expanding" else 0.0))
    stance = _stance_for_direction(direction, aligned)

    drivers = [
        KeyDriver(
            description=f"{trend} trend, bias {trend_bias:+.2f}",
            source_category="price_structure",
            direction=_driver_direction(stance, direction),
            weight=_clamp(0.4 + abs(trend_bias) * 0.6),
        ),
    ]
    if momentum == "expanding":
        drivers.append(KeyDriver(
            description="momentum expanding",
            source_category="price_structure",
            direction=_driver_direction(stance, direction),
            weight=0.4,
        ))
    invalidators = (
        ["trend flips to bearish", "trend bias collapses to ±0.1"]
        if direction == "long" else
        ["trend flips to bullish", "trend bias collapses to ±0.1"]
        if direction == "short" else []
    )
    return AgentVote(
        agent="market", role="Market Regime", stance=stance, confidence=conf,
        weight=1.2,
        reasoning=(f"{trend} trend, bias={trend_bias:+.2f}, momentum {momentum}"),
        reasoning_type=_reasoning_type_for(stance, direction),
        expected_edge=_expected_edge_bps(conf, 1.2,
                                              sign=1 if aligned else -1),
        risk_level=RISK_LOW if aligned else RISK_MEDIUM,
        invalidators=invalidators,
        key_drivers=drivers,
    )


def agent_flow(context: Dict[str, Any]) -> AgentVote:
    """Options flow + dealer positioning.

    Supports the trade when flow_bullishness aligns; flags abstain if
    flow data is missing entirely (we shouldn't pretend to know).
    """
    direction = _direction_from_action(context.get("action"))
    features = ((context.get("analytics") or {}).get("features")
                  or context.get("features") or {})
    flow_b = features.get("flow_bullishness")
    sweeps = features.get("premarket_bullish_sweeps")
    dealer = (features.get("dealer_regime") or "unknown").lower()
    hedge = (features.get("hedging_pressure") or "normal").lower()

    if flow_b is None and sweeps is None:
        return AgentVote(
            agent="flow", role="Options Flow",
            stance=STANCE_ABSTAIN, confidence=0.30, weight=0.8,
            reasoning="no flow data; not voting",
        )
    flow_b = float(flow_b or 0.0)
    sweeps = float(sweeps or 0.0)
    score = flow_b + 0.5 * sweeps
    if direction == "long":
        aligned = score > 0.15
    elif direction == "short":
        aligned = score < -0.15
    else:
        aligned = False
    conf = _clamp(0.50 + abs(score) * 0.5)
    note = (f"flow score {score:+.2f}, dealer {dealer}, hedging {hedge}")
    return AgentVote(
        agent="flow", role="Options Flow",
        stance=_stance_for_direction(direction, aligned),
        confidence=conf, weight=1.0, reasoning=note,
    )


def agent_options(context: Dict[str, Any]) -> AgentVote:
    """IV / pinning / OPEX timing.

    Hostile to long-premium trades when IV is elevated + pinning prob high;
    supportive when IV is reasonable and there's no pin risk overhead.
    """
    direction = _direction_from_action(context.get("action"))
    features = ((context.get("analytics") or {}).get("features")
                  or context.get("features") or {})
    iv_rank = float(features.get("iv_rank") or 0.0)
    pin_prob = float(features.get("pinning_probability") or 0.0)
    earnings_days = features.get("earnings_days")
    action = (context.get("action") or "").upper()
    is_options = "CALL" in action or "PUT" in action

    notes = []
    if is_options:
        if iv_rank > 75:
            notes.append(f"IV rank {iv_rank:.0f}% elevated → premium-buyer hostile")
        if pin_prob > 0.6:
            notes.append(f"pin prob {pin_prob:.0%} near wall")
        if earnings_days is not None and earnings_days <= 7:
            notes.append(f"earnings in {earnings_days:.0f}d — IV crush risk")
        if iv_rank > 75 or pin_prob > 0.6:
            return AgentVote(
                agent="options", role="Options Microstructure",
                stance=STANCE_SELL if direction == "long" else STANCE_ABSTAIN,
                confidence=0.65, weight=1.0,
                reasoning="; ".join(notes) or "options conditions adverse",
            )

    # Non-options or favorable options conditions.
    aligned = iv_rank < 60 and pin_prob < 0.4
    note = "; ".join(notes) or (f"IV {iv_rank:.0f}%, pin {pin_prob:.0%} — clean")
    return AgentVote(
        agent="options", role="Options Microstructure",
        stance=_stance_for_direction(direction, aligned),
        confidence=_clamp(0.50 + (0.20 if aligned else 0.0)),
        weight=1.0 if is_options else 0.6, reasoning=note,
    )


def agent_macro(context: Dict[str, Any]) -> AgentVote:
    """Cross-asset + macro tape.

    VIX + SPY trend + news sentiment + the FRED macro panel (yield curve,
    HY spread, NFCI). Hostile to longs when risk-off OR macro is screaming
    recession; hostile to shorts when conditions are loose and credit is
    tight (risk-on confirmed). Stage-20a: leans on
    ``MarketInternalsScore.{macro_liquidity, credit, volatility}`` for the
    panel-wide view rather than recomputing.
    """
    direction = _direction_from_action(context.get("action"))
    features = ((context.get("analytics") or {}).get("features")
                  or context.get("features") or {})
    cross_asset = context.get("cross_asset") or {}
    snap = context.get("snapshot") or {}
    macro = context.get("macro") or {}
    vix = float(features.get("vix") or snap.get("vix") or 0.0)
    news = float(features.get("news_sentiment") or 0.0)
    spy_trend = (snap.get("spy_trend") or "neutral").lower()
    equities = (cross_asset.get("equities") or "").lower()
    internals: Optional[MarketInternalsScore] = context.get("market_internals_obj")

    # Stage-18b — FRED macro panel signals. Each lookup is tolerant of
    # missing data (cold start before the FRED cron has run, or no API
    # key configured) — falls through to None.
    def _macro_value(key):
        d = macro.get(key) or {}
        return d.get("value") if isinstance(d, dict) else None

    def _macro_change(key):
        d = macro.get(key) or {}
        return d.get("change_30d_pct") if isinstance(d, dict) else None

    curve_inverted = bool(macro.get("yield_curve_inverted"))
    hy_spread = _macro_value("BAMLH0A0HYM2")        # high-yield OAS, %
    hy_spread_change = _macro_change("BAMLH0A0HYM2")
    nfci = _macro_value("NFCI")                       # Chicago Fed conditions
    spread_10y_2y = macro.get("spread_10y_2y")

    macro_concerns = []
    macro_boosts = []
    if curve_inverted:
        macro_concerns.append(
            f"yield curve inverted ({spread_10y_2y:+.2f})"
            if spread_10y_2y is not None else "yield curve inverted"
        )
    if hy_spread is not None and hy_spread > 5.5:
        macro_concerns.append(f"HY spread {hy_spread:.1f}% (credit stress)")
    if hy_spread_change is not None and hy_spread_change > 0.15:
        macro_concerns.append(f"HY spread widening +{hy_spread_change:+.0%} 30d")
    if nfci is not None and nfci > 0.30:
        macro_concerns.append(f"NFCI {nfci:+.2f} (tight conditions)")
    if hy_spread is not None and hy_spread < 3.0:
        macro_boosts.append(f"HY spread {hy_spread:.1f}% (credit tight, risk-on)")
    if nfci is not None and nfci < -0.30:
        macro_boosts.append(f"NFCI {nfci:+.2f} (loose conditions, risk-on)")

    risk_on = (vix < 16 and (spy_trend == "bullish" or equities == "risk_on"))
    risk_off = (vix > 22 or spy_trend == "bearish" or equities == "risk_off")

    # Silent: no macro inputs of any kind.
    has_any_signal = bool(macro) or vix > 0 or spy_trend != "neutral" or news != 0.0
    if not has_any_signal:
        return AgentVote(
            agent="macro", role="Macro/Cross-Asset",
            stance=STANCE_ABSTAIN, confidence=0.20, weight=1.0,
            reasoning="no macro inputs available",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    def _macro_drivers(concerns: List[str], boosts: List[str],
                            stance: str) -> List[KeyDriver]:
        out: List[KeyDriver] = []
        d_dir = _driver_direction(stance, direction)
        # VIX → volatility category
        if vix > 0:
            out.append(KeyDriver(
                description=f"VIX {vix:.1f}",
                source_category="volatility",
                direction=d_dir,
                weight=_clamp(abs(vix - 20) / 15.0),
                time_sensitive=True,
            ))
        # HY spread → credit category
        if hy_spread is not None:
            out.append(KeyDriver(
                description=f"HY OAS {hy_spread:.1f}%" +
                    (f" ({hy_spread_change:+.0%} 30d)"
                     if hy_spread_change is not None else ""),
                source_category="credit",
                direction=d_dir,
                weight=_clamp(0.4 + min(abs(hy_spread - 4.0) / 4.0, 0.6)),
            ))
        # NFCI → macro_liquidity category
        if nfci is not None:
            out.append(KeyDriver(
                description=f"NFCI {nfci:+.2f}",
                source_category="macro_liquidity",
                direction=d_dir,
                weight=_clamp(0.3 + min(abs(nfci) / 1.5, 0.6)),
            ))
        # Yield curve → macro_liquidity
        if curve_inverted:
            out.append(KeyDriver(
                description="yield curve inverted",
                source_category="macro_liquidity",
                direction=d_dir,
                weight=0.6,
            ))
        # Fall back to volatility category if nothing else fired
        if not out:
            out.append(KeyDriver(
                description=f"risk {'off' if risk_off else 'on' if risk_on else 'mixed'}",
                source_category="volatility",
                direction=d_dir,
                weight=0.4,
            ))
        return out

    # Strong-veto path: longs into a bad-macro tape, regardless of VIX.
    if direction == "long" and len(macro_concerns) >= 2:
        stance = STANCE_SELL
        return AgentVote(
            agent="macro", role="Macro/Cross-Asset",
            stance=stance, confidence=0.75, weight=1.0,
            reasoning="; ".join(macro_concerns[:3]),
            reasoning_type=_reasoning_type_for(stance, direction),
            risk_level=RISK_HIGH,
            expected_edge=_expected_edge_bps(0.75, 1.0, sign=-1),
            invalidators=["HY spread tightens back < 4.5%",
                            "yield curve un-inverts"],
            key_drivers=_macro_drivers(macro_concerns, macro_boosts, stance),
        )

    if direction == "long":
        if risk_off:
            note = f"risk-off (VIX {vix:.1f}, SPY {spy_trend})"
            if macro_concerns:
                note += " · " + macro_concerns[0]
            stance = STANCE_SELL
            return AgentVote(
                agent="macro", role="Macro/Cross-Asset",
                stance=stance, confidence=0.70, weight=1.0,
                reasoning=note,
                reasoning_type=_reasoning_type_for(stance, direction),
                risk_level=RISK_HIGH,
                expected_edge=_expected_edge_bps(0.70, 1.0, sign=-1),
                invalidators=["VIX < 18 and SPY trend turns bullish"],
                key_drivers=_macro_drivers(macro_concerns, macro_boosts, stance),
            )
        aligned = (risk_on or news > 0.2 or bool(macro_boosts)) \
            and not macro_concerns
    elif direction == "short":
        if risk_on and not macro_concerns:
            stance = STANCE_BUY
            return AgentVote(
                agent="macro", role="Macro/Cross-Asset",
                stance=stance, confidence=0.65, weight=1.0,
                reasoning=f"risk-on tape fights shorts (VIX {vix:.1f})",
                reasoning_type=_reasoning_type_for(stance, direction),
                risk_level=RISK_MEDIUM,
                expected_edge=_expected_edge_bps(0.65, 1.0, sign=-1),
                invalidators=["VIX > 22 or credit stress emerges"],
                key_drivers=_macro_drivers(macro_concerns, macro_boosts, stance),
            )
        aligned = risk_off or news < -0.2 or bool(macro_concerns)
    else:
        aligned = False

    # Reasoning combines headline read with macro-panel one-liner.
    rationale_parts = [f"VIX {vix:.1f}", f"SPY {spy_trend}",
                          f"news {news:+.2f}"]
    if spread_10y_2y is not None:
        rationale_parts.append(f"10y-2y {spread_10y_2y:+.2f}")
    if hy_spread is not None:
        rationale_parts.append(f"HY {hy_spread:.1f}%")
    if macro_concerns:
        rationale_parts.append("⚠ " + macro_concerns[0])
    elif macro_boosts:
        rationale_parts.append("✓ " + macro_boosts[0])
    reasoning = ", ".join(rationale_parts)

    base_conf = 0.55 + abs(news) * 0.3
    if aligned and macro_boosts:
        base_conf += 0.10
    if aligned and macro_concerns:
        base_conf -= 0.10
    stance = _stance_for_direction(direction, aligned)
    conf = _clamp(base_conf)
    risk = RISK_LOW if aligned and not macro_concerns else (
        RISK_HIGH if macro_concerns else RISK_MEDIUM
    )
    invalidators = (
        ["macro concerns emerge (HY widens, NFCI > 0.3)"]
        if aligned else ["risk-off tape clears"]
    )
    return AgentVote(
        agent="macro", role="Macro/Cross-Asset",
        stance=stance,
        confidence=conf,
        weight=1.0,
        reasoning=reasoning,
        reasoning_type=_reasoning_type_for(stance, direction),
        risk_level=risk,
        expected_edge=_expected_edge_bps(conf, 1.0,
                                              sign=1 if aligned else -1),
        invalidators=invalidators,
        key_drivers=_macro_drivers(macro_concerns, macro_boosts, stance),
    )


def agent_risk(context: Dict[str, Any]) -> AgentVote:
    """Portfolio risk — concentration + drawdown + correlation.

    Vetoes trades that would add to a hot sector / theme or that take fire
    in a drawdown above the policy band.
    """
    portfolio_risk = context.get("portfolio_risk") or {}
    optimizer = context.get("optimizer") or {}
    direction = _direction_from_action(context.get("action"))
    flags = portfolio_risk.get("concentration_flags") or []
    drawdown = float(portfolio_risk.get("drawdown_pct")
                       or optimizer.get("drawdown_pct") or 0.0)
    net_beta = float(portfolio_risk.get("net_beta") or 0.0)

    if drawdown > 0.08:
        return AgentVote(
            agent="risk", role="Portfolio Risk",
            stance=STANCE_ABSTAIN, confidence=0.75, weight=1.2,
            reasoning=f"portfolio in {drawdown:.1%} drawdown — pause new risk",
        )
    if flags:
        return AgentVote(
            agent="risk", role="Portfolio Risk",
            stance=STANCE_SELL if direction == "long" else STANCE_BUY,
            confidence=0.65, weight=1.1,
            reasoning=f"concentration flags: {', '.join(str(f) for f in flags)[:120]}",
        )
    if abs(net_beta) > 1.8:
        return AgentVote(
            agent="risk", role="Portfolio Risk",
            stance=STANCE_ABSTAIN, confidence=0.60, weight=1.0,
            reasoning=f"net beta {net_beta:+.2f} already extreme",
        )
    return AgentVote(
        agent="risk", role="Portfolio Risk",
        stance=_stance_for_direction(direction, True),
        confidence=0.60, weight=1.0,
        reasoning=f"drawdown {drawdown:.1%}, net beta {net_beta:+.2f} — within bands",
    )


def agent_portfolio(context: Dict[str, Any]) -> AgentVote:
    """Portfolio composition — theme heat + correlation balance.

    Backs the trade when theme heat is cold (room to add), pushes back when
    cohort is overheated.
    """
    direction = _direction_from_action(context.get("action"))
    portfolio_risk = context.get("portfolio_risk") or {}
    top_theme = portfolio_risk.get("top_theme")
    theme_pct = float(portfolio_risk.get("top_theme_pct") or 0.0)
    cohort = context.get("cohort") or {}
    cohort_wr = cohort.get("win_rate")

    if theme_pct > 0.40:
        return AgentVote(
            agent="portfolio", role="Portfolio Composition",
            stance=STANCE_ABSTAIN, confidence=0.65, weight=1.0,
            reasoning=f"theme {top_theme} already {theme_pct:.0%} of book",
        )
    if cohort_wr is not None and cohort_wr < 0.40:
        return AgentVote(
            agent="portfolio", role="Portfolio Composition",
            stance=STANCE_SELL if direction == "long" else STANCE_BUY,
            confidence=0.65, weight=0.9,
            reasoning=f"cohort win-rate {cohort_wr:.0%} weak — fade",
        )
    note = (f"theme {top_theme or 'n/a'} {theme_pct:.0%}; "
              f"cohort wr {cohort_wr or 0:.0%}")
    return AgentVote(
        agent="portfolio", role="Portfolio Composition",
        stance=_stance_for_direction(direction, True),
        confidence=_clamp(0.55 + (0.15 if (cohort_wr or 0) > 0.55 else 0.0)),
        weight=0.9, reasoning=note,
    )


def agent_execution(context: Dict[str, Any]) -> AgentVote:
    """Execution quality — spread + liquidity + adverse selection risk."""
    snap = context.get("snapshot") or {}
    features = ((context.get("analytics") or {}).get("features")
                  or context.get("features") or {})
    direction = _direction_from_action(context.get("action"))
    volume = float(snap.get("volume") or 0.0)
    avg_volume = float(snap.get("avg_volume") or 0.0) or 1.0
    volume_ratio = float(features.get("volume_ratio") or (volume / avg_volume))
    spread_bps = features.get("spread_bps")  # may be None

    if volume_ratio < 0.4:
        return AgentVote(
            agent="execution", role="Execution Quality",
            stance=STANCE_ABSTAIN, confidence=0.60, weight=0.8,
            reasoning=f"volume {volume_ratio:.1f}× avg — thin tape, slippage risk",
        )
    if spread_bps is not None and float(spread_bps) > 40:
        return AgentVote(
            agent="execution", role="Execution Quality",
            stance=STANCE_ABSTAIN, confidence=0.65, weight=0.9,
            reasoning=f"spread {float(spread_bps):.0f} bps — pass",
        )
    return AgentVote(
        agent="execution", role="Execution Quality",
        stance=_stance_for_direction(direction, True),
        confidence=_clamp(0.55 + min(0.20, max(0.0, (volume_ratio - 1.0) * 0.10))),
        weight=0.7,
        reasoning=(f"vol {volume_ratio:.1f}× avg" +
                     (f", spread {float(spread_bps):.0f}bps" if spread_bps is not None
                      else "")),
    )


def agent_devils_advocate(context: Dict[str, Any]) -> AgentVote:
    """Stage-12.A2 — dedicated red-team agent.

    Asks one question: *why is this trade wrong?* Surveys every known
    adverse condition independently of the other agents (which lean toward
    the case for the trade). When ≥ 2 adverse factors fire, votes ABSTAIN
    loudly. When 1 fires on a directional trade, votes against direction.
    Otherwise registers an audible HOLD so it always shows up in the panel
    even when the trade looks clean.

    The point is not to be "balanced" — it's to be the agent whose only job
    is to argue the contrary case, so a unanimous bull council can still be
    interrupted by a single voice asking "are you sure?".
    """
    direction = _direction_from_action(context.get("action"))
    analytics = context.get("analytics") or {}
    features = (analytics.get("features") or context.get("features") or {})
    regime = analytics.get("regime") or {}
    optimizer = context.get("optimizer") or {}
    portfolio_risk = context.get("portfolio_risk") or {}
    cohort = context.get("cohort") or {}
    snap = context.get("snapshot") or {}
    action = (context.get("action") or "").upper()

    # If there is literally nothing to red-team against (no analytics, no
    # features, no snapshot) we go SILENT. The original 12.A2 behavior
    # was to abstain "on absent evidence" — Stage-20a recasts this as
    # the third state ("insufficient_signal") rather than a confident
    # abstain so the Chairman doesn't count it as a dissent.
    has_any_signal = bool(analytics or features or snap or portfolio_risk
                              or optimizer or cohort)
    if not has_any_signal:
        return AgentVote(
            agent="devils_advocate", role="Devil's Advocate (red team)",
            stance=STANCE_ABSTAIN, confidence=0.20, weight=1.0,
            reasoning="RED-TEAM: no analytics to verify — silent on absent evidence",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    concerns: List[str] = []
    concern_drivers: List[KeyDriver] = []
    # Helper to record a concern AND its categorized driver in lockstep.
    def _add_concern(text: str, *, category: str,
                          weight: float = 0.6,
                          time_sensitive: bool = False) -> None:
        concerns.append(text)
        concern_drivers.append(KeyDriver(
            description=text,
            source_category=category,
            direction=DIRECTION_SHORT if direction == "long"
                      else (DIRECTION_LONG if direction == "short"
                            else DIRECTION_ABSTAIN),
            weight=weight,
            time_sensitive=time_sensitive,
        ))

    # Event proximity — earnings inside 3 days.
    earnings_days = features.get("earnings_days")
    if earnings_days is not None:
        try:
            d = float(earnings_days)
            if 0 <= d <= 3:
                _add_concern(f"earnings in {d:.0f}d — IV crush risk",
                                category="fundamentals", weight=0.7,
                                time_sensitive=True)
        except Exception:
            pass

    # IV elevation hostile to long premium.
    iv_rank = float(features.get("iv_rank") or 0.0)
    if ("CALL" in action or "PUT" in action) and iv_rank > 80:
        _add_concern(f"IV rank {iv_rank:.0f}% — premium buyer disadvantaged",
                          category="volatility", weight=0.6)

    # Pin probability near a known wall.
    pin_prob = float(features.get("pinning_probability") or 0.0)
    if pin_prob > 0.65:
        _add_concern(f"pin probability {pin_prob:.0%} suggests sideways drift",
                          category="microstructure_flow", weight=0.5,
                          time_sensitive=True)

    # Regime mismatch — long signal in a bearish or choppy tape.
    trend = (regime.get("trend") or "unknown").lower()
    if direction == "long" and trend in ("bearish", "choppy"):
        _add_concern(f"long signal but tape is {trend}",
                          category="price_structure", weight=0.6)
    if direction == "short" and trend == "bullish":
        _add_concern("short signal into a bullish tape",
                          category="price_structure", weight=0.6)

    # Cross-asset risk-off into longs / risk-on into shorts.
    vix = float(features.get("vix") or snap.get("vix") or 0.0)
    if direction == "long" and vix > 25:
        _add_concern(f"VIX {vix:.1f} — risk-off favors holding back longs",
                          category="volatility", weight=0.6,
                          time_sensitive=True)

    # Cold cohort — the (strategy × regime) combination has been losing.
    cohort_wr = cohort.get("win_rate")
    cohort_n = cohort.get("closed_count")
    if (cohort_wr is not None and cohort_n is not None
            and cohort_n >= 10 and cohort_wr < 0.40):
        _add_concern(
            f"cohort win-rate {cohort_wr:.0%} on {cohort_n} trades — cold",
            category="portfolio_state", weight=0.6,
        )

    # Drawdown — adding risk into a drawdown.
    drawdown = float(portfolio_risk.get("drawdown_pct")
                       or optimizer.get("drawdown_pct") or 0.0)
    if drawdown > 0.06:
        _add_concern(f"portfolio drawdown {drawdown:.1%}",
                          category="portfolio_state",
                          weight=_clamp(drawdown * 6.0))

    # Optimizer materially cut size — the sizer already sees something off.
    requested = float(optimizer.get("requested_dollar") or 0.0)
    recommended = float(optimizer.get("recommended_dollar") or 0.0)
    if requested > 0 and recommended < requested * 0.4:
        _add_concern(
            f"optimizer cut size from ${requested:.0f} → ${recommended:.0f}",
            category="portfolio_state", weight=0.5,
        )

    # Stage-19 — Earnings Call Intelligence.
    ei = context.get("earnings_intel") or {}
    if ei:
        tone = ei.get("management_tone") or ""
        gc = ei.get("guidance_change") or ""
        if tone == "cautious" and direction == "long":
            _add_concern("earnings call tone CAUTIOUS",
                              category="fundamentals", weight=0.6)
        if gc == "reduced":
            _add_concern("management LOWERED guidance",
                              category="fundamentals", weight=0.8)
        elif gc == "withdrawn":
            _add_concern("management WITHDREW guidance",
                              category="fundamentals", weight=0.9)
        if (ei.get("margin_trajectory") or "") == "contracting" \
                and direction == "long":
            _add_concern("margins contracting per earnings call",
                              category="fundamentals", weight=0.7)

    # MITS Phase 14.E — book-degrading guard. Whenever an open position's
    # thesis-health score has fallen below the exit threshold the operator
    # tuned for the exit agent, the red-team argues against piling fresh
    # risk on top of a bleeding book.
    try:
        health_threshold = float(TUNABLES.thesis_health_exit_threshold)
    except Exception:
        health_threshold = 40.0
    for ph in (context.get("open_positions_thesis_health") or []):
        try:
            score = float(ph.get("score"))
        except (TypeError, ValueError):
            continue
        if score >= health_threshold:
            continue
        traits = ph.get("degraded_traits") or []
        traits_str = ", ".join(traits) if traits else "none reported"
        _add_concern(
            f"Open position {ph.get('ticker')} has degraded thesis "
            f"(score {score:.0f}/100, degraded traits: {traits_str}); "
            "proposing a new trade while existing book is bleeding",
            category="portfolio_state", weight=0.7,
        )

    # Composite stance.
    if len(concerns) >= 2:
        stance = STANCE_ABSTAIN
        conf = _clamp(0.55 + 0.10 * len(concerns))
        reason = "RED-TEAM: " + " · ".join(concerns[:3])
        reasoning_type_v = REASONING_CONTRIBUTING
        risk = RISK_HIGH
    elif len(concerns) == 1 and direction in ("long", "short"):
        stance = STANCE_SELL if direction == "long" else STANCE_BUY
        conf = 0.55
        reason = "RED-TEAM caution: " + concerns[0]
        reasoning_type_v = _reasoning_type_for(stance, direction)
        risk = RISK_MEDIUM
    else:
        # No adverse factors — vote HOLD so the agent always shows up but
        # without opposing a clean trade. HOLD with no key_drivers is
        # legal under the 20a contract as long as confidence stays
        # below ``min_confidence_for_contribution``; we set 0.40 by
        # default which is just above the 0.35 floor, so we ALSO emit
        # one synthetic "clean setup" driver to keep the contract
        # satisfied without inventing evidence.
        stance = STANCE_HOLD
        conf = 0.40
        reason = "no contrarian flags — clean setup"
        reasoning_type_v = REASONING_CONTRIBUTING
        risk = RISK_LOW
        concern_drivers.append(KeyDriver(
            description="surveyed adverse conditions — none fired",
            source_category="price_structure",
            direction=DIRECTION_ABSTAIN,
            weight=0.3,
        ))

    return AgentVote(
        agent="devils_advocate", role="Devil's Advocate (red team)",
        stance=stance, confidence=conf, weight=1.0,
        reasoning=reason,
        reasoning_type=reasoning_type_v,
        risk_level=risk,
        expected_edge=_expected_edge_bps(conf, 1.0,
                                              sign=-1 if concerns else 0),
        invalidators=["adverse condition list shrinks below 2",
                        "trend flips in our favor"],
        key_drivers=concern_drivers,
    )


def agent_microstructure(context: Dict[str, Any]) -> AgentVote:
    """Stage-17 consolidated — Options Microstructure + Flow + Execution.

    A single voice on "is this tradeable right now?" combining:
      • Options flow + dealer positioning (was agent_flow)
      • IV / pinning / OPEX proximity     (was agent_options)
      • Spread / liquidity                (was agent_execution)
      • Stage-18b: SEC insider activity (Form 4 burst flag)

    Stage-20a: emits structured key_drivers tagged with their source
    category — ``microstructure_flow`` for flow/volume/spread,
    ``volatility`` for IV/pinning, ``insider_flow`` for Form-4 activity.
    """
    direction = _direction_from_action(context.get("action"))
    features = ((context.get("analytics") or {}).get("features")
                  or context.get("features") or {})
    snap = context.get("snapshot") or {}
    action = (context.get("action") or "").upper()
    is_options = "CALL" in action or "PUT" in action

    flow_b = features.get("flow_bullishness")
    sweeps = features.get("premarket_bullish_sweeps")
    iv_rank = float(features.get("iv_rank") or 0.0)
    pin_prob = float(features.get("pinning_probability") or 0.0)
    earnings_days = features.get("earnings_days")
    volume = float(snap.get("volume") or 0.0)
    avg_volume = float(snap.get("avg_volume") or 0.0) or 1.0
    volume_ratio = float(features.get("volume_ratio") or (volume / avg_volume))
    spread_bps = features.get("spread_bps")

    # Stage-18b — SEC Form-4 burst feature.
    insider_30d_count = 0
    ticker_for_edgar = context.get("ticker") or ""
    if ticker_for_edgar:
        try:
            from backend.bot.data.edgar import insider_activity_summary
            insider_30d_count = (insider_activity_summary(
                ticker_for_edgar, days=30) or {}).get("form4_count", 0)
        except Exception:
            insider_30d_count = 0

    # Stage-18b — FINRA short pressure.
    short_pressure_data: Dict[str, Any] = {}
    if ticker_for_edgar:
        try:
            from backend.bot.data.finra import short_pressure
            short_pressure_data = short_pressure(ticker_for_edgar) or {}
        except Exception:
            short_pressure_data = {}

    has_flow = flow_b is not None or sweeps is not None
    has_vol = (snap.get("volume") is not None
                  or features.get("volume_ratio") is not None)
    has_iv = bool(features.get("iv_rank"))
    has_any = has_flow or has_vol or has_iv or is_options

    # Silent: no microstructure inputs at all.
    if not has_any:
        return AgentVote(
            agent="microstructure", role="Microstructure (flow + options + execution)",
            stance=STANCE_ABSTAIN, confidence=0.20, weight=1.0,
            reasoning="no microstructure data available",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    concerns: List[str] = []
    notes: List[str] = []

    # Hard execution problems → abstain regardless of direction.
    if volume_ratio < 0.4:
        return AgentVote(
            agent="microstructure", role="Microstructure (flow + options + execution)",
            stance=STANCE_ABSTAIN, confidence=0.65, weight=1.0,
            reasoning=f"thin tape: volume {volume_ratio:.1f}× avg — slippage risk",
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_HIGH,
            expected_edge=_expected_edge_bps(0.65, 1.0, sign=0),
            invalidators=["volume normalizes to > 0.7x avg"],
            key_drivers=[KeyDriver(
                description=f"volume {volume_ratio:.1f}× avg (thin tape)",
                source_category="microstructure_flow",
                direction=DIRECTION_ABSTAIN,
                weight=0.7,
                time_sensitive=True,
            )],
        )
    if spread_bps is not None and float(spread_bps) > 40:
        return AgentVote(
            agent="microstructure", role="Microstructure (flow + options + execution)",
            stance=STANCE_ABSTAIN, confidence=0.70, weight=1.0,
            reasoning=f"spread {float(spread_bps):.0f} bps — pass",
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_HIGH,
            expected_edge=_expected_edge_bps(0.70, 1.0, sign=0),
            invalidators=["spread tightens below 25 bps"],
            key_drivers=[KeyDriver(
                description=f"spread {float(spread_bps):.0f} bps",
                source_category="microstructure_flow",
                direction=DIRECTION_ABSTAIN,
                weight=0.7,
                time_sensitive=True,
            )],
        )

    # Options-specific hostility
    if is_options:
        if iv_rank > 80:
            concerns.append(f"IV rank {iv_rank:.0f}% elevated")
        if pin_prob > 0.65:
            concerns.append(f"pin prob {pin_prob:.0%} near wall")
        if earnings_days is not None and 0 <= float(earnings_days) <= 7:
            concerns.append(f"earnings in {float(earnings_days):.0f}d")
        if len(concerns) >= 2:
            stance = STANCE_SELL if direction == "long" else STANCE_ABSTAIN
            drivers = []
            if iv_rank > 80:
                drivers.append(KeyDriver(
                    description=f"IV rank {iv_rank:.0f}%",
                    source_category="volatility",
                    direction=DIRECTION_SHORT if direction == "long" else DIRECTION_ABSTAIN,
                    weight=0.7,
                ))
            if pin_prob > 0.65:
                drivers.append(KeyDriver(
                    description=f"pin probability {pin_prob:.0%}",
                    source_category="microstructure_flow",
                    direction=DIRECTION_SHORT if direction == "long" else DIRECTION_ABSTAIN,
                    weight=0.6,
                    time_sensitive=True,
                ))
            if earnings_days is not None and 0 <= float(earnings_days) <= 7:
                drivers.append(KeyDriver(
                    description=f"earnings in {float(earnings_days):.0f}d (IV crush)",
                    source_category="fundamentals",
                    direction=DIRECTION_SHORT if direction == "long" else DIRECTION_ABSTAIN,
                    weight=0.6,
                    time_sensitive=True,
                ))
            return AgentVote(
                agent="microstructure", role="Microstructure (flow + options + execution)",
                stance=stance, confidence=0.70, weight=1.0,
                reasoning="; ".join(concerns),
                reasoning_type=_reasoning_type_for(stance, direction)
                    if stance != STANCE_ABSTAIN else REASONING_CONTRIBUTING,
                risk_level=RISK_HIGH,
                expected_edge=_expected_edge_bps(0.70, 1.0, sign=-1),
                invalidators=["IV rank < 60 and pin prob < 0.4"],
                key_drivers=drivers,
            )

    # Flow alignment score (when data is present).
    flow_score = None
    if flow_b is not None or sweeps is not None:
        flow_score = float(flow_b or 0.0) + 0.5 * float(sweeps or 0.0)
        notes.append(f"flow {flow_score:+.2f}")

    # Composite alignment decision.
    if flow_score is None and not is_options:
        # No flow data, no options → vote on execution quality alone.
        notes.append(f"vol {volume_ratio:.1f}× avg")
        if spread_bps is not None:
            notes.append(f"spread {float(spread_bps):.0f}bps")
        stance = _stance_for_direction(direction, True)
        conf = _clamp(0.55 + min(0.20, max(0.0, (volume_ratio - 1.0) * 0.10)))
        drivers = [KeyDriver(
            description=f"volume {volume_ratio:.1f}× avg",
            source_category="microstructure_flow",
            direction=_driver_direction(stance, direction),
            weight=_clamp(0.3 + min(0.4, max(0.0, (volume_ratio - 1.0) * 0.2))),
        )]
        if spread_bps is not None:
            drivers.append(KeyDriver(
                description=f"spread {float(spread_bps):.0f} bps",
                source_category="microstructure_flow",
                direction=_driver_direction(stance, direction),
                weight=0.3,
            ))
        return AgentVote(
            agent="microstructure", role="Microstructure (flow + options + execution)",
            stance=stance, confidence=conf,
            weight=0.8, reasoning="; ".join(notes),
            reasoning_type=_reasoning_type_for(stance, direction),
            risk_level=RISK_MEDIUM,
            expected_edge=_expected_edge_bps(conf, 0.8),
            invalidators=["volume drops below 0.7× avg"],
            key_drivers=drivers,
        )

    # Has flow signal → flow alignment drives the vote.
    if direction == "long":
        aligned = (flow_score or 0.0) > 0.15
    elif direction == "short":
        aligned = (flow_score or 0.0) < -0.15
    else:
        aligned = False

    if concerns:
        notes.extend(concerns)

    insider_dampener = 0.0
    if insider_30d_count >= 5:
        notes.append(f"insider activity heavy: {insider_30d_count} Form 4 / 30d")
        insider_dampener = 0.05
    elif insider_30d_count >= 3:
        notes.append(f"insider activity: {insider_30d_count} Form 4 / 30d")
        insider_dampener = 0.03

    squeeze_boost = 0.0
    sp_level = (short_pressure_data.get("level") or "unknown")
    sp_trend = (short_pressure_data.get("trend") or "unknown")
    if sp_level in ("high", "moderate") and sp_trend == "rising":
        if direction == "long" and aligned:
            squeeze_boost = 0.08
            notes.append(f"short pressure {sp_level}/{sp_trend} → squeeze fuel")
        elif direction == "short":
            squeeze_boost = -0.05
            notes.append(f"crowded short trade ({sp_level} SI)")
    elif sp_level == "high":
        notes.append(f"short interest {sp_level} ({short_pressure_data.get('latest_short_pct')})")
    if not notes:
        notes.append("clean tape")

    stance = _stance_for_direction(direction, aligned)
    conf = _clamp(0.50 + abs(flow_score or 0.0) * 0.5
                       - (0.10 if concerns else 0.0)
                       - insider_dampener
                       + squeeze_boost)

    drivers = []
    if flow_score is not None:
        drivers.append(KeyDriver(
            description=f"flow score {flow_score:+.2f}",
            source_category="microstructure_flow",
            direction=_driver_direction(stance, direction),
            weight=_clamp(0.3 + abs(flow_score) * 0.5),
            time_sensitive=True,
        ))
    for c in concerns:
        cat = "volatility" if "IV" in c else (
            "fundamentals" if "earnings" in c else "microstructure_flow"
        )
        drivers.append(KeyDriver(
            description=c,
            source_category=cat,
            direction=DIRECTION_SHORT if direction == "long" else DIRECTION_LONG,
            weight=0.5,
        ))
    if insider_30d_count >= 3:
        drivers.append(KeyDriver(
            description=f"{insider_30d_count} Form 4 / 30d",
            source_category="insider_flow",
            direction=DIRECTION_ABSTAIN,
            weight=_clamp(insider_30d_count / 10.0),
        ))
    if sp_level in ("high", "moderate") and sp_trend == "rising":
        drivers.append(KeyDriver(
            description=f"short pressure {sp_level}/{sp_trend}",
            source_category="microstructure_flow",
            direction=DIRECTION_LONG if direction == "long" else DIRECTION_SHORT,
            weight=0.5,
        ))
    # If still empty (no flow score and no concerns), use volume_ratio
    # as the fallback driver so we never emit a contributing vote with
    # zero drivers.
    if not drivers:
        drivers.append(KeyDriver(
            description=f"volume {volume_ratio:.1f}× avg",
            source_category="microstructure_flow",
            direction=_driver_direction(stance, direction),
            weight=0.3,
        ))

    return AgentVote(
        agent="microstructure", role="Microstructure (flow + options + execution)",
        stance=stance, confidence=conf,
        weight=1.0, reasoning="; ".join(notes),
        reasoning_type=_reasoning_type_for(stance, direction),
        risk_level=RISK_LOW if aligned and not concerns else RISK_MEDIUM,
        expected_edge=_expected_edge_bps(conf, 1.0,
                                              sign=1 if aligned else -1),
        invalidators=["flow score flips sign", "spread widens > 30 bps"],
        key_drivers=drivers,
    )


def agent_portfolio_risk(context: Dict[str, Any]) -> AgentVote:
    """Stage-17 consolidated — Portfolio Risk + Composition.

    Merges drawdown / concentration / net-beta checks with theme-heat /
    cohort-cold checks. A single voice answering "should the portfolio
    take more of this?". Stage-20a: every driver tagged with the
    ``portfolio_state`` source category — book-level evidence that is
    structurally distinct from market signals.
    """
    direction = _direction_from_action(context.get("action"))
    portfolio_risk = context.get("portfolio_risk") or {}
    optimizer = context.get("optimizer") or {}
    cohort = context.get("cohort") or {}

    drawdown = float(portfolio_risk.get("drawdown_pct")
                       or optimizer.get("drawdown_pct") or 0.0)
    flags = portfolio_risk.get("concentration_flags") or []
    net_beta = float(portfolio_risk.get("net_beta") or 0.0)
    top_theme = portfolio_risk.get("top_theme")
    theme_pct = float(portfolio_risk.get("top_theme_pct") or 0.0)
    cohort_wr = cohort.get("win_rate")

    has_any_signal = bool(portfolio_risk or optimizer or cohort)
    if not has_any_signal:
        return AgentVote(
            agent="portfolio_risk", role="Portfolio Risk + Composition",
            stance=STANCE_ABSTAIN, confidence=0.20, weight=1.0,
            reasoning="no portfolio data available",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    # Hard veto: drawdown band breach
    if drawdown > 0.08:
        return AgentVote(
            agent="portfolio_risk", role="Portfolio Risk + Composition",
            stance=STANCE_ABSTAIN, confidence=0.75, weight=1.2,
            reasoning=f"portfolio in {drawdown:.1%} drawdown — pause new risk",
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_HIGH,
            expected_edge=_expected_edge_bps(0.75, 1.2, sign=-1),
            invalidators=[f"drawdown recovers below 5%"],
            key_drivers=[KeyDriver(
                description=f"portfolio drawdown {drawdown:.1%}",
                source_category="portfolio_state",
                direction=DIRECTION_ABSTAIN,
                weight=_clamp(drawdown * 5.0),
            )],
        )
    # Hard veto: theme concentration
    if theme_pct > 0.40:
        return AgentVote(
            agent="portfolio_risk", role="Portfolio Risk + Composition",
            stance=STANCE_ABSTAIN, confidence=0.70, weight=1.0,
            reasoning=f"theme {top_theme} already {theme_pct:.0%} of book",
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_HIGH,
            expected_edge=_expected_edge_bps(0.70, 1.0, sign=-1),
            invalidators=[f"theme exposure drops below 30%"],
            key_drivers=[KeyDriver(
                description=f"theme {top_theme or '?'} at {theme_pct:.0%} of book",
                source_category="portfolio_state",
                direction=DIRECTION_ABSTAIN,
                weight=_clamp(theme_pct * 1.5),
            )],
        )
    # Concentration flags or extreme net beta → oppose direction
    if flags or abs(net_beta) > 1.8:
        why = (f"concentration flags: {', '.join(str(f) for f in flags)[:120]}"
                if flags else f"net beta {net_beta:+.2f} extreme")
        stance = STANCE_SELL if direction == "long" else STANCE_BUY
        drivers = []
        if flags:
            drivers.append(KeyDriver(
                description=f"concentration: {', '.join(str(f) for f in flags)[:80]}",
                source_category="portfolio_state",
                direction=_driver_direction(stance, direction),
                weight=0.7,
            ))
        if abs(net_beta) > 1.8:
            drivers.append(KeyDriver(
                description=f"net β {net_beta:+.2f}",
                source_category="portfolio_state",
                direction=_driver_direction(stance, direction),
                weight=_clamp(abs(net_beta) / 3.0),
            ))
        return AgentVote(
            agent="portfolio_risk", role="Portfolio Risk + Composition",
            stance=stance, confidence=0.65, weight=1.1, reasoning=why,
            reasoning_type=_reasoning_type_for(stance, direction),
            risk_level=RISK_HIGH,
            expected_edge=_expected_edge_bps(0.65, 1.1, sign=-1),
            invalidators=["concentration flags clear",
                            "net beta normalizes below 1.5"],
            key_drivers=drivers,
        )
    # Cold cohort → fade direction
    if cohort_wr is not None and cohort_wr < 0.40:
        stance = STANCE_SELL if direction == "long" else STANCE_BUY
        return AgentVote(
            agent="portfolio_risk", role="Portfolio Risk + Composition",
            stance=stance, confidence=0.65, weight=1.0,
            reasoning=f"cohort win-rate {cohort_wr:.0%} weak — fade",
            reasoning_type=_reasoning_type_for(stance, direction),
            risk_level=RISK_MEDIUM,
            expected_edge=_expected_edge_bps(0.65, 1.0, sign=-1),
            invalidators=["cohort win rate recovers above 50%"],
            key_drivers=[KeyDriver(
                description=f"cohort win rate {cohort_wr:.0%}",
                source_category="portfolio_state",
                direction=_driver_direction(stance, direction),
                weight=_clamp((0.50 - cohort_wr) * 2.0),
            )],
        )
    # Clean: vote with direction
    note = (f"drawdown {drawdown:.1%}, β {net_beta:+.2f}, "
              f"theme {top_theme or 'n/a'} {theme_pct:.0%}")
    if cohort_wr is not None:
        note += f", cohort wr {cohort_wr:.0%}"
    stance = _stance_for_direction(direction, True)
    conf = _clamp(0.55 + (0.15 if (cohort_wr or 0) > 0.55 else 0.0))
    drivers = [KeyDriver(
        description=f"drawdown {drawdown:.1%}, β {net_beta:+.2f}",
        source_category="portfolio_state",
        direction=_driver_direction(stance, direction),
        weight=0.4,
    )]
    if theme_pct > 0:
        drivers.append(KeyDriver(
            description=f"theme {top_theme or 'n/a'} {theme_pct:.0%}",
            source_category="portfolio_state",
            direction=_driver_direction(stance, direction),
            weight=_clamp(0.2 + theme_pct),
        ))
    if cohort_wr is not None and cohort_wr > 0.55:
        drivers.append(KeyDriver(
            description=f"cohort win rate {cohort_wr:.0%}",
            source_category="portfolio_state",
            direction=_driver_direction(stance, direction),
            weight=_clamp(cohort_wr),
        ))
    return AgentVote(
        agent="portfolio_risk", role="Portfolio Risk + Composition",
        stance=stance, confidence=conf, weight=1.0, reasoning=note,
        reasoning_type=_reasoning_type_for(stance, direction),
        risk_level=RISK_LOW,
        expected_edge=_expected_edge_bps(conf, 1.0),
        invalidators=["drawdown exceeds 6%", "theme exposure exceeds 35%"],
        key_drivers=drivers,
    )


def agent_mechanical_trend(context: Dict[str, Any]) -> AgentVote:
    """STRAT.1 — deterministic rule-based trend agent.

    Implements the same 4-condition continuation rule as the
    ``ema50_momentum`` strategy (in ``all_strategies.py``), but votes
    BUY/HOLD/ABSTAIN instead of returning a Signal. Designed to give
    the council a *non-LLM baseline* — every cycle, the AI Brain has
    to beat (or match) this mechanical vote to earn its API cost.

    Conditions for a BUY vote:
      - price > EMA50
      - EMA50 > EMA200
      - RSI(14) > 50
      - volume > 20-day average
      - price > EMA200 (regime filter)

    When the action is SELL_*, the agent votes ABSTAIN (the rule is
    long-only by design — voting SELL would invent an opinion the rule
    doesn't actually hold).
    """
    snap = context.get("snapshot") or {}
    direction = _direction_from_action(context.get("action"))

    price = float(snap.get("price") or 0)
    ema50 = float(snap.get("ema50") or snap.get("ma50") or 0)
    ema200 = float(snap.get("ema200") or snap.get("ma200") or 0)
    rsi = float(snap.get("rsi") or 50.0)
    volume = float(snap.get("volume") or 0)
    avg_volume = float(snap.get("avg_volume") or 0)

    if price <= 0 or ema50 <= 0 or ema200 <= 0:
        return AgentVote(
            agent="mechanical_trend", role="Mechanical Trend (rule-based)",
            stance=STANCE_ABSTAIN, confidence=0.20, weight=0.8,
            reasoning="EMA/price data unavailable",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    # Track which conditions pass — useful for the reasoning string +
    # any future analysis of which gate is the most common blocker.
    c1 = price > ema50
    c2 = ema50 > ema200
    c3 = rsi > 50.0
    c4 = (avg_volume > 0) and (volume > avg_volume)
    c_regime = price > ema200
    passed = sum((c1, c2, c3, c4, c_regime))
    all_pass = passed == 5

    drivers: List[KeyDriver] = []
    if c1:
        drivers.append(KeyDriver(
            description=f"price ${price:.2f} > EMA50 ${ema50:.2f}",
            source_category="price_structure",
            direction=DIRECTION_LONG, weight=0.6,
        ))
    if c2:
        gap = ((ema50 - ema200) / ema200) * 100 if ema200 > 0 else 0.0
        drivers.append(KeyDriver(
            description=f"EMA50 > EMA200 ({gap:.1f}% gap)",
            source_category="price_structure",
            direction=DIRECTION_LONG,
            weight=_clamp(0.3 + min(0.5, gap / 10.0)),
        ))
    if c3:
        drivers.append(KeyDriver(
            description=f"RSI {rsi:.0f} > 50",
            source_category="price_structure",
            direction=DIRECTION_LONG,
            weight=_clamp((rsi - 50.0) / 20.0),
        ))
    if c4:
        vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        drivers.append(KeyDriver(
            description=f"volume {vol_ratio:.1f}× 20-day avg",
            source_category="price_structure",
            direction=DIRECTION_LONG,
            weight=_clamp((vol_ratio - 1.0) * 0.4),
        ))

    # On SELL_* actions the long-only rule has no opinion — silent abstain.
    if direction == "short":
        return AgentVote(
            agent="mechanical_trend", role="Mechanical Trend (rule-based)",
            stance=STANCE_ABSTAIN, confidence=0.30, weight=0.8,
            reasoning="long-only rule — no opinion on short proposals",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    if all_pass:
        # All four conditions + regime filter aligned. Confidence reflects
        # the strength margin: a barely-passing rule (price just above
        # EMA50, RSI 51) is weaker than a fully-confirmed move.
        rsi_strength = min(1.0, (rsi - 50.0) / 20.0)
        gap_strength = min(1.0, ((ema50 - ema200) / ema200) * 20.0
                           if ema200 > 0 else 0)
        vol_strength = min(1.0, (volume / avg_volume - 1.0)
                           if avg_volume > 0 else 0)
        conf = _clamp(0.55 + 0.10 * rsi_strength + 0.10 * gap_strength
                          + 0.10 * vol_strength)
        # Always vote BUY when the rule fires, even if the action under
        # consideration is HOLD — that's the mechanical agent surfacing
        # an opinion the LLM agents may have missed.
        stance = STANCE_BUY
        return AgentVote(
            agent="mechanical_trend", role="Mechanical Trend (rule-based)",
            stance=stance, confidence=conf, weight=1.0,
            reasoning=(f"EMA50 continuation rule firing: "
                       f"price > EMA50 > EMA200, RSI {rsi:.0f}, "
                       f"vol {volume/max(avg_volume,1):.1f}× avg"),
            reasoning_type=_reasoning_type_for(stance, direction),
            risk_level=RISK_LOW,
            expected_edge=_expected_edge_bps(conf, 1.0),
            key_drivers=drivers,
            invalidators=[
                "price closes below EMA50 — trailing exit triggers",
                "EMA50 crosses back below EMA200 — trend break",
                "RSI drops below 45 — momentum loss",
            ],
        )

    # Some conditions failed → contributing HOLD with the failing gates
    # listed so the council can see exactly which leg is missing.
    blockers = []
    if not c1: blockers.append(f"price ${price:.2f} ≤ EMA50 ${ema50:.2f}")
    if not c2: blockers.append("EMA50 ≤ EMA200")
    if not c3: blockers.append(f"RSI {rsi:.0f} ≤ 50")
    if not c4 and avg_volume > 0:
        blockers.append(f"volume {volume/avg_volume:.2f}× ≤ avg")
    if not c_regime: blockers.append("price below EMA200")
    return AgentVote(
        agent="mechanical_trend", role="Mechanical Trend (rule-based)",
        stance=STANCE_ABSTAIN, confidence=_clamp(0.30 + 0.08 * passed),
        weight=0.9,
        reasoning=("rule not firing — blocked by: "
                   + "; ".join(blockers)),
        reasoning_type=REASONING_CONTRIBUTING,
        risk_level=RISK_MEDIUM,
        key_drivers=drivers,
    )


# Stage-17 → STRAT.1 — six-agent panel. The fifth agent (mechanical_trend)
# is the deterministic baseline — every cycle, the AI Brain has to beat
# (or match) this rule's verdict to justify its API spend.
#
# Previous 8-agent panel kept in the codebase (functions above) but
# removed from AGENT_FUNCS so they don't vote unless a caller explicitly
# invokes them. Old persisted consensus blobs in detail_json continue to
# render with their original agent names — no migration needed.
# MITS-5 — `agent_thesis_health` is the 7th agent. Loaded lazily here
# to keep its module's import cost off the boot path. The agent only
# fires when ``context["open_position"]`` is populated; new-trade
# evaluations get a silent abstain.
from backend.bot.agents.thesis_health import agent_thesis_health  # noqa: E402
# MITS Phase 14.C — Simulator. Sits BEFORE devils_advocate so the
# red-team pass can react to the simulator's payoff distribution.
from backend.bot.agents.simulator_agent import agent_simulator  # noqa: E402

AGENT_FUNCS: List[Tuple[str, str, Callable[[Dict[str, Any]], AgentVote]]] = [
    ("market", "Market Regime", agent_market),
    ("microstructure", "Microstructure (flow + options + execution)",
        agent_microstructure),
    ("macro", "Macro/Cross-Asset", agent_macro),
    ("portfolio_risk", "Portfolio Risk + Composition", agent_portfolio_risk),
    ("mechanical_trend", "Mechanical Trend (rule-based)", agent_mechanical_trend),
    ("thesis_health", "Thesis Health (winner-trajectory exit monitor)",
        agent_thesis_health),
    ("simulator", "Forward Payoff Simulator", agent_simulator),
    ("devils_advocate", "Devil's Advocate (red team)", agent_devils_advocate),
]


def list_agents() -> List[Dict[str, str]]:
    """Names + roles — surfaced by ``GET /agents/list`` for the UI."""
    return [{"agent": name, "role": role} for name, role, _ in AGENT_FUNCS]


# ── consensus engine ────────────────────────────────────────────────────


def _weighted_mean(values: List[float], weights: List[float]) -> float:
    if not values:
        return 0.0
    w_sum = sum(weights) or 1.0
    return sum(v * w for v, w in zip(values, weights)) / w_sum


def _three_way_probs(votes: List[AgentVote]) -> Dict[str, float]:
    """Stage-12.C7 — softmax-style three-way probability head over
    {long, short, abstain}. Each agent contributes ``confidence * weight``
    to its stance bucket (HOLD splits 50/50 between long+short since HOLD
    means "stay the course / neutral"). Result is normalized to sum to 1.0
    so callers can compare directly: P(long) vs P(short) vs P(abstain).
    """
    if not votes:
        return {"long": 0.0, "short": 0.0, "abstain": 1.0}
    score = {"long": 0.0, "short": 0.0, "abstain": 0.0}
    for v in votes:
        contrib = float(v.confidence) * float(v.weight)
        if v.stance == STANCE_BUY:
            score["long"] += contrib
        elif v.stance == STANCE_SELL:
            score["short"] += contrib
        elif v.stance == STANCE_ABSTAIN:
            score["abstain"] += contrib
        else:                       # HOLD splits — keeps probability mass
            score["long"] += contrib * 0.5
            score["short"] += contrib * 0.5
    total = sum(score.values()) or 1.0
    return {k: round(v / total, 4) for k, v in score.items()}


def _apply_dynamic_weights(votes: List[AgentVote],
                              weights: Optional[Dict[str, float]]) -> List[AgentVote]:
    """Multiply each vote's static weight by its scorecard-derived
    multiplier. Returns *copies* of the votes — caller's list untouched.
    Stage-20a: preserves all structured contract fields on the copy."""
    if not weights:
        return votes
    out: List[AgentVote] = []
    for v in votes:
        mult = float(weights.get(v.agent, 1.0))
        if mult == 1.0:
            out.append(v)
        else:
            out.append(AgentVote(
                agent=v.agent, role=v.role, stance=v.stance,
                confidence=v.confidence,
                weight=round(v.weight * mult, 3),
                reasoning=v.reasoning,
                reasoning_type=v.reasoning_type,
                expected_edge=v.expected_edge,
                risk_level=v.risk_level,
                invalidators=list(v.invalidators),
                key_drivers=list(v.key_drivers),
            ))
    return out


# MITS Phase 15.D — which analytical axes each agent feeds. Agents
# with an empty list contribute only to the composite when nonzero,
# but never to a named axis (portfolio_risk, thesis_health,
# devils_advocate are book/exit/red-team roles, not analytical axes).
_AGENT_AXIS_MAP: Dict[str, List[str]] = {
    "market":            ["market_structure"],
    "macro":             ["market_structure", "macro"],
    "microstructure":    ["technical", "options"],
    "mechanical_trend":  ["technical"],
    "portfolio_risk":    [],
    "thesis_health":     [],
    "simulator":         ["simulator"],
    "devils_advocate":   [],
}


def _compute_confidence_breakdown(
    votes: List[AgentVote],
    *,
    analog_cluster: Optional[Dict[str, Any]] = None,
    simulator_verdict: Optional[Dict[str, Any]] = None,
) -> ConfidenceBreakdown:
    """Per-axis breakdown of council confidence.

    Each axis collects (confidence, weight) pairs from agents in
    ``_AGENT_AXIS_MAP``. The options axis additionally re-derives a
    sub-confidence from microstructure's key_drivers when those drivers
    cite ``volatility`` or ``microstructure_flow`` and mention IV / pin
    / flow — so an agent that fired purely on volume-delta doesn't
    inflate options confidence. ``historical_analog`` uses the
    AnalogCluster's pre-summarized ``analog_win_rate`` when present,
    otherwise computes it from realized returns in the cohort.
    ``simulator`` falls back to the consensus-level
    ``simulator_verdict.conviction_score`` if the simulator vote was
    silent.

    Axis health: green if >= 2 votes contribute, yellow if 1, red if 0.
    Composite: weighted mean of axes with nonzero confidence.
    """
    axes: Dict[str, List[Tuple[float, float]]] = {
        "market_structure": [], "technical": [], "options": [],
        "historical_analog": [], "simulator": [], "macro": [],
    }
    for v in votes:
        if v.is_silent():
            continue
        for axis in _AGENT_AXIS_MAP.get(v.agent, []):
            axes[axis].append((float(v.confidence), float(v.weight)))

    # Options sub-confidence — only count microstructure toward the
    # options axis if its drivers actually mention options-relevant
    # signals (IV, pin risk, options flow). A volume-only microstructure
    # vote contributes to ``technical`` but not ``options``.
    for v in votes:
        if v.agent != "microstructure" or v.is_silent():
            continue
        opt_drivers = [kd for kd in (v.key_drivers or [])
                       if kd.source_category in ("volatility",
                                                  "microstructure_flow")
                       and any(k in (kd.description or "").lower()
                               for k in ("iv", "pin", "flow"))]
        if opt_drivers:
            mean_dw = sum(d.weight for d in opt_drivers) / len(opt_drivers)
            axes["options"].append((float(v.confidence) * mean_dw,
                                     float(v.weight)))

    if analog_cluster and analog_cluster.get("cohort_size", 0) > 0:
        wr = analog_cluster.get("analog_win_rate")
        if wr is None:
            ans = analog_cluster.get("analogs") or []
            if ans:
                wr = (sum(1 for a in ans
                          if a.get("realized_return_pct", 0) > 0)
                      / len(ans))
        if wr is not None:
            axes["historical_analog"].append((float(wr), 1.0))

    if not axes["simulator"] and simulator_verdict:
        cs = simulator_verdict.get("conviction_score")
        if cs is not None:
            axes["simulator"].append((float(cs), 1.0))

    means: Dict[str, float] = {}
    health: Dict[str, str] = {}
    n_map: Dict[str, int] = {}
    for ax, pairs in axes.items():
        n_map[ax] = len(pairs)
        if not pairs:
            means[ax] = 0.0
            health[ax] = "red"
            continue
        wsum = sum(c * w for c, w in pairs)
        wnorm = sum(w for _, w in pairs) or 1.0
        means[ax] = round(wsum / wnorm, 4)
        health[ax] = "green" if len(pairs) >= 2 else "yellow"
    nonzero = [v for v in means.values() if v > 0]
    composite = round(statistics.mean(nonzero), 4) if nonzero else 0.0
    return ConfidenceBreakdown(
        market_structure=means["market_structure"],
        technical=means["technical"],
        options=means["options"],
        historical_analog=means["historical_analog"],
        simulator=means["simulator"],
        macro=means["macro"],
        composite=composite,
        axis_health=health,
        axis_n=n_map,
    )


def aggregate(votes: List[AgentVote],
              *,
              abstain_threshold: float = 0.40,
              disagreement_threshold: float = 0.30,
              dynamic_weights: Optional[Dict[str, float]] = None,
              market_internals: Optional[MarketInternalsScore] = None,
              quorum_min: Optional[int] = None,
              ) -> Consensus:
    """Combine ``votes`` into a single recommendation + sizing.

    - ``abstain_threshold``: if ≥ this fraction of voters abstain, the
      consensus is ABSTAIN — we don't have enough conviction.
    - ``disagreement_threshold``: if the std-dev of confidences exceeds
      this, we size down (mixed signal).
    - ``dynamic_weights``: optional per-agent multiplier on the static
      ``vote.weight``.
    - ``market_internals``: optional shared MarketInternalsScore to
      attach to the resulting Consensus (Stage-20a).
    - ``quorum_min``: required number of non-silent agents
      (contributing OR dissenting) before any recommendation other than
      ``abstain`` is allowed. Defaults to
      ``TUNABLES.agent_quorum_min``. When violated, the consensus is
      forced to ``abstain`` with
      ``recommendation_reason == "insufficient_council_quorum"`` —
      Stage-20a quorum rule.
    """
    votes = _apply_dynamic_weights(votes, dynamic_weights)
    # MITS Phase 16.A — removed dead apply_memory_bias(votes, context)
    # block. The bare ``context`` symbol was undefined in aggregate()'s
    # scope (the function takes votes + keyword-only sizing params), so
    # every cycle raised NameError that the broad try/except silently
    # swallowed. Memory bias still ships through the explicit
    # build_agent_context → apply_memory_bias path callers wire
    # themselves (see tests/unit/test_agent_context_knowledge.py).
    quorum_required = int(quorum_min if quorum_min is not None
                            else TUNABLES.agent_quorum_min)
    internals_dict = market_internals.to_dict() if market_internals else {}

    def _chairman(stance: str, quorum_met_flag: bool,
                       quorum_count_val: int) -> Dict[str, Any]:
        """Run the Chairman on the current votes + decision context.
        Returns the report dict (empty when legacy-only)."""
        report = chairman_review(
            votes=votes,
            consensus_stance=stance,
            abstain_stance=STANCE_ABSTAIN,
            quorum_met=quorum_met_flag,
            quorum_count=quorum_count_val,
            quorum_required=quorum_required,
        )
        return report.to_dict()

    if not votes:
        return Consensus(stance=STANCE_ABSTAIN, confidence=0.0,
                          disagreement_score=0.0, recommendation="abstain",
                          size_multiplier=0.0, abstain_count=0,
                          supporters=[], dissenters=[],
                          probs={"long": 0.0, "short": 0.0, "abstain": 1.0},
                          votes=[],
                          silent_agents=[], quorum_met=False,
                          quorum_required=quorum_required, quorum_count=0,
                          recommendation_reason="no_votes",
                          market_internals=internals_dict,
                          chairman_report=_chairman(STANCE_ABSTAIN, False, 0))

    abstain_count = sum(1 for v in votes if v.stance == STANCE_ABSTAIN)
    silent_votes = [v for v in votes if v.is_silent()]
    silent_names = [v.agent for v in silent_votes]
    non_silent_count = len(votes) - len(silent_votes)
    quorum_met = non_silent_count >= quorum_required

    probs = _three_way_probs(votes)
    n = len(votes)

    # Quorum gate (Stage-20a). Falls below ⇒ refuse to execute regardless
    # of what the contributing agents say. Surfaced as a first-class
    # abstain reason so the UI distinguishes it from "agents abstained".
    if not quorum_met:
        return Consensus(
            stance=STANCE_ABSTAIN, confidence=0.0, disagreement_score=0.0,
            recommendation="abstain", size_multiplier=0.0,
            abstain_count=abstain_count,
            supporters=[], dissenters=[],
            probs=probs,
            votes=[v.to_dict() for v in votes],
            silent_agents=silent_names,
            quorum_met=False,
            quorum_required=quorum_required,
            quorum_count=non_silent_count,
            recommendation_reason="insufficient_council_quorum",
            market_internals=internals_dict,
            chairman_report=_chairman(STANCE_ABSTAIN, False,
                                              non_silent_count),
        )

    if abstain_count / n >= abstain_threshold:
        return Consensus(
            stance=STANCE_ABSTAIN, confidence=0.0, disagreement_score=0.0,
            recommendation="abstain", size_multiplier=0.0,
            abstain_count=abstain_count,
            supporters=[], dissenters=[],
            probs=probs,
            votes=[v.to_dict() for v in votes],
            silent_agents=silent_names,
            quorum_met=True,
            quorum_required=quorum_required,
            quorum_count=non_silent_count,
            recommendation_reason="majority_abstain",
            market_internals=internals_dict,
            chairman_report=_chairman(STANCE_ABSTAIN, True,
                                              non_silent_count),
        )

    # Score each side by weighted confidence.
    buy_score = sum(v.confidence * v.weight for v in votes if v.stance == STANCE_BUY)
    sell_score = sum(v.confidence * v.weight for v in votes if v.stance == STANCE_SELL)
    hold_score = sum(v.confidence * v.weight for v in votes if v.stance == STANCE_HOLD)

    if buy_score > sell_score and buy_score > hold_score:
        stance = STANCE_BUY
    elif sell_score > buy_score and sell_score > hold_score:
        stance = STANCE_SELL
    else:
        stance = STANCE_HOLD

    supporters = [v for v in votes if v.stance == stance]
    dissenters = [v for v in votes
                    if v.stance != stance and v.stance != STANCE_ABSTAIN]

    supporter_confs = [v.confidence for v in supporters]
    supporter_weights = [v.weight for v in supporters]
    consensus_conf = _weighted_mean(supporter_confs, supporter_weights)

    all_confs = [v.confidence for v in votes if v.stance != STANCE_ABSTAIN]
    disagreement = (statistics.pstdev(all_confs) if len(all_confs) >= 2 else 0.0)

    if stance == STANCE_HOLD or consensus_conf < 0.45:
        recommendation = "abstain"
        size_mult = 0.0
        rec_reason = "low_consensus"
    elif disagreement > disagreement_threshold or len(dissenters) >= 2:
        recommendation = "size_down"
        size_mult = _clamp(0.50 + (consensus_conf - 0.5))
        rec_reason = "disagreement_size_down"
    else:
        recommendation = "execute"
        size_mult = _clamp(0.80 + (consensus_conf - 0.6) * 0.5)
        rec_reason = ""

    return Consensus(
        stance=stance, confidence=round(consensus_conf, 3),
        disagreement_score=round(disagreement, 3),
        recommendation=recommendation,
        size_multiplier=round(size_mult, 2),
        abstain_count=abstain_count,
        supporters=[v.agent for v in supporters],
        dissenters=[v.agent for v in dissenters],
        probs=probs,
        votes=[v.to_dict() for v in votes],
        silent_agents=silent_names,
        quorum_met=True,
        quorum_required=quorum_required,
        quorum_count=non_silent_count,
        recommendation_reason=rec_reason,
        market_internals=internals_dict,
        chairman_report=_chairman(stance, True, non_silent_count),
    )


def run_consensus(context: Dict[str, Any],
                   *,
                   only: Optional[List[str]] = None,
                   abstain_threshold: float = 0.40,
                   disagreement_threshold: float = 0.30,
                   use_dynamic_weights: bool = False,
                   enrich_with_claude: bool = False,
                   quorum_min: Optional[int] = None,
                   ) -> Consensus:
    """Run every agent on ``context`` and aggregate the votes.

    ``only`` optionally restricts which agents fire (useful for testing
    or for opt-out via config).

    Stage-20a: computes the shared ``MarketInternalsScore`` once and
    threads it into the context (under ``market_internals_obj``)
    before any agent runs. This is the architectural shift the
    framework called for — agents read off the same panel view instead
    of independently re-interpreting macro + breadth + credit.

    ``use_dynamic_weights`` (Stage-14) loads per-agent weights from the
    scorecard. ``quorum_min`` overrides the council quorum (defaults to
    ``TUNABLES.agent_quorum_min``).
    """
    # Stage-20a — compute the shared market view once before any agent fires.
    features = ((context.get("analytics") or {}).get("features")
                  or context.get("features") or {})
    internals = compute_market_internals(
        macro=context.get("macro"),
        breadth=context.get("breadth"),
        snapshot=context.get("snapshot"),
        features=features,
        cot=context.get("cot_snapshot"),
        earnings_intel=context.get("earnings_intel"),
        insider=context.get("insider_activity"),
        short_pressure=context.get("short_pressure"),
    )
    # Attach to context so agents that want it can read it. We use a
    # distinct key name (``market_internals_obj``) to avoid colliding
    # with any pre-existing ``market_internals`` dict a caller might
    # have stashed in the context.
    context = dict(context)        # don't mutate caller's dict
    context["market_internals_obj"] = internals

    votes: List[AgentVote] = []
    for name, role, fn in AGENT_FUNCS:
        if only is not None and name not in only:
            continue
        try:
            v = fn(context)
        except Exception as exc:
            logger.debug("agent %s failed: %s", name, exc, exc_info=True)
            v = AgentVote(agent=name, role=role, stance=STANCE_ABSTAIN,
                            confidence=0.0, weight=0.5,
                            reasoning=f"agent crashed: {exc}")
        votes.append(v)
    # MITS Phase 18.D — Online Agent Weight Adaptation (Advisory).
    # Gated on ``TUNABLES.adaptive_weights_apply_enabled`` (default OFF).
    # When ON, override each vote's static weight with the latest
    # persisted ``weight_active`` from ``agent_weight_history``. This
    # affects only future cycles — replay paths read from the
    # ``agent_outputs_json`` snapshot, not the live adaptive table, so
    # 16.B replay drift stays at 0.0.
    #
    # 18-FU Gap R2 — route the apply call through
    # ``apply_weights_for_cycle`` so each consumption writes a row to
    # ``weight_application_log`` (cycle_id, history_id, weight_set
    # snapshot). The advisor's read path keeps using the un-logged
    # ``get_current_weights`` (where logging would be spurious).
    if bool(getattr(TUNABLES, "adaptive_weights_apply_enabled", False)):
        try:
            from backend.bot.learning.weight_adaptation import (
                apply_weights_for_cycle,
            )
            cycle_id = context.get("cycle_id")
            decision_provenance_id = context.get("decision_provenance_id")
            composite_quality = context.get("composite_quality_at_apply")
            adaptive = apply_weights_for_cycle(
                cycle_id=(str(cycle_id) if cycle_id else None),
                decision_provenance_id=(
                    int(decision_provenance_id)
                    if decision_provenance_id is not None else None
                ),
                composite_quality_at_apply=(
                    float(composite_quality)
                    if composite_quality is not None else None
                ),
            )
            for vote in votes:
                if vote.agent in adaptive:
                    vote.weight = float(adaptive[vote.agent])
        except Exception:
            logger.debug(
                "adaptive_weights_apply override failed; using static weights",
                exc_info=True,
            )
    weights = None
    if use_dynamic_weights:
        try:
            from backend.bot.agents.scorecard import vote_weights
            weights = vote_weights()
        except Exception:
            logger.debug("vote_weights load failed; defaulting to static", exc_info=True)
            weights = None
    # Stage-15 — optional Claude enrichment: single batched call augments
    # every agent's one-line heuristic with 1-2 sentences of richer text.
    # Falls through silently to heuristics on any failure. Stage-20a:
    # preserves the structured contract fields on the enriched copy.
    if enrich_with_claude:
        try:
            from backend.bot.agents.claude_voice import get_enricher
            enricher = get_enricher()
            if enricher.available:
                enriched = enricher.enrich(votes=votes, context=context)
                if enriched:
                    new_votes: List[AgentVote] = []
                    for v in votes:
                        extra = enriched.get(v.agent)
                        if extra:
                            new_votes.append(AgentVote(
                                agent=v.agent, role=v.role, stance=v.stance,
                                confidence=v.confidence, weight=v.weight,
                                reasoning=f"{v.reasoning}\n➜ {extra}",
                                reasoning_type=v.reasoning_type,
                                expected_edge=v.expected_edge,
                                risk_level=v.risk_level,
                                invalidators=list(v.invalidators),
                                key_drivers=list(v.key_drivers),
                            ))
                        else:
                            new_votes.append(v)
                    votes = new_votes
        except Exception:
            logger.debug("agent voice enrich failed; using heuristics",
                            exc_info=True)
    consensus = aggregate(votes, abstain_threshold=abstain_threshold,
                       disagreement_threshold=disagreement_threshold,
                       dynamic_weights=weights,
                       market_internals=internals,
                       quorum_min=quorum_min)
    # MITS Phase 14.C — simulator verdict written to the shared context
    # by ``agent_simulator``. Lift it onto the Consensus so the engine
    # can read it without re-running the simulation.
    sv = context.get("simulator_verdict")
    if isinstance(sv, dict):
        consensus.simulator_verdict = sv
    # MITS Phase 15.D — multi-axis confidence breakdown. Post-aggregate
    # so the ``aggregate`` signature stays stable for callers that hit
    # it directly (tests, lineage replays).
    consensus.confidence_breakdown = _compute_confidence_breakdown(
        votes,
        analog_cluster=context.get("analog_cluster"),
        simulator_verdict=consensus.simulator_verdict,
    ).to_dict()

    # MITS Phase 16.B — emit the typed AgentInput + per-vote AgentOutput
    # projections so the decision_provenance ledger has a stable, lossless
    # shape to replay from. Each vote's ``agent_output`` field is populated
    # in place, and the envelope + per-agent projection lists are attached
    # to the Consensus so downstream consumers (engine.run_cycle,
    # /lineage, replay) read them without re-doing the projection.
    # Failures are logged at debug — they never abort consensus; replay
    # just sees None on that vote and falls back to the legacy projection.
    try:
        from backend.bot.agents.contracts_v2 import (
            agent_output_from_vote, make_agent_input,
        )
        agent_input = make_agent_input(context)
        consensus_direction = _consensus_direction_from(consensus)
        outputs: List[Dict[str, Any]] = []
        for vote in votes:
            try:
                ao = agent_output_from_vote(
                    vote, consensus_direction=consensus_direction,
                ).to_dict()
                vote.agent_output = ao
                outputs.append(ao)
            except Exception:
                logger.debug(
                    "agent_output projection failed for %s", vote.agent,
                    exc_info=True,
                )
        consensus.agent_input = agent_input.to_dict()
        consensus.agent_outputs = outputs
        # Reflect the populated agent_output onto the persisted votes dict
        # so callers that only read ``Consensus.votes`` (not the original
        # AgentVote objects) still see the projection.
        consensus.votes = [v.to_dict() for v in votes]
    except Exception:
        logger.debug("agent_input envelope build failed", exc_info=True)
    return consensus


def _consensus_direction_from(consensus: "Consensus") -> str:
    """Map the council's stance into the directional vocabulary used
    by ``KeyDriver.direction`` (long / short / neutral). HOLD becomes
    neutral so directional drivers on a HOLD consensus land in
    ``concerns`` — the abstain bucket isn't a "support" position."""
    s = (consensus.stance or "").lower()
    if s == STANCE_BUY:
        return "long"
    if s == STANCE_SELL:
        return "short"
    return "neutral"
