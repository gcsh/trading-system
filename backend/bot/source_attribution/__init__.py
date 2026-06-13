"""Stage-19 — Source Contribution Tracker.

Institutional shops ask: "Which sources actually contribute to wins?"
Not which sources are interesting in theory — which ones move the
outcome distribution after 100-200 trades have closed.

Each new data layer (Breadth, Macro/FRED, EDGAR, FINRA short interest,
CFTC COT, Insider activity, Earnings intel) emits a numeric *score* at
decision time — a single number in [-1, +1] capturing how favorably
that source views the trade. We snapshot all of those scores into
``Trade.detail_json["source_scores"]``. Once enough closed trades
accumulate, ``compute_contributions()`` joins source scores with
outcomes and reports per-source contribution to win rate + P&L.

Heuristic — pure correlation against outcome, not full ML attribution.
The user gets directionally honest evidence about which feeds are
worth investing further in.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# ── score extractors (per data source) ──────────────────────────────────


def _score_breadth(ctx: Dict[str, Any]) -> Optional[float]:
    """Breadth: healthy_advance → +1, broken → -1, narrow_rally → -0.4, etc."""
    b = ctx.get("breadth") or {}
    verdict = (b.get("verdict") or "unknown").lower()
    if verdict == "healthy_advance":
        return 0.8
    if verdict == "pullback_in_bull":
        return 0.4
    if verdict == "mixed":
        return 0.0
    if verdict == "narrow_rally_fragile":
        return -0.4
    if verdict == "broken":
        return -0.8
    return None


def _score_macro(ctx: Dict[str, Any]) -> Optional[float]:
    """Macro: synthesized from FRED panel. Curve inverted + HY wide = -;
    loose NFCI + tight HY = +. None when no panel."""
    m = ctx.get("macro") or {}
    if not m or all(v in (None, {}, [])
                       for v in m.values() if not isinstance(v, bool)):
        return None
    score = 0.0
    weight = 0
    if m.get("yield_curve_inverted") is True:
        score -= 0.4
        weight += 1
    hy = (m.get("BAMLH0A0HYM2") or {}).get("value")
    if hy is not None:
        if hy < 3.0:
            score += 0.4
        elif hy > 5.5:
            score -= 0.4
        weight += 1
    nfci = (m.get("NFCI") or {}).get("value")
    if nfci is not None:
        if nfci < -0.30:
            score += 0.4
        elif nfci > 0.30:
            score -= 0.4
        weight += 1
    return round(score, 3) if weight else None


def _score_edgar(ctx: Dict[str, Any]) -> Optional[float]:
    """EDGAR: ticker had a recent material 8-K or earnings call → score
    derived from the call's guidance / tone. None when no signal."""
    ei = ctx.get("earnings_intel") or {}
    if not ei:
        return None
    score = 0.0
    gc = ei.get("guidance_change")
    if gc == "improved":
        score += 0.5
    elif gc == "reduced":
        score -= 0.5
    elif gc == "withdrawn":
        score -= 0.8
    tone = ei.get("management_tone")
    if tone == "confident":
        score += 0.3
    elif tone == "cautious":
        score -= 0.3
    margin = ei.get("margin_trajectory")
    if margin == "expanding":
        score += 0.2
    elif margin == "contracting":
        score -= 0.2
    return round(max(-1.0, min(1.0, score)), 3)


def _score_short_interest(ctx: Dict[str, Any]) -> Optional[float]:
    """FINRA: rising short pressure on a long is constructive (squeeze
    fuel). For shorts, crowded SI is a fade signal → negative."""
    sp = ctx.get("short_pressure") or {}
    level = sp.get("level") or "unknown"
    trend = sp.get("trend") or "unknown"
    if level == "unknown":
        return None
    direction = (ctx.get("action") or "").upper()
    is_long = direction.startswith("BUY") and "PUT" not in direction
    if level == "high" and trend == "rising":
        return 0.6 if is_long else -0.4
    if level == "moderate" and trend == "rising":
        return 0.3 if is_long else -0.2
    if level == "high" and trend == "falling":
        return -0.2
    return 0.0


