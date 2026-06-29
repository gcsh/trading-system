"""Stage-17 — Trade Journal Intelligence.

Retail bots recall *trades*. Institutions accumulate *lessons*.

A trade is "Trade #181 looked like this and lost $42." Useful for forensics
but not actionable. A lesson is "Opening-range-breakout in high-VIX CPI
weeks: win rate 31%, expectancy -0.42R over 13 trades — reduce size by
50%." That's a rule the bot can act on going forward.

This module mines closed trades across multiple conditioning axes,
identifies (strategy × condition) buckets that significantly under- or
over-perform the global baseline, and emits ``Lesson`` records with:

  • pattern (human-readable)
  • condition_keys (machine-readable for engine consumption)
  • sample size, win rate, expectancy
  • recommended size_multiplier (size×0.5, size×1.5, abstain, unchanged)
  • confidence in the lesson (Wilson-bounded; small samples shrink)

Conditioning axes considered:
  • strategy
  • regime (trend × vol × gamma)
  • event proximity (earnings within 3d / 7d, FOMC / CPI windows)
  • day-of-week
  • IV-rank band (low / mid / high)
  • cross-asset risk (risk_on / off / neutral)

Pure compute over `decision_log` + `trades`. No DB writes. Lessons are
returned on demand by the endpoint + the engine consults them at decision
time (when ``ai.use_journal_lessons`` is enabled — default OFF until
trust is built).
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# Minimum sample size before a lesson can be published. Wilson bound
# already shrinks small samples but we also enforce a hard floor so the
# UI doesn't surface noise.
_MIN_SAMPLES = 8

# How far the bucket's win rate has to deviate from the global baseline
# before we call it a lesson. 0.10 = ±10 percentage points.
_DELTA_THRESHOLD = 0.10


@dataclass
class Lesson:
    pattern: str                          # human-readable rule
    condition_keys: Dict[str, Any]        # machine-readable
    sample_size: int
    wins: int
    losses: int
    win_rate: float
    baseline_win_rate: float              # global baseline for comparison
    expectancy: float                     # mean pnl per trade
    expectancy_r: Optional[float]         # in units of avg loss (Rs)
    avg_win: float
    avg_loss: float
    profit_factor: Optional[float]
    delta_pp: float                       # win_rate - baseline (pp)
    confidence_bound_lo: float            # Wilson lower bound
    confidence_bound_hi: float
    suggested_action: str                 # "abstain" | "reduce_size_50" |
                                           # "reduce_size_25" | "unchanged" |
                                           # "increase_size_25" | "increase_size_50"
    size_multiplier: float
    severity: str                         # "info" | "warn" | "alert"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JournalReport:
    lessons: List[Lesson] = field(default_factory=list)
    baseline_win_rate: Optional[float] = None
    baseline_expectancy: Optional[float] = None
    total_closed_trades: int = 0
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lessons": [l.to_dict() for l in self.lessons],
            "baseline_win_rate": self.baseline_win_rate,
            "baseline_expectancy": self.baseline_expectancy,
            "total_closed_trades": self.total_closed_trades,
            "generated_at": self.generated_at,
        }


# ── Wilson interval ─────────────────────────────────────────────────────


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson confidence interval for a binomial proportion. Returns
    (lower, upper) bounds; both in [0, 1]."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lo = (centre - margin) / denom
    hi = (centre + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


# ── corpus loader ───────────────────────────────────────────────────────


def _load_closed_trades(limit: int = 5000) -> List[Dict[str, Any]]:
    """Pull closed trades + parse the detail_json blob so we can read the
    full conditioning context (regime, features, event_risk) without a
    second round-trip per trade.

    P1.2 — excludes the historical_replay synthetic corpus + closed_by_reset
    rows so the journal lessons (which feed live decisions) aren't biased
    by backfill outcomes."""
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
                detail: Dict[str, Any] = {}
                if r.detail_json:
                    try:
                        detail = json.loads(r.detail_json) or {}
                    except Exception:
                        detail = {}
                # The DecisionLog row carries regime as canonical fields too.
                # Prefer the DecisionLog values when present, fall back to
                # what we parsed from detail.analytics.regime.
                d_log = session.execute(
                    select(DecisionLog)
                    .where(DecisionLog.trade_id == r.id)
                    .limit(1)
                ).scalar_one_or_none()
                analytics = detail.get("analytics") or {}
                regime = analytics.get("regime") or {}
                features = analytics.get("features") or {}
                out.append({
                    "trade_id": r.id,
                    "timestamp": r.timestamp,
                    "ticker": r.ticker,
                    "action": r.action,
                    "strategy": r.strategy or (d_log.strategy if d_log else ""),
                    "pnl": float(r.pnl or 0.0),
                    "regime_trend": (regime.get("trend")
                                       or (d_log.regime_trend if d_log else "unknown")),
                    "regime_volatility": (regime.get("volatility")
                                            or (d_log.regime_volatility if d_log else "normal")),
                    "regime_gamma": (regime.get("gamma")
                                       or (d_log.regime_gamma if d_log else "unknown")),
                    "earnings_days": features.get("earnings_days"),
                    "iv_rank": features.get("iv_rank"),
                    "cross_asset_equities": (detail.get("cross_asset") or {}).get("equities"),
                    "vix": features.get("vix"),
                })
    except Exception:
        logger.debug("journal._load_closed_trades failed", exc_info=True)
    return out


# ── bucket builder ──────────────────────────────────────────────────────


def _bucket_keys(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    """For one closed trade, emit every (axis, value) bucket it belongs to.

    The journal mines lessons by *crossing* a primary axis (strategy) with
    a secondary axis (regime / event / day / vol-band). Returns a list of
    dicts so a single trade can contribute to multiple lessons.
    """
    strat = t.get("strategy") or "unknown"
    keys: List[Dict[str, Any]] = []

    # strategy × regime_trend
    keys.append({
        "axis": "strategy_x_regime",
        "strategy": strat, "regime": t["regime_trend"],
    })
    # strategy × volatility
    keys.append({
        "axis": "strategy_x_volatility",
        "strategy": strat, "volatility": t["regime_volatility"],
    })
    # strategy × gamma
    keys.append({
        "axis": "strategy_x_gamma",
        "strategy": strat, "gamma": t["regime_gamma"],
    })
    # strategy × earnings proximity
    ed = t.get("earnings_days")
    if ed is not None:
        try:
            ed = float(ed)
            band = ("immediate" if ed <= 1 else "near" if ed <= 7 else "far")
            keys.append({
                "axis": "strategy_x_earnings",
                "strategy": strat, "earnings_band": band,
            })
        except Exception:
            pass
    # strategy × IV band
    iv = t.get("iv_rank")
    if iv is not None:
        try:
            iv = float(iv)
            band = ("low" if iv < 30 else "mid" if iv < 70 else "high")
            keys.append({
                "axis": "strategy_x_iv",
                "strategy": strat, "iv_band": band,
            })
        except Exception:
            pass
    # strategy × cross-asset risk
    eq = t.get("cross_asset_equities")
    if eq:
        keys.append({
            "axis": "strategy_x_cross_asset",
            "strategy": strat, "equities": eq,
        })
    # strategy × VIX band (when present)
    vix = t.get("vix")
    if vix is not None:
        try:
            v = float(vix)
            band = ("low" if v < 16 else "mid" if v < 22 else "high")
            keys.append({
                "axis": "strategy_x_vix",
                "strategy": strat, "vix_band": band,
            })
        except Exception:
            pass
    # strategy × day-of-week
    ts = t.get("timestamp")
    if ts:
        try:
            dow = ts.strftime("%A")
            keys.append({
                "axis": "strategy_x_dow",
                "strategy": strat, "day": dow,
            })
        except Exception:
            pass
    return keys


def _pattern_text(condition: Dict[str, Any]) -> str:
    """Render a condition dict as a one-liner suitable for the UI."""
    strat = condition.get("strategy", "?")
    axis = condition.get("axis", "")
    if axis == "strategy_x_regime":
        return f"{strat} in {condition['regime']} regime"
    if axis == "strategy_x_volatility":
        return f"{strat} when volatility is {condition['volatility']}"
    if axis == "strategy_x_gamma":
        return f"{strat} when dealer gamma is {condition['gamma']}"
    if axis == "strategy_x_earnings":
        return f"{strat} with earnings {condition['earnings_band']}"
    if axis == "strategy_x_iv":
        return f"{strat} when IV rank is {condition['iv_band']}"
    if axis == "strategy_x_cross_asset":
        return f"{strat} when cross-asset is {condition['equities']}"
    if axis == "strategy_x_vix":
        return f"{strat} when VIX is {condition['vix_band']}"
    if axis == "strategy_x_dow":
        return f"{strat} on {condition['day']}s"
    return str(condition)


# ── lesson synthesis ────────────────────────────────────────────────────


def _action_for(delta_pp: float, expectancy: float, baseline: float
                  ) -> Tuple[str, float, str]:
    """Map (Δ vs baseline, expectancy) onto an action + size multiplier
    + severity. Smooth bands — no step functions."""
    if expectancy < 0 and delta_pp <= -0.20:
        return "abstain", 0.0, "alert"
    if expectancy < 0 and delta_pp <= -0.10:
        return "reduce_size_50", 0.5, "warn"
    if delta_pp <= -0.05:
        return "reduce_size_25", 0.75, "warn"
    if expectancy > 0 and delta_pp >= 0.20:
        return "increase_size_50", 1.5, "info"
    if expectancy > 0 and delta_pp >= 0.10:
        return "increase_size_25", 1.25, "info"
    return "unchanged", 1.0, "info"


def build_lessons(*, limit: int = 5000,
                     min_samples: int = _MIN_SAMPLES,
                     delta_threshold: float = _DELTA_THRESHOLD,
                     ) -> JournalReport:
    """Mine the closed-trade corpus for actionable lessons.

    A *lesson* is a (strategy × condition) bucket whose win-rate deviates
    from the global baseline by at least ``delta_threshold`` and whose
    sample size is at least ``min_samples``. Lessons are returned sorted
    by impact (signed delta × sample size).
    """
    from datetime import datetime, timezone
    trades = _load_closed_trades(limit=limit)
    if not trades:
        return JournalReport(generated_at=datetime.now(timezone.utc).isoformat())

    # Global baseline
    pnls = [t["pnl"] for t in trades]
    wins_global = sum(1 for p in pnls if p > 0)
    n_global = len(pnls)
    baseline_wr = wins_global / n_global
    baseline_exp = sum(pnls) / n_global

    # Bucket trades by every applicable (axis, condition) key.
    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"trades": [], "condition": None}
    )
    for t in trades:
        for cond in _bucket_keys(t):
            key = json.dumps(cond, sort_keys=True, default=str)
            buckets[key]["trades"].append(t)
            buckets[key]["condition"] = cond

    lessons: List[Lesson] = []
    for key, b in buckets.items():
        bk_trades = b["trades"]
        n = len(bk_trades)
        if n < min_samples:
            continue
        bk_pnls = [t["pnl"] for t in bk_trades]
        wins = sum(1 for p in bk_pnls if p > 0)
        losses = sum(1 for p in bk_pnls if p < 0)
        wr = wins / n
        delta = wr - baseline_wr
        if abs(delta) < delta_threshold:
            continue
        expectancy = sum(bk_pnls) / n
        avg_win = (sum(p for p in bk_pnls if p > 0) / wins) if wins else 0.0
        avg_loss = (sum(p for p in bk_pnls if p < 0) / losses) if losses else 0.0
        pf = (abs(sum(p for p in bk_pnls if p > 0))
                / abs(sum(p for p in bk_pnls if p < 0))
                if losses and any(p < 0 for p in bk_pnls) else None)
        lo, hi = _wilson_interval(wins, n)
        action, size_mult, severity = _action_for(delta, expectancy, baseline_wr)
        # Expectancy expressed in Rs (units of avg loss) for the
        # institution-style "expectancy = -0.42R" reading.
        exp_r = (expectancy / abs(avg_loss)) if avg_loss else None
        lessons.append(Lesson(
            pattern=_pattern_text(b["condition"]),
            condition_keys=b["condition"],
            sample_size=n, wins=wins, losses=losses,
            win_rate=round(wr, 3),
            baseline_win_rate=round(baseline_wr, 3),
            expectancy=round(expectancy, 2),
            expectancy_r=round(exp_r, 3) if exp_r is not None else None,
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            profit_factor=round(pf, 2) if pf is not None else None,
            delta_pp=round(delta, 3),
            confidence_bound_lo=round(lo, 3),
            confidence_bound_hi=round(hi, 3),
            suggested_action=action,
            size_multiplier=size_mult,
            severity=severity,
        ))

    # Sort by impact (signed delta × sample size) so the most actionable
    # lessons appear first.
    lessons.sort(key=lambda l: -abs(l.delta_pp) * l.sample_size)

    return JournalReport(
        lessons=lessons,
        baseline_win_rate=round(baseline_wr, 3),
        baseline_expectancy=round(baseline_exp, 2),
        total_closed_trades=n_global,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── engine-side lookup ─────────────────────────────────────────────────


def similar_trades(*, ticker: Optional[str] = None,
                       regime_trend: Optional[str] = None,
                       regime_volatility: Optional[str] = None,
                       strategy: Optional[str] = None,
                       k: int = 5,
                       limit: int = 2000) -> List[Dict[str, Any]]:
    """Return the K most recent closed trades that *match* the supplied
    conditioning filters (Item #1 — memory-rich agent context).

    Match strength is ranked highest-to-lowest, then recency. A "match"
    requires at least one of:
      • same ticker,
      • same regime trend AND same volatility,
      • same strategy.

    Returns a slim dict per trade for direct embedding in the agent
    context: timestamp, ticker, strategy, action, pnl, regime, was_winner.
    """
    rows = _load_closed_trades(limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        match_score = 0
        # Same-ticker is the highest-value match for an agent considering
        # a specific name.
        if ticker and r.get("ticker") == ticker.upper():
            match_score += 3
        if regime_trend and r.get("regime_trend") == regime_trend:
            match_score += 2
        if regime_volatility and r.get("regime_volatility") == regime_volatility:
            match_score += 1
        if strategy and r.get("strategy") == strategy:
            match_score += 2
        if match_score == 0:
            continue
        pnl = float(r.get("pnl") or 0.0)
        out.append({
            "trade_id": r.get("trade_id"),
            "timestamp": r.get("timestamp").isoformat() if r.get("timestamp")
                else None,
            "ticker": r.get("ticker"),
            "strategy": r.get("strategy"),
            "action": r.get("action"),
            "pnl": round(pnl, 2),
            "was_winner": pnl > 0,
            "regime_trend": r.get("regime_trend"),
            "regime_volatility": r.get("regime_volatility"),
            "match_score": match_score,
        })
    # Highest match_score first, then most recent.
    out.sort(key=lambda d: (-d["match_score"],
                                 d["timestamp"] or "", ), reverse=False)
    out.sort(key=lambda d: (-d["match_score"], d["timestamp"] or ""),
                  reverse=False)
    return out[:k]


def applicable_lessons(*, strategy: str, regime_trend: str,
                          volatility: str, gamma: str,
                          earnings_days: Optional[float] = None,
                          iv_rank: Optional[float] = None,
                          cross_asset_equities: Optional[str] = None,
                          vix: Optional[float] = None,
                          day_of_week: Optional[str] = None,
                          iv_regime: Optional[Dict[str, Any]] = None,
                          yield_curve_inverted: Optional[bool] = None,
                          ) -> List[Lesson]:
    """Return lessons whose condition matches a live trade context. Used
    by the engine when ``ai.use_journal_lessons`` is enabled — it picks
    the *most penalizing* size_multiplier across matches as the size
    adjustment for the trade.

    Returns the union of:
      - **organic** lessons mined from the closed-trade corpus
      - **curated** lessons (P2.2 — hand-coded institutional guardrails)

    Curated lessons fire regardless of corpus size — they don't depend
    on having mined enough trades to support a finding.
    """
    report = build_lessons()
    matches: List[Lesson] = []
    for l in report.lessons:
        c = l.condition_keys
        if c.get("strategy") != strategy:
            continue
        axis = c.get("axis")
        if axis == "strategy_x_regime" and c.get("regime") == regime_trend:
            matches.append(l)
        elif axis == "strategy_x_volatility" and c.get("volatility") == volatility:
            matches.append(l)
        elif axis == "strategy_x_gamma" and c.get("gamma") == gamma:
            matches.append(l)
        elif axis == "strategy_x_earnings" and earnings_days is not None:
            band = ("immediate" if float(earnings_days) <= 1
                     else "near" if float(earnings_days) <= 7 else "far")
            if c.get("earnings_band") == band:
                matches.append(l)
        elif axis == "strategy_x_iv" and iv_rank is not None:
            band = ("low" if float(iv_rank) < 30
                     else "mid" if float(iv_rank) < 70 else "high")
            if c.get("iv_band") == band:
                matches.append(l)
        elif axis == "strategy_x_cross_asset" \
                and c.get("equities") == cross_asset_equities:
            matches.append(l)
        elif axis == "strategy_x_vix" and vix is not None:
            band = ("low" if float(vix) < 16
                     else "mid" if float(vix) < 22 else "high")
            if c.get("vix_band") == band:
                matches.append(l)
        elif axis == "strategy_x_dow" and c.get("day") == day_of_week:
            matches.append(l)

    # Curated lessons (P2.2). Merged into the same list so any caller —
    # ``trade_size_multiplier``, agent_context, the engine's risk gate —
    # sees both sources without code changes.
    try:
        from backend.bot.journal.curated import applicable_curated_lessons
        matches.extend(applicable_curated_lessons(
            strategy=strategy,
            regime_trend=regime_trend,
            volatility=volatility,
            gamma=gamma,
            earnings_days=earnings_days,
            iv_rank=iv_rank,
            cross_asset_equities=cross_asset_equities,
            vix=vix,
            day_of_week=day_of_week,
            iv_regime=iv_regime,
            yield_curve_inverted=yield_curve_inverted,
        ))
    except Exception:
        logger.debug("curated lessons merge failed", exc_info=True)

    return matches


def trade_size_multiplier(*, strategy: str, **context) -> Tuple[float, List[Lesson]]:
    """Convenience: compute the size multiplier the journal recommends
    for a live trade. Returns the *most penalizing* multiplier across all
    applicable lessons (you'd rather under-size than over-size when in
    doubt) plus the matched lessons for transparency."""
    matches = applicable_lessons(strategy=strategy, **context)
    if not matches:
        return 1.0, []
    multiplier = min(l.size_multiplier for l in matches)
    return multiplier, matches
