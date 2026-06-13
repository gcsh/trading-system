"""Stage-12.A1 Agent Scorecards.

Every closed trade in the system has:
  • Trade.detail_json["consensus"]["votes"]  — what each agent voted
  • Trade.pnl                                  — what actually happened

This module joins them and computes per-agent accuracy: how often each agent
voted on the side that the trade's outcome favored, and how often it
abstained when it should have / shouldn't have.

This is the **foundation** for dynamic vote-weighting (Stage 12 follow-up)
and the Research Layer's "which agent is degrading" surface (Stage 13).

Read-only, pure compute. No DB writes, no state.

Scoring rules (simple, transparent):

  Profitable trade (pnl > 0):
    • Agent voted BUY  → +1 correct
    • Agent voted SELL → +1 wrong
    • Agent voted HOLD/ABSTAIN → +1 missed_winner

  Losing trade (pnl < 0):
    • Agent voted BUY  → +1 wrong
    • Agent voted SELL → +1 correct
    • Agent voted ABSTAIN → +1 avoided_loser   (this is a GOOD outcome)
    • Agent voted HOLD → +1 neutral_on_loser

The hit_rate metric below excludes ABSTAIN votes from the denominator —
abstaining is judged separately via avoided_loser / missed_winner.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.bot.agents import AGENT_FUNCS
from backend.db import session_scope
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


@dataclass
class AgentScore:
    agent: str
    role: str
    decided_trades: int           # trades where agent voted BUY or SELL
    correct: int
    wrong: int
    hit_rate: Optional[float]     # correct / decided_trades  (None if 0 decided)
    abstain_count: int
    avoided_losers: int           # abstain on losing trade (good)
    missed_winners: int           # abstain on winning trade (bad)
    avg_confidence: float
    pnl_attributed: float         # sum of pnl on trades where agent voted with the winner

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScorecardReport:
    window: str                   # e.g. "all", "30d", "recent_50"
    closed_trades: int            # trades included in this report
    agents: List[AgentScore] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window": self.window,
            "closed_trades": self.closed_trades,
            "agents": [a.to_dict() for a in self.agents],
        }


# ── extraction ───────────────────────────────────────────────────────────


def _load_closed_with_consensus(limit: int = 2000) -> List[Dict[str, Any]]:
    """Pull closed trades + parse persisted consensus votes. Returns
    a list of ``{pnl, votes, status}`` dicts."""
    out: List[Dict[str, Any]] = []
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(Trade)
                .where(Trade.pnl.is_not(None))
                # P1.2 — agent scoring must reflect LIVE consensus votes
                # against LIVE outcomes. Synthetic-replay trades carry
                # no consensus votes and would bias the scorecard.
                .where(Trade.status != "closed_by_reset")
                .where(Trade.signal_source != "historical_replay")
                .order_by(desc(Trade.timestamp))
                .limit(limit)
            ).scalars().all())
            for r in rows:
                detail = {}
                if r.detail_json:
                    try:
                        detail = json.loads(r.detail_json) or {}
                    except Exception:
                        detail = {}
                consensus = (detail.get("consensus") or {})
                votes = consensus.get("votes") or []
                if not votes:
                    continue
                out.append({
                    "trade_id": r.id,
                    "pnl": float(r.pnl),
                    "votes": votes,
                    "status": r.status,
                })
    except Exception:
        logger.debug("scorecard load failed", exc_info=True)
    return out


# ── scoring ─────────────────────────────────────────────────────────────


def _empty_score(agent: str, role: str) -> Dict[str, Any]:
    return {
        "agent": agent, "role": role,
        "decided_trades": 0, "correct": 0, "wrong": 0,
        "abstain_count": 0, "avoided_losers": 0, "missed_winners": 0,
        "_conf_sum": 0.0, "_conf_n": 0,
        "pnl_attributed": 0.0,
    }


def build_scorecard(*, limit: int = 2000) -> ScorecardReport:
    """Compute per-agent scorecards across the most recent ``limit`` closed
    trades that carry a persisted consensus block."""
    closed = _load_closed_with_consensus(limit=limit)
    n = len(closed)

    # Seed with the canonical agent roster so the UI always shows every
    # agent, even ones that haven't voted yet.
    accum: Dict[str, Dict[str, Any]] = {}
    role_by_name: Dict[str, str] = {}
    for name, role, _ in AGENT_FUNCS:
        accum[name] = _empty_score(name, role)
        role_by_name[name] = role

    for trade in closed:
        pnl = trade["pnl"]
        is_winner = pnl > 0
        is_loser = pnl < 0
        for vote in trade["votes"]:
            name = str(vote.get("agent") or "")
            if not name:
                continue
            if name not in accum:
                accum[name] = _empty_score(name, str(vote.get("role") or name))
            score = accum[name]
            stance = str(vote.get("stance") or "").lower()
            conf = float(vote.get("confidence") or 0.0)
            score["_conf_sum"] += conf
            score["_conf_n"] += 1

            if stance == "buy":
                if is_winner:
                    score["correct"] += 1
                    score["pnl_attributed"] += pnl
                elif is_loser:
                    score["wrong"] += 1
                score["decided_trades"] += 1
            elif stance == "sell":
                if is_loser:
                    score["correct"] += 1
                    # On loss, "sell" stance avoided the loss → credit the
                    # absolute pnl as captured edge.
                    score["pnl_attributed"] += abs(pnl)
                elif is_winner:
                    score["wrong"] += 1
                score["decided_trades"] += 1
            elif stance == "abstain":
                score["abstain_count"] += 1
                if is_loser:
                    score["avoided_losers"] += 1
                elif is_winner:
                    score["missed_winners"] += 1
            # hold is intentionally neutral — neither rewarded nor punished.

    # Finalize: hit_rate, avg_confidence, rounding.
    agents: List[AgentScore] = []
    for name, score in accum.items():
        decided = score["decided_trades"]
        hit_rate = (score["correct"] / decided) if decided else None
        avg_conf = (score["_conf_sum"] / score["_conf_n"]) if score["_conf_n"] else 0.0
        agents.append(AgentScore(
            agent=name,
            role=role_by_name.get(name, score["role"]),
            decided_trades=decided,
            correct=score["correct"],
            wrong=score["wrong"],
            hit_rate=round(hit_rate, 3) if hit_rate is not None else None,
            abstain_count=score["abstain_count"],
            avoided_losers=score["avoided_losers"],
            missed_winners=score["missed_winners"],
            avg_confidence=round(avg_conf, 3),
            pnl_attributed=round(score["pnl_attributed"], 2),
        ))

    # Sort: agents with data first (decided trades desc), then by hit-rate.
    agents.sort(key=lambda a: (a.decided_trades > 0,
                                  a.hit_rate or 0.0,
                                  a.decided_trades), reverse=True)

    return ScorecardReport(window=f"recent_{n}", closed_trades=n, agents=agents)


# ── recent-performance lookup (Item #1 — memory-rich agent context) ────


def recent_performance(agent_name: str, *,
                            window: int = 30) -> Dict[str, Any]:
    """Per-agent slim performance snapshot over the last ``window``
    closed-with-consensus trades. Designed to be plumbed into the
    individual agent's context dict so the agent can self-temper —
    "I've been over-confident in this regime, dial back."

    Returns:
        {
            "agent": str,
            "decided_trades": int,
            "hit_rate": float | None,        # 0..1
            "calibration_error": float,      # |avg_conf - hit_rate|
            "abstain_count": int,
            "avg_confidence": float,         # 0..1
            "drift_flag": bool,              # True when calibration_error > 0.20
            "window_size": int,
        }
    """
    closed = _load_closed_with_consensus(limit=max(window * 5, 100))
    closed = closed[:window]  # most-recent window
    decided = correct = abstain = 0
    conf_sum = 0.0
    conf_n = 0
    for trade in closed:
        pnl = trade["pnl"]
        is_winner = pnl > 0
        is_loser = pnl < 0
        for vote in trade["votes"]:
            if str(vote.get("agent")) != agent_name:
                continue
            stance = str(vote.get("stance") or "").lower()
            conf = float(vote.get("confidence") or 0.0)
            conf_sum += conf
            conf_n += 1
            if stance == "buy":
                decided += 1
                if is_winner:
                    correct += 1
            elif stance == "sell":
                decided += 1
                if is_loser:
                    correct += 1
            elif stance == "abstain":
                abstain += 1
    hit_rate = (correct / decided) if decided else None
    avg_conf = (conf_sum / conf_n) if conf_n else 0.0
    calib_err = (abs(avg_conf - hit_rate) if hit_rate is not None else 0.0)
    return {
        "agent": agent_name,
        "decided_trades": decided,
        "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "calibration_error": round(calib_err, 3),
        "abstain_count": abstain,
        "avg_confidence": round(avg_conf, 3),
        "drift_flag": calib_err > 0.20 and decided >= 5,
        "window_size": len(closed),
    }


# ── dynamic weight derivation ───────────────────────────────────────────


def vote_weights(*, prior_weight: int = 20,
                    default_weight: float = 1.0,
                    max_boost: float = 1.5,
                    max_penalty: float = 0.5) -> Dict[str, float]:
    """Derive a per-agent vote weight from the scorecard hit-rate using
    Bayesian shrinkage toward 0.5 (no edge prior).

    Stage-16 — the original Stage-12 implementation used a hard
    ``min_decided`` cutoff that left dynamic weighting dormant until 20
    trades accumulated. The shrinkage version blends the observed hit
    rate with the prior so the weight evolves smoothly from trade #1:

        shrunken_rate = (correct + prior_weight * 0.5) /
                        (decided + prior_weight)

    With ``prior_weight = 20``: 1 decided trade barely moves the rate,
    20 trades moves it halfway, 100 trades dominates the prior. Then we
    map ``shrunken_rate`` to a weight in ``[max_penalty, max_boost]``.

    The result: agents start at 1.0 and *gradually* gain or lose weight
    as evidence accumulates — no step-function activation at trade #20.
    """
    report = build_scorecard()
    out: Dict[str, float] = {}
    for agent in report.agents:
        # Shrink the observed hit rate toward 0.5 (no edge) with the
        # prior_weight pseudo-observations.
        n = agent.decided_trades
        correct = agent.correct
        shrunken_rate = (correct + prior_weight * 0.5) / (n + prior_weight)

        # Map shrunken_rate to weight, centered at 0.5.
        delta = (shrunken_rate - 0.5) * 2.0       # [-1, +1]
        if delta >= 0:
            w = default_weight + delta * (max_boost - 1.0) * default_weight
        else:
            w = default_weight + delta * (1.0 - max_penalty) * default_weight
        out[agent.agent] = round(max(default_weight * max_penalty,
                                        min(default_weight * max_boost, w)), 3)
    return out