def _score_cot(ctx: Dict[str, Any]) -> Optional[float]:
    """CFTC COT: specs deeply long ES → equity crowding (slightly bearish
    for longs because trade is consensus). Net positions in TY indicate
    risk-off positioning."""
    cot = ctx.get("cot_snapshot") or {}
    if not cot or all(v in (None, {}) for v in cot.values()):
        return None
    es = cot.get("ES") or {}
    es_net = es.get("noncommercial_net")
    es_oi = es.get("open_interest")
    if es_net is None or es_oi in (None, 0):
        return None
    pct_net = es_net / es_oi
    # Specs crowded long (>20% of OI) → fade slightly; crowded short → boost
    if pct_net > 0.20:
        return -0.3
    if pct_net < -0.20:
        return 0.3
    return 0.0


def _score_insider(ctx: Dict[str, Any]) -> Optional[float]:
    """SEC Form-4 burst — informational, mildly negative because we
    can't yet classify buy vs sell."""
    n = (ctx.get("insider_activity") or {}).get("form4_count")
    if n is None:
        return None
    if n >= 5:
        return -0.2
    if n >= 3:
        return -0.1
    return 0.0


# Canonical source list — the order they appear in the dashboard.
SOURCES = (
    ("breadth", _score_breadth),
    ("macro", _score_macro),
    ("edgar", _score_edgar),
    ("short_interest", _score_short_interest),
    ("cot", _score_cot),
    ("insider", _score_insider),
)


