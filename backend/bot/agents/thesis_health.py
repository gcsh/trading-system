"""MITS-5 — 7th council agent: thesis-health exit voice.

Reads:
  context["open_position"]   — the live option position (or None)
  context["winner_profile"]  — the WinnerProfile dict (or None)
  context["current_bars"]    — recent bars for the underlying (optional)

Behaviour:
  * For NEW trade evaluations (no open_position), the agent abstains
    silently with `reasoning_type=insufficient_signal` — it only fires
    on holds, not entries.
  * For OPEN positions, the agent computes a thesis-health score (0-100)
    via `backend.bot.thesis.calculate_health` and votes EXIT (stance=
    SELL for a long option position; stance=BUY for a short) when the
    score falls below TUNABLES.thesis_health_exit_threshold.
  * When the score is above threshold, the agent votes HOLD with
    intact-trait reasoning (so the operator sees positive signal too).
  * When the winner profile is untrustworthy (thin corpus), the agent
    abstains — EXIT.1's mechanical safety net continues to protect the
    trade.

The Chairman receives the vote like any other; under the legacy
recommendation path the engine consults this agent BEFORE EXIT.1 runs.
"""
from __future__ import annotations

from typing import Any, Dict

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
from backend.bot.thesis import calculate_health
from backend.bot.thesis.winner_profile import WinnerProfile
from backend.config import TUNABLES


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _profile_from_context(context: Dict[str, Any]) -> "WinnerProfile | None":
    """Allow callers to pass WinnerProfile either as the dataclass or a
    dict (e.g. from a UI endpoint that serialized through JSON)."""
    raw = context.get("winner_profile")
    if raw is None:
        return None
    if isinstance(raw, WinnerProfile):
        return raw
    if isinstance(raw, dict):
        try:
            return WinnerProfile(**raw)
        except Exception:
            return None
    return None


def agent_thesis_health(context: Dict[str, Any]):
    """Council vote on thesis-health for an OPEN position.

    Imported lazily inside `AGENT_FUNCS` so the agents package keeps
    its existing import surface (avoiding cycles with the chairman /
    contract modules).
    """
    # Lazy imports to dodge circulars.
    from backend.bot.agents import (
        AgentVote,
        STANCE_ABSTAIN,
        STANCE_BUY,
        STANCE_HOLD,
        STANCE_SELL,
    )

    open_pos = context.get("open_position") or {}
    profile = _profile_from_context(context)
    bars = context.get("current_bars")
    threshold = float(getattr(TUNABLES, "thesis_health_exit_threshold", 40.0))
    min_samples = int(getattr(TUNABLES, "thesis_health_min_samples", 30))

    # NEW trade evaluation — no open position. The agent stays silent;
    # entry-side reasoning is the other 6 agents' job.
    if not open_pos:
        return AgentVote(
            agent="thesis_health",
            role="Thesis Health (winner-trajectory exit monitor)",
            stance=STANCE_ABSTAIN,
            confidence=0.20,
            weight=1.0,
            reasoning=("THESIS-HEALTH: no open position — agent only fires "
                          "on holds"),
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    # Profile must exist and be trustworthy to vote.
    if profile is None or not profile.is_trustworthy \
            or profile.sample_size < min_samples:
        thin_n = profile.sample_size if profile else 0
        return AgentVote(
            agent="thesis_health",
            role="Thesis Health (winner-trajectory exit monitor)",
            stance=STANCE_ABSTAIN,
            confidence=0.20,
            weight=1.0,
            reasoning=(
                f"THESIS-HEALTH: winner profile thin "
                f"(N={thin_n}/{min_samples}) — abstain"
            ),
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_UNKNOWN,
        )

    health = calculate_health(open_pos, bars, profile)

    # Score is 0-100. Map to vote stance + confidence.
    is_long = (open_pos.get("option_type") or "").lower().startswith("c")
    # When the position is a CALL, exit means SELL. When PUT, "exit"
    # closes a SELL_PUT or BUY_PUT — the engine treats both the same
    # via close_option. We just emit SELL on calls and BUY on puts to
    # mean "exit this leg" within the AgentVote contract semantics.
    exit_stance = STANCE_SELL if is_long else STANCE_BUY

    # Drivers — always categorize via portfolio_state since this is a
    # position-management vote, not a market-data signal.
    drivers = []
    for t in (health.intact_traits + health.degraded_traits)[:5]:
        drivers.append(KeyDriver(
            description=f"trait {t}: {'intact' if t in health.intact_traits else 'degraded'}",
            source_category="portfolio_state",
            direction=(DIRECTION_LONG if (t in health.intact_traits) == is_long
                          else DIRECTION_SHORT),
            weight=0.5,
        ))
    if not drivers:
        drivers.append(KeyDriver(
            description=f"thesis health score {health.score:.0f}/100",
            source_category="portfolio_state",
            direction=DIRECTION_ABSTAIN,
            weight=0.3,
        ))

    if health.score < threshold:
        # Health below threshold → vote EXIT.
        # Confidence scales with how degraded the trade is + corpus depth.
        deficit = (threshold - health.score) / threshold
        conf = _clamp(0.55 + 0.40 * deficit, 0.55, 0.95)
        return AgentVote(
            agent="thesis_health",
            role="Thesis Health (winner-trajectory exit monitor)",
            stance=exit_stance,
            confidence=round(conf, 3),
            weight=1.0,
            reasoning=(
                f"THESIS-HEALTH EXIT (score {health.score:.0f}/100, "
                f"threshold {threshold:.0f}): {health.reason}"
            ),
            reasoning_type=REASONING_CONTRIBUTING,
            risk_level=RISK_HIGH,
            expected_edge=round(20.0 * deficit, 2),
            invalidators=[
                "thesis-health rises above threshold",
                "degraded traits flip back to intact",
            ],
            key_drivers=drivers,
        )

    # Healthy trade — HOLD with low-but-positive confidence so the agent
    # is visible in the panel without overpowering the directional votes.
    conf = _clamp(0.40 + 0.30 * (health.score - threshold) / max(1.0, 100.0 - threshold),
                       0.35, 0.70)
    return AgentVote(
        agent="thesis_health",
        role="Thesis Health (winner-trajectory exit monitor)",
        stance=STANCE_HOLD,
        confidence=round(conf, 3),
        weight=1.0,
        reasoning=(
            f"THESIS-HEALTH HOLD (score {health.score:.0f}/100): "
            f"{health.reason}"
        ),
        reasoning_type=REASONING_CONTRIBUTING,
        risk_level=RISK_MEDIUM if health.degraded_traits else RISK_LOW,
        key_drivers=drivers,
    )
