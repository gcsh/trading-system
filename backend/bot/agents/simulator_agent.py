"""MITS Phase 14.C — Simulator council agent.

Wraps ``SimulatorAgent.simulate(...)`` in the structured ``AgentVote``
shape every other council member produces. The agent runs the analog
roll-forward + Monte Carlo for the candidate trade, then:

  • If ``p_max_loss`` crosses ``TUNABLES.simulator_max_loss_veto`` the
    vote is STANCE_ABSTAIN with confidence=1.0 and a reasoning string
    that begins ``simulator_veto:``. The engine reads the verdict's
    ``reject_reason`` field directly and short-circuits the cycle —
    this vote's stance only documents the council position.

  • Otherwise the vote takes the direction of the proposed action
    (STANCE_BUY for long candidates, STANCE_SELL for short candidates)
    with confidence = ``verdict.conviction_score``.

  • When the cohort + analog set is too thin (analog sample_size < 5)
    the vote is silent — STANCE_ABSTAIN with
    ``reasoning_type=insufficient_signal`` so the Chairman knows we have
    no evidence rather than no opinion.

The verdict dict is also written into ``context["simulator_verdict"]``
so the engine can read it after ``run_consensus`` resolves without
re-running the simulation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.bot.agents.contract import (
    DIRECTION_ABSTAIN,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_INSUFFICIENT_SIGNAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
)
from backend.bot.analysis.simulator import SimulatorAgent, SimulatorVerdict
from backend.config import TUNABLES


_SIMULATOR = SimulatorAgent()

_MIN_ANALOG_SAMPLES = 5         # Below this we report "insufficient signal"


def _direction_from_action(action: Optional[str]) -> str:
    """Translate the engine's action string into the simulator's
    direction enum. Defaults to long_stock so the simulator still runs
    for ambiguous signals (the council can lean on it for context)."""
    if not action:
        return "long_stock"
    a = action.upper()
    if a == "BUY_CALL" or "_CALL" in a and a.startswith("BUY"):
        return "long_call"
    if a == "BUY_PUT" or "_PUT" in a and a.startswith("BUY"):
        return "long_put"
    if a.startswith("SELL") or "SHORT" in a:
        return "short_stock"
    return "long_stock"


def _strike_from_context(context: Dict[str, Any], spot: float) -> Optional[float]:
    """Pull a proposed strike off the signal metadata. Falls back to ATM
    when the candidate is an option but the strike hasn't been chosen."""
    meta = context.get("signal_meta") or {}
    strike = meta.get("strike") or meta.get("proposed_strike")
    if strike is not None:
        try:
            return float(strike)
        except (TypeError, ValueError):
            return None
    return spot if spot > 0 else None


def _dte_from_context(context: Dict[str, Any]) -> Optional[int]:
    meta = context.get("signal_meta") or {}
    for key in ("dte", "days_to_expiry", "horizon_days"):
        val = meta.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    # No explicit DTE — read the analytics horizon if present.
    analytics = context.get("analytics") or {}
    horizon = analytics.get("horizon_days") or analytics.get("dte")
    if horizon is not None:
        try:
            return int(horizon)
        except (TypeError, ValueError):
            return None
    return None


def _risk_level(verdict: SimulatorVerdict) -> str:
    if verdict.sample_size == 0:
        return RISK_UNKNOWN
    if verdict.p_max_loss >= 0.20:
        return RISK_HIGH
    if verdict.p_max_loss >= 0.10:
        return RISK_MEDIUM
    return RISK_LOW