def snapshot_sources(context: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Walk every source extractor over the decision context. Returns a
    dict mapping source name → numeric score in [-1, +1] (or None when
    the source had no data). Persisted into Trade.detail_json so the
    contribution rollup can join scores ↔ outcomes after the fact."""
    out: Dict[str, Optional[float]] = {}
    for name, fn in SOURCES:
        try:
            out[name] = fn(context)
        except Exception:
            logger.debug("source_attribution.%s failed", name, exc_info=True)
            out[name] = None
    return out


# ── contribution rollup ─────────────────────────────────────────────────


@dataclass
class SourceContribution:
    source: str
    sample_size: int                  # trades where this source had a score
    contribution: float               # 0-1 — normalized share of explained signal
    mean_score_winners: Optional[float]
    mean_score_losers: Optional[float]
    correlation_with_pnl: Optional[float]  # Pearson r in [-1, +1]
    favorable_hit_rate: Optional[float]    # win rate when source said "go" (>0)
    unfavorable_hit_rate: Optional[float]  # win rate when source said "skip" (<0)
    avg_pnl_when_favorable: Optional[float]
    avg_pnl_when_unfavorable: Optional[float]
    insight: str                       # one-line read

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContributionReport:
    closed_trades: int
    sources: List[SourceContribution] = field(default_factory=list)
    min_trades: int = 30
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "closed_trades": self.closed_trades,
            "min_trades": self.min_trades,
            "sources": [s.to_dict() for s in self.sources],
            "generated_at": self.generated_at,
        }


def _load_closed_with_scores(limit: int = 5000) -> List[Dict[str, Any]]:
    """Pull closed trades + parsed source_scores from detail_json.

    P1.2 — synthetic-replay trades carry no source_scores (the brain's
    per-source attribution snapshot doesn't exist for backfilled rows).
    Including them would weight data-source contribution to zero across
    the board and bias live-source weighting downward."""
    out: List[Dict[str, Any]] = []
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(Trade)
                .where(Trade.pnl.is_not(None))
                .where(Trade.status != "closed_by_reset")
                .where(Trade.signal_source != "historical_replay")
                .order_by(desc(Trade.timestamp))
                .limit(limit)
            ).scalars().all())
            for r in rows:
                if not r.detail_json:
                    continue
                try:
                    d = json.loads(r.detail_json) or {}
                except Exception:
                    continue
                scores = d.get("source_scores") or {}
                if not isinstance(scores, dict):
                    continue
                out.append({
                    "pnl": float(r.pnl), "scores": scores,
                })
    except Exception:
        logger.debug("source_attribution._load failed", exc_info=True)
    return out


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation coefficient. None when n<3 or zero variance."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return round(cov / ((vx ** 0.5) * (vy ** 0.5)), 3)


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 3) if xs else None


def _hit_rate(pnls: List[float]) -> Optional[float]:
    if not pnls:
        return None
    return round(sum(1 for p in pnls if p > 0) / len(pnls), 3)


def _insight_for(c: SourceContribution) -> str:
    """One-line human read for the dashboard."""
    if c.sample_size < 10:
        return f"only {c.sample_size} signals — need more data"
    if c.correlation_with_pnl is None:
        return "insufficient variance to score"
    r = c.correlation_with_pnl
    if abs(r) < 0.05:
        return "no correlation with outcomes — consider deprecating"
    if r > 0.20:
        return f"strong positive contributor (r={r:+.2f})"
    if r < -0.20:
        return f"strong inverse signal — flipping interpretation may help (r={r:+.2f})"
    if r > 0:
        return f"mildly positive contributor (r={r:+.2f})"
    return f"weak inverse signal (r={r:+.2f})"


def compute_contributions(*, limit: int = 5000,
                              min_trades: int = 30) -> ContributionReport:
    """Build the per-source contribution table from closed trades."""
    from datetime import datetime, timezone
    closed = _load_closed_with_scores(limit=limit)
    n_closed = len(closed)

    # Bucket scores + pnls by source
    by_source: Dict[str, Dict[str, List[float]]] = {
        name: {"scores": [], "pnls": [], "favorable_pnls": [],
                  "unfavorable_pnls": []}
        for name, _ in SOURCES
    }
    for t in closed:
        for name, _ in SOURCES:
            s = (t["scores"] or {}).get(name)
            if s is None:
                continue
            by_source[name]["scores"].append(float(s))
            by_source[name]["pnls"].append(t["pnl"])
            if s > 0:
                by_source[name]["favorable_pnls"].append(t["pnl"])
            elif s < 0:
                by_source[name]["unfavorable_pnls"].append(t["pnl"])

    # Normalize correlations to a "contribution" share that sums to 1.0
    # across sources with valid r.
    sources_with_r: List[Dict[str, Any]] = []
    for name, data in by_source.items():
        r = _pearson(data["scores"], data["pnls"]) \
            if len(data["scores"]) >= min_trades else None
        sources_with_r.append({"name": name, "data": data, "r": r})
    total_abs_r = sum(abs(x["r"]) for x in sources_with_r if x["r"] is not None)

    out: List[SourceContribution] = []
    for x in sources_with_r:
        data = x["data"]
        r = x["r"]
        contribution = (abs(r) / total_abs_r
                          if r is not None and total_abs_r > 0 else 0.0)
        # Score-segmented stats
        winners = [s for s, p in zip(data["scores"], data["pnls"]) if p > 0]
        losers = [s for s, p in zip(data["scores"], data["pnls"]) if p < 0]
        sc = SourceContribution(
            source=x["name"],
            sample_size=len(data["scores"]),
            contribution=round(contribution, 3),
            mean_score_winners=_mean(winners),
            mean_score_losers=_mean(losers),
            correlation_with_pnl=r,
            favorable_hit_rate=_hit_rate(data["favorable_pnls"]),
            unfavorable_hit_rate=_hit_rate(data["unfavorable_pnls"]),
            avg_pnl_when_favorable=_mean(data["favorable_pnls"]),
            avg_pnl_when_unfavorable=_mean(data["unfavorable_pnls"]),
            insight="",
        )
        sc.insight = _insight_for(sc)
        out.append(sc)

    # Rank by contribution descending so the dashboard reads top-down.
    out.sort(key=lambda c: -c.contribution)

    return ContributionReport(
        closed_trades=n_closed, sources=out, min_trades=min_trades,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
