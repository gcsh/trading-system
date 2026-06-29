"""Stage-13.C9 Research Layer — autonomous "what changed today" agent.

Most institutional systems have a *pull* dashboard the operator can poke.
This module gives them a *push* digest: a daily report that answers

  • Which agents are degrading (hit-rate dropped vs last week)?
  • Which features lost importance (rank shifted out of top-10)?
  • Which strategies / regimes started losing edge (cohort win-rate dropped)?
  • Which feeds got worse (more errors, slower)?
  • What's the cumulative cost trend vs P&L trend?

Each finding has a *delta* and a *severity*. The composite report can be
rendered in the UI, surfaced via push notification, or piped into Slack.

Pure compute over the persistent tables we already have. No new schema —
just analysis. Heuristic comparisons (week-over-week, recent-vs-baseline)
so it always returns *something* useful even before we have months of data.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ALERT = "alert"


@dataclass
class Finding:
    area: str                     # agents | features | cohorts | feeds | cost
    title: str
    detail: str
    severity: str = SEVERITY_INFO
    delta: Optional[float] = None
    sample_size: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchDigest:
    generated_at: str
    findings: List[Finding] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "findings": [f.to_dict() for f in self.findings],
            "counts": self.counts,
        }


# ── individual researchers ──────────────────────────────────────────────


def _agent_drift(min_decided: int = 20) -> List[Finding]:
    """Compare recent vs all-time agent hit-rates. Flag agents that
    degraded by ≥ 10 pp."""
    from backend.bot.agents.scorecard import build_scorecard
    out: List[Finding] = []
    try:
        recent = build_scorecard(limit=50)
        baseline = build_scorecard(limit=500)
    except Exception:
        return out
    by_recent = {a.agent: a for a in recent.agents}
    by_baseline = {a.agent: a for a in baseline.agents}
    for agent_name, r in by_recent.items():
        b = by_baseline.get(agent_name)
        if (r.hit_rate is None or b is None or b.hit_rate is None
                or r.decided_trades < min_decided
                or b.decided_trades < min_decided):
            continue
        delta = r.hit_rate - b.hit_rate
        if delta <= -0.10:
            out.append(Finding(
                area="agents", title=f"{agent_name} hit-rate degrading",
                detail=(f"Recent hit-rate {r.hit_rate:.0%} vs baseline "
                          f"{b.hit_rate:.0%} ({delta:+.0%})"),
                severity=SEVERITY_ALERT if delta <= -0.20 else SEVERITY_WARN,
                delta=round(delta, 3),
                sample_size=r.decided_trades,
                metadata={"agent": agent_name},
            ))
    return out


def _feature_importance_shift(top_k: int = 10) -> List[Finding]:
    """Detect a regime shift in feature importance — features that moved
    in or out of the top-K vs the cached previous report."""
    from backend.bot.explain import compute_importance
    out: List[Finding] = []
    try:
        report = compute_importance()
    except Exception:
        return out
    if report.method != "permutation":
        # Uniform fallback — nothing to compare yet.
        return out
    top = [fi.feature for fi in report.importances[:top_k]]
    state = _state()
    prev_top = state.get("prev_top_features") or []
    moved_in = [f for f in top if f not in prev_top]
    moved_out = [f for f in prev_top if f not in top]
    state["prev_top_features"] = top
    if moved_in or moved_out:
        out.append(Finding(
            area="features",
            title="Top-K feature importance shifted",
            detail=(f"In: {', '.join(moved_in) or 'none'} · "
                      f"Out: {', '.join(moved_out) or 'none'}"),
            severity=SEVERITY_WARN if (moved_in or moved_out) else SEVERITY_INFO,
            sample_size=report.sample_size,
            metadata={"current_top": top, "prior_top": prev_top},
        ))
    return out


def _cohort_decay(min_closed: int = 10) -> List[Finding]:
    """Look at the cohort matrix and flag (strategy × regime) combos with
    win-rate < 40%."""
    from backend.bot.cohort_matrix import build_cohort_matrix
    out: List[Finding] = []
    try:
        matrix = build_cohort_matrix(limit=5000)
    except Exception:
        return out
    cells = matrix.get("cells") if isinstance(matrix, dict) else None
    if not cells:
        return out
    weak = []
    for cell in cells:
        wr = cell.get("win_rate")
        closed = cell.get("closed", 0)
        if wr is None or closed < min_closed:
            continue
        if wr < 0.40:
            weak.append((cell, closed, wr))
    weak.sort(key=lambda x: x[2])      # worst first
    for cell, closed, wr in weak[:5]:
        out.append(Finding(
            area="cohorts",
            title=(f"{cell.get('strategy', '?')} × "
                     f"{cell.get('regime', '?')} losing edge"),
            detail=(f"Win-rate {wr:.0%} on {closed} closed; "
                      f"lift {cell.get('lift', 0):+.1%} vs baseline"),
            severity=SEVERITY_ALERT if wr < 0.30 else SEVERITY_WARN,
            delta=round(wr - 0.50, 3),
            sample_size=closed,
            metadata=cell,
        ))
    return out


def _feed_health() -> List[Finding]:
    """Check the feed-monitoring SLO state for breaches."""
    out: List[Finding] = []
    try:
        from backend.bot.monitoring import feed_summary
        summary = feed_summary()
    except Exception:
        return out
    for feed in summary.get("breached_feeds") or []:
        out.append(Finding(
            area="feeds", title=f"{feed} feed SLO breached",
            detail="Latency or staleness above threshold",
            severity=SEVERITY_ALERT,
            metadata={"feed": feed},
        ))
    return out


def _cost_trend() -> List[Finding]:
    """Cost trend — total spend and alpha-per-dollar."""
    from backend.bot.ai_cost import alpha_per_dollar, totals
    out: List[Finding] = []
    try:
        t = totals()
        alpha = alpha_per_dollar()
    except Exception:
        return out
    if t.get("calls", 0) == 0:
        return out
    out.append(Finding(
        area="cost",
        title=f"AI spend ${t['cost_usd']:.2f} across {t['calls']} calls",
        detail=(f"Avg ${t.get('cost_per_call_usd', 0):.4f}/call; "
                  f"alpha-per-dollar: {alpha.get('alpha_per_dollar')}"),
        severity=(SEVERITY_WARN if (alpha.get("alpha_per_dollar") is not None
                                       and alpha["alpha_per_dollar"] < 1.0)
                    else SEVERITY_INFO),
        delta=alpha.get("alpha_per_dollar"),
        metadata={**t, **alpha},
    ))
    return out


# ── module state (week-over-week comparison memory) ─────────────────────


_STATE: Dict[str, Any] = {}


def _state() -> Dict[str, Any]:
    return _STATE


def reset_state() -> None:
    """Test helper — clear the digest's persistent comparison state."""
    _STATE.clear()


# ── orchestrator ────────────────────────────────────────────────────────


def generate_digest() -> ResearchDigest:
    """Run every researcher and combine findings into one digest."""
    findings: List[Finding] = []
    for fn in (_agent_drift, _feature_importance_shift,
                  _cohort_decay, _feed_health, _cost_trend):
        try:
            findings.extend(fn())
        except Exception:
            logger.debug("research worker %s failed", fn.__name__, exc_info=True)
    counts = Counter(f.severity for f in findings)
    return ResearchDigest(
        generated_at=datetime.utcnow().isoformat(),
        findings=findings,
        counts={
            SEVERITY_INFO: counts.get(SEVERITY_INFO, 0),
            SEVERITY_WARN: counts.get(SEVERITY_WARN, 0),
            SEVERITY_ALERT: counts.get(SEVERITY_ALERT, 0),
        },
    )