def agent_simulator(context: Dict[str, Any]):
    """7th council member — payoff-distribution voice.

    Reads ``ticker``, ``action``, ``analytics.regime``, ``snapshot``,
    and ``knowledge_evidence.cells`` from the council context. Returns
    a structured ``AgentVote`` and writes the full verdict dict to
    ``context["simulator_verdict"]``.
    """
    # Local import — avoids a circular dependency with backend.bot.agents.
    from backend.bot.agents import (
        AgentVote, STANCE_ABSTAIN, STANCE_BUY, STANCE_SELL,
    )

    ticker = (context.get("ticker") or "").upper()
    action = context.get("action") or ""
    direction = _direction_from_action(action)

    snapshot = context.get("snapshot") or {}
    spot = float(snapshot.get("price") or snapshot.get("close") or 0.0)

    analytics = context.get("analytics") or {}
    regime_block = analytics.get("regime") or {}
    regime = (regime_block.get("trend") or "unknown").lower()
    vol_state = (regime_block.get("volatility") or "normal").lower()

    cohort_cells = ((context.get("knowledge_evidence") or {}).get("cells")
                    or [])
    # Heuristic pattern label — first cell's pattern when available.
    pattern = ""
    if cohort_cells:
        pattern = str(cohort_cells[0].get("pattern") or "")

    # Empty / unworkable inputs → silent abstain. No verdict to publish.
    if not ticker or spot <= 0:
        return AgentVote(
            agent="simulator",
            role="Forward Payoff Simulator",
            stance=STANCE_ABSTAIN,
            confidence=0.20,
            weight=1.0,
            reasoning="no ticker or spot — cannot simulate",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    strike = _strike_from_context(context, spot)
    dte = _dte_from_context(context)

    verdict = _SIMULATOR.simulate(
        ticker=ticker, pattern=pattern, regime=regime,
        vol_state=vol_state, direction=direction, spot=spot,
        strike=strike, dte=dte, cohort_cells=cohort_cells,
    )

    # Publish for the engine. ``run_consensus`` does ``dict(context)``
    # once and reuses the same dict for every agent — writes here land
    # in the same shallow copy the caller of ``aggregate`` later reads.
    context["simulator_verdict"] = verdict.to_dict()

    # Veto path. We still emit a vote so the Chairman / UI see the
    # simulator weighed in, but the engine reads ``reject_reason`` and
    # short-circuits before any sizing / order construction.
    if verdict.reject_reason:
        drivers = [KeyDriver(
            description=(f"p_max_loss {verdict.p_max_loss:.0%} "
                         f"breaches veto threshold "
                         f"{float(TUNABLES.simulator_max_loss_veto):.0%}"),
            source_category="portfolio_state",
            direction=DIRECTION_ABSTAIN,
            weight=1.0,
            time_sensitive=True,
        )]
        return AgentVote(
            agent="simulator",
            role="Forward Payoff Simulator",
            stance=STANCE_ABSTAIN,
            confidence=1.0,
            weight=1.5,
            reasoning=verdict.reject_reason,
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_HIGH,
            key_drivers=drivers,
            invalidators=["p_max_loss falls below veto threshold"],
        )

    # Cohort too thin → silent abstain.
    if verdict.sample_size < _MIN_ANALOG_SAMPLES:
        return AgentVote(
            agent="simulator",
            role="Forward Payoff Simulator",
            stance=STANCE_ABSTAIN,
            confidence=0.25,
            weight=0.8,
            reasoning=(f"cohort too thin: {verdict.sample_size} samples "
                       f"< {_MIN_ANALOG_SAMPLES} minimum"),
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    # Normal path — stance aligned with proposed direction.
    if direction in ("long_stock", "long_call"):
        stance = STANCE_BUY
        driver_dir = DIRECTION_LONG
    elif direction in ("short_stock", "long_put"):
        stance = STANCE_SELL
        driver_dir = DIRECTION_SHORT
    else:
        stance = STANCE_ABSTAIN
        driver_dir = DIRECTION_ABSTAIN

    drivers = [
        KeyDriver(
            description=(f"E[payoff]=${verdict.expected_payoff:.2f}, "
                         f"p_win={verdict.p_win:.0%}, "
                         f"5%ile DD=${verdict.max_drawdown_pctile_5:.2f}"),
            source_category="portfolio_state",
            direction=driver_dir,
            weight=min(1.0, max(0.1, verdict.conviction_score)),
            time_sensitive=False,
        ),
        KeyDriver(
            description=(f"{verdict.mode} sample size "
                         f"{verdict.sample_size}"),
            source_category="portfolio_state",
            direction=driver_dir,
            weight=min(1.0, 0.3 + verdict.sample_size / 1000.0),
            time_sensitive=False,
        ),
    ]
    return AgentVote(
        agent="simulator",
        role="Forward Payoff Simulator",
        stance=stance,
        confidence=float(verdict.conviction_score),
        weight=1.0,
        reasoning=(f"{verdict.mode}: E[payoff]="
                   f"${verdict.expected_payoff:.2f}, "
                   f"p_win={verdict.p_win:.0%}, "
                   f"p_max_loss={verdict.p_max_loss:.0%}, "
                   f"n={verdict.sample_size}"),
        reasoning_type=REASONING_CONTRIBUTING,
        risk_level=_risk_level(verdict),
        key_drivers=drivers,
        invalidators=[
            "p_max_loss crosses veto threshold",
            "cohort sample size drops below minimum",
        ],
    )
