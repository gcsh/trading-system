"""MITS Phase 18.A — Learned Hypothesis Attribution.

Answers the question "which agents + which axes + which strategies
actually predict realized P&L?" against the live trade ledger.

Reads two tables:

  * ``decision_provenance`` — agent_outputs_json, consensus_json,
    regime_vector_json, strategy_matrix_json
  * ``trades`` — pnl, status, strategy, signal_source, price, quantity

Joins each ``decision_provenance.trade_id`` to its Trade. A Trade is
considered "closed" when ``Trade.status == 'closed'`` AND ``Trade.pnl``
is not NULL. ``status='closed_by_reset'`` and rows with
``signal_source='historical_replay'`` are excluded — they are
synthetic / non-decision-driven and would poison the calibration.

Realized return % is computed as ``pnl / (price * quantity) * 100``
when the notional is positive; falls back to raw ``pnl`` rescaled by
100 so the bin axis stays comparable across instruments. This is the
SAME formula the Phase 16.C ``/decision/scorecard`` route uses
(see backend/api/routes/decision.py:202-207), so cohort numbers tie
out exactly between the two surfaces.

Honesty guardrails:

  * ``min_n`` (default 30 for agent + axis, 10 for strategy): if
    ``n_closed`` falls below the floor, every metric is set to None
    and the dataclass carries ``notes="insufficient_sample_size_n_lt_<N>"``.
    The operator sees "not enough data" instead of a misleading 47%.
  * ``stale_calibration``: if the oldest sample is older than
    ``stale_after_days`` (default 30), the dataclass adds the
    ``stale_calibration`` note even when n_closed clears the floor —
    regime shifts invalidate old wins.
  * ``hit_rate_wilson_(lower|upper)``: Wilson 95% CI on the
    binomial hit-rate proportion. The operator sees the band, not
    just the point estimate.
  * NO synthetic data is generated or backfilled here. Rows where
    ``Trade.pnl IS NULL`` are skipped silently — they simply don't
    contribute to the aggregate.

The module is pure aggregation + serialization. It does NOT write to
the database. ``backend.bot.learning.attribution_writer`` (see below)
is the persistence layer; the scheduler / route triggers call into
``compute_attribution_report`` and then hand the result to the writer.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.trade import Trade


logger = logging.getLogger(__name__)


# ── Public knobs (config-driven; no magic numbers in the math) ────────


DEFAULT_WINDOW_DAYS = 90
DEFAULT_MIN_N_AGENT = 30
DEFAULT_MIN_N_AXIS = 30
DEFAULT_MIN_N_STRATEGY = 10
DEFAULT_CONFIDENCE_BINS = 5
DEFAULT_STALE_AFTER_DAYS = 30
# A high/low axis bucket fires when the axis score (0..100) sits above
# or below these thresholds. 70 / 30 is the standard "strong reading"
# convention used by `_compute_confidence_breakdown` consumers.
DEFAULT_HIGH_AXIS_THRESHOLD = 70.0
DEFAULT_LOW_AXIS_THRESHOLD = 30.0

# The 8 council agents (verified against
# backend/bot/agents/__init__.py:1669-1680, AGENT_FUNCS). Hard-coded so
# the scorecard always lists EVERY agent — even when one returned zero
# closed trades — instead of silently dropping it.
KNOWN_AGENTS: Tuple[str, ...] = (
    "market",
    "microstructure",
    "macro",
    "portfolio_risk",
    "mechanical_trend",
    "thesis_health",
    "simulator",
    "devils_advocate",
)

# The 6 ConfidenceBreakdown axes (verified against
# backend/bot/agents/__init__.py:267-286, ConfidenceBreakdown dataclass).
KNOWN_AXES: Tuple[str, ...] = (
    "market_structure",
    "technical",
    "options",
    "historical_analog",
    "simulator",
    "macro",
)


# ── Wilson 95% CI on a binomial proportion ────────────────────────────


def _wilson_interval(
    wins: int, n: int, z: float = 1.96,
) -> Tuple[Optional[float], Optional[float]]:
    """Wilson 95% confidence interval for a binomial proportion.

    Mirrors the helper at
    ``backend.bot.corpus.knowledge_aggregator._wilson_interval`` so the
    bands here match the cohort CI the operator already sees on the
    knowledge surfaces.
    """
    if n <= 0:
        return None, None
    p_hat = wins / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    margin = (
        z * math.sqrt(
            (p_hat * (1.0 - p_hat) / n) + (z * z / (4.0 * n * n))
        )
    ) / denom
    return (
        max(0.0, center - margin),
        min(1.0, center + margin),
    )


# ── Spearman rank correlation (no scipy dep) ──────────────────────────


def _ranks(values: Iterable[float]) -> List[float]:
    """Average-rank assignment for tied values, matching scipy's
    ``scipy.stats.rankdata(method='average')``. The Spearman ρ uses
    average ranks so ties don't bias the correlation toward 0."""
    vals = list(values)
    indexed = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[indexed[j + 1]] == vals[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0   # 1-indexed average
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    """Spearman rank correlation. Returns None when the input is
    too short (<3 pairs) or when either side is constant (ρ undefined)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx = _ranks(xs)
    ry = _ranks(ys)
    mx = statistics.fmean(rx)
    my = statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx == 0 or deny == 0:
        return None
    return num / (denx * deny)


# ── Brier + Expected Calibration Error ────────────────────────────────


def brier_score(probs: List[float], outcomes: List[int]) -> Optional[float]:
    """Brier score on a binary classifier — lower is better.

    ``probs[i]`` ∈ [0, 1] is the predicted P(win); ``outcomes[i]`` ∈ {0, 1}
    is the realized win flag. The Brier score is the MSE between the
    two; the perfect calibrator scores 0.0, the always-coin-flip
    baseline scores 0.25.
    """
    if not probs or len(probs) != len(outcomes):
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def ece(
    probs: List[float], outcomes: List[int], bins: int = DEFAULT_CONFIDENCE_BINS,
) -> Optional[float]:
    """Expected Calibration Error with equal-width probability bins.

    Bucket predictions into ``bins`` evenly spaced bins on [0, 1]. For
    each bin, compute |mean_predicted_p - realized_win_rate| and weight
    by the share of samples in that bin. Sum is ECE. A perfectly
    calibrated model has ECE = 0; the worst case is 1.0.
    """
    if not probs or len(probs) != len(outcomes) or bins <= 0:
        return None
    bin_pairs: List[List[Tuple[float, int]]] = [[] for _ in range(bins)]
    for p, o in zip(probs, outcomes):
        # Clip to [0, 1] in case of float drift; bin = floor(p * bins).
        pp = max(0.0, min(1.0, float(p)))
        idx = min(bins - 1, int(pp * bins))
        bin_pairs[idx].append((pp, int(o)))
    total = len(probs)
    err = 0.0
    for pairs in bin_pairs:
        if not pairs:
            continue
        mean_p = sum(p for p, _ in pairs) / len(pairs)
        mean_o = sum(o for _, o in pairs) / len(pairs)
        err += (len(pairs) / total) * abs(mean_p - mean_o)
    return err


# ── Dataclasses (round-trippable) ─────────────────────────────────────


@dataclass
class AgentCalibration:
    agent: str
    n_closed: int
    hit_rate: Optional[float] = None
    hit_rate_wilson_lower: Optional[float] = None
    hit_rate_wilson_upper: Optional[float] = None
    mean_pnl_pct: Optional[float] = None
    median_pnl_pct: Optional[float] = None
    confidence_mean: Optional[float] = None
    brier_score: Optional[float] = None
    ece: Optional[float] = None
    by_stance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_confidence_bin: List[Dict[str, Any]] = field(default_factory=list)
    sample_age_days: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AxisCalibration:
    axis: str
    n_closed: int
    spearman_corr: Optional[float] = None
    high_axis_pnl_mean: Optional[float] = None
    low_axis_pnl_mean: Optional[float] = None
    high_axis_n: int = 0
    low_axis_n: int = 0
    discrimination: Optional[float] = None
    sample_age_days: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyCalibration:
    strategy_name: str
    n_closed: int
    hit_rate: Optional[float] = None
    hit_rate_wilson_lower: Optional[float] = None
    hit_rate_wilson_upper: Optional[float] = None
    mean_pnl_pct: Optional[float] = None
    median_pnl_pct: Optional[float] = None
    by_regime: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    sample_age_days: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    # 18-FU Gap 2 — provenance breakdown: how many rows in this bucket
    # came from strategy_matrix_json (entry-side template) vs the
    # Trade.strategy fallback vs the UNATTRIBUTED sentinel. Operators
    # use this to spot calibrations dominated by fallback rows.
    provenance_breakdown: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Trade closure + return helpers ────────────────────────────────────


def _is_closed_trade(trade: Trade) -> bool:
    """A trade is "closed" for attribution if status == 'closed' AND
    pnl is populated. ``closed_by_reset`` is the explicit synthetic-
    cleanup status and MUST be excluded — it carries no decision
    signal."""
    if trade is None:
        return False
    if trade.pnl is None:
        return False
    status = (trade.status or "").lower()
    if status == "closed_by_reset":
        return False
    return status == "closed"


def _is_eligible_signal_source(trade: Trade) -> bool:
    """Exclude synthetic-replay decisions from the calibration. They
    were never actually traded; counting them would conflate live
    behavior with backfilled history."""
    src = (getattr(trade, "signal_source", "") or "").lower()
    return src != "historical_replay"


def _realized_pct(trade: Trade) -> Optional[float]:
    """Realized return % using the SAME formula as
    ``/decision/scorecard`` (decision.py:204-207). Returns None when
    pnl is NULL or notional can't be computed."""
    try:
        pnl = float(trade.pnl) if trade.pnl is not None else None
        if pnl is None:
            return None
        price = float(trade.price or 0.0)
        qty = float(trade.quantity or 0.0)
        notional = price * qty
        if notional > 0:
            return (pnl / notional) * 100.0
        # Fallback: raw pnl rescaled by 100. Same fallback the 16.C
        # scorecard uses so cohort numbers tie out across surfaces.
        return pnl
    except (TypeError, ValueError):
        return None


# ── JSON decode helpers (robust to malformed rows) ────────────────────


def _decode(blob: Optional[str]) -> Optional[Dict[str, Any]]:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return None


def _stance_norm(raw: Any) -> str:
    """Lowercase + strip the stance string. Empty / non-string → ''."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _confidence_norm(raw: Any) -> Optional[float]:
    """Normalize confidence into [0, 1].

    AgentOutput.confidence is int 0..100 (contracts_v2.py:299);
    AgentVote.confidence is float 0..1 (agents/__init__.py:160). The
    persisted ``agent_outputs_json`` carries the AgentOutput int
    projection. We accept both and clip to [0, 1].
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 1.0001:        # surely 0..100 form
        v = v / 100.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# ── Closed-decision row builder ───────────────────────────────────────


@dataclass
class _ClosedDecision:
    """One closed-Trade × decision_provenance pair, parsed into the
    shapes the aggregators need. Holding this together as one struct
    lets the three calibrators share the same query + decode pass.

    18-FU Gap 2 — ``strategy_name`` is now the 15.C template that won
    at ENTRY time (read off ``DecisionProvenance.strategy_matrix_json``),
    NOT the close-side ``Trade.strategy`` legacy field. ``strategy_provenance``
    records WHERE the name came from so the operator can spot fallbacks
    in the calibration UI:
      * "strategy_matrix_top_candidate" — winner template from the
        matrix (the load-bearing case; replaces the broken close-side
        read that surfaced every trade as ``exit_manager``).
      * "strategy_matrix_top_strategy_field" — the matrix's pre-baked
        ``top_strategy.strategy_name`` (used when candidates[] is
        absent but top_strategy is set; the 15.C builder sets both
        but tolerate sparse rows).
      * "fallback_trade_strategy" — strategy_matrix_json is missing
        BUT Trade.strategy is populated with a non-exit-side value
        (rare; surfaces legacy rows that pre-date 15.C).
      * "unattributed_no_strategy_matrix" — strategy_matrix_json is
        absent AND Trade.strategy is ``exit_manager`` or empty.
        Closed-Trade rows where the entry-side decision never wrote
        a strategy matrix (e.g. 18.B counterfactual prov rows that
        do not link back to the entry-side prov row). The operator
        sees ``UNATTRIBUTED`` clearly.
    """
    trade_id: int
    pnl_pct: float
    pnl_raw: float
    win: int                  # 1 if pnl_pct > 0 else 0
    decision_timestamp: datetime
    agent_outputs: List[Dict[str, Any]]
    consensus: Dict[str, Any]
    confidence_breakdown: Dict[str, Any]
    strategy_name: Optional[str]
    regime_trend: Optional[str]
    # 18-FU Gap 2 — provenance of the strategy_name field above.
    # Always populated; see dataclass docstring for the four values.
    strategy_provenance: str = "fallback_trade_strategy"


# 18-FU Gap 2 — sentinel for closed Trades with no entry-side strategy
# matrix attribution. The strategy calibrator emits one bucket under this
# name so the operator can see how many closed-trade rows are missing
# their 15.C template stamp.
UNATTRIBUTED_STRATEGY = "UNATTRIBUTED"

# Trade.strategy values that signal a close-side / exit-manager row.
# We treat these as no-info for strategy attribution — they are the
# whole reason 18-FU Gap 2 exists.
_CLOSE_SIDE_STRATEGY_VALUES = {"exit_manager", "", None}


def _extract_strategy_from_matrix(
    matrix: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Pull the winning template's ``strategy_name`` off a parsed
    ``strategy_matrix_json`` blob.

    15.C ranks ``candidates[]`` by ``final_score`` descending before
    persistence (strategy_matrix.py:415-416), so ``candidates[0]`` IS
    the winner. The blob also carries a pre-baked
    ``top_strategy.strategy_name`` mirror; we prefer ``candidates[0]``
    as the load-bearing source and fall back to ``top_strategy`` when
    ``candidates[]`` is absent.

    Returns the strategy_name string when successfully extracted,
    or None when the blob has neither field populated (will surface
    upstream as UNATTRIBUTED with the appropriate provenance tag).
    """
    if not isinstance(matrix, dict):
        return None
    cands = matrix.get("candidates")
    if isinstance(cands, list) and cands:
        first = cands[0]
        if isinstance(first, dict):
            name = first.get("strategy_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    # candidates[] empty/missing — try top_strategy mirror.
    top = matrix.get("top_strategy")
    if isinstance(top, dict):
        name = top.get("strategy_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _resolve_strategy_attribution(
    *,
    strategy_matrix: Optional[Dict[str, Any]],
    trade_strategy: Optional[str],
) -> Tuple[str, str]:
    """Map (strategy_matrix_json, Trade.strategy) → (name, provenance).

    Encodes the 18-FU Gap 2 attribution rule. See the ``_ClosedDecision``
    docstring for the four provenance values returned. Always returns
    a non-empty ``name`` so downstream groupers never crash on None.

    Provenance semantics:
      * Strategy matrix wins when populated — that's the 15.C entry-side
        template, the actual hypothesis that fired.
      * Trade.strategy is consulted only as a fallback when the matrix
        is missing AND Trade.strategy carries a non-exit-side value.
      * Closed-side / empty Trade.strategy with no matrix → UNATTRIBUTED.
    """
    name = _extract_strategy_from_matrix(strategy_matrix)
    if name is not None:
        # We extracted from the matrix; distinguish which field carried
        # it so the operator can audit sparse 15.C rows.
        if isinstance(strategy_matrix, dict):
            cands = strategy_matrix.get("candidates")
            if isinstance(cands, list) and cands and isinstance(
                cands[0], dict,
            ) and cands[0].get("strategy_name"):
                return name, "strategy_matrix_top_candidate"
        return name, "strategy_matrix_top_strategy_field"

    # Matrix is missing or both fields blank. Inspect Trade.strategy.
    norm = (trade_strategy or "").strip().lower()
    if norm and norm not in {"exit_manager", "_unknown"}:
        return trade_strategy.strip(), "fallback_trade_strategy"

    return UNATTRIBUTED_STRATEGY, "unattributed_no_strategy_matrix"


def _iter_closed_decisions(
    window_days: int,
    *,
    include_synthetic: bool = False,
) -> List[_ClosedDecision]:
    """Walk decision_provenance + trades and emit one row per closed
    decision in the window. Uses ``decision_timestamp`` so the window
    is decision-anchored (not close-anchored); a trade opened 100 days
    ago that closed last week is correctly excluded when window=90.

    Excludes:
      • Trades with status != 'closed' or pnl IS NULL
      • status == 'closed_by_reset' (synthetic cleanup)
      • signal_source == 'historical_replay' (legacy replay corpus tag
        used by Phase 0 — kept for back-compat)
      • prov.source_kind == 'synthetic_backfill' (18-FU Gap 4 — the
        new flag-gated learning backfill) UNLESS
        ``include_synthetic=True``. Synthetic rows MUST never bleed
        into the default attribution read; the opt-in is the safety
        contract.
    """
    if window_days <= 0:
        return []
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    out: List[_ClosedDecision] = []
    with session_scope() as s:
        q = (
            select(DecisionProvenance)
            .where(DecisionProvenance.trade_id.is_not(None))
            .where(DecisionProvenance.decision_timestamp >= cutoff)
        )
        if not include_synthetic:
            # 18-FU Gap 4 — exclude synthetic-backfill rows from the
            # default attribution read. NULL source_kind is treated
            # as live (back-compat with pre-migration rows).
            q = q.where(
                (DecisionProvenance.source_kind != "synthetic_backfill")
                | (DecisionProvenance.source_kind.is_(None))
            )
        rows = s.execute(
            q.order_by(DecisionProvenance.decision_timestamp.desc())
        ).scalars().all()
        # Bulk-load the trades by ID to avoid an N+1 query.
        trade_ids = [r.trade_id for r in rows if r.trade_id is not None]
        trade_map: Dict[int, Trade] = {}
        if trade_ids:
            for t in s.execute(
                select(Trade).where(Trade.id.in_(trade_ids))
            ).scalars().all():
                trade_map[int(t.id)] = t
        for row in rows:
            trade = trade_map.get(int(row.trade_id))
            if trade is None:
                continue
            if not _is_closed_trade(trade):
                continue
            if not _is_eligible_signal_source(trade):
                continue
            pct = _realized_pct(trade)
            if pct is None:
                continue
            consensus = _decode(row.consensus_json) or {}
            agent_outputs = _decode(row.agent_outputs_json) or []
            if not isinstance(agent_outputs, list):
                agent_outputs = []
            cb = consensus.get("confidence_breakdown") or {}
            if not isinstance(cb, dict):
                cb = {}
            regime_vector = _decode(row.regime_vector_json) or {}
            regime_trend = (
                (regime_vector.get("trend") or {}).get("value")
                if isinstance(regime_vector.get("trend"), dict)
                else regime_vector.get("trend")
            )
            # 18-FU Gap 2 — strategy attribution now reads the 15.C
            # entry-side template off ``strategy_matrix_json``. The
            # legacy ``Trade.strategy`` read returned ``exit_manager``
            # on every close-side row, collapsing the strategy axis.
            strategy_matrix_blob = _decode(row.strategy_matrix_json)
            strat_name, strat_prov = _resolve_strategy_attribution(
                strategy_matrix=strategy_matrix_blob,
                trade_strategy=trade.strategy,
            )
            out.append(_ClosedDecision(
                trade_id=int(row.trade_id),
                pnl_pct=float(pct),
                pnl_raw=float(trade.pnl or 0.0),
                win=1 if float(trade.pnl or 0.0) > 0 else 0,
                decision_timestamp=row.decision_timestamp or datetime.utcnow(),
                agent_outputs=agent_outputs,
                consensus=consensus,
                confidence_breakdown=cb,
                strategy_name=strat_name,
                regime_trend=(
                    str(regime_trend) if regime_trend is not None else None
                ),
                strategy_provenance=strat_prov,
            ))
    return out


def _sample_age_days(decisions: List[_ClosedDecision]) -> Optional[int]:
    """Days between the OLDEST decision in the sample and ``utcnow``.

    Used to flag stale calibrations — a 90-day window where every
    sample is 60+ days old means the regime has likely shifted out
    from under the read. Returns None when the sample is empty."""
    if not decisions:
        return None
    oldest = min(d.decision_timestamp for d in decisions)
    # Decision timestamps are stored naive UTC; compare to utcnow().
    delta = datetime.utcnow() - oldest
    return max(0, delta.days)


# ── Per-agent calibration ─────────────────────────────────────────────


def compute_agent_calibration(
    *, window_days: int = DEFAULT_WINDOW_DAYS,
    min_n: int = DEFAULT_MIN_N_AGENT,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    confidence_bins: int = DEFAULT_CONFIDENCE_BINS,
    decisions: Optional[List[_ClosedDecision]] = None,
    include_synthetic: bool = False,
) -> List[AgentCalibration]:
    """Per-agent calibration over closed decisions in the window.

    For each agent in ``KNOWN_AGENTS``:

      * Pull every agent_output projection across the closed decisions.
      * Convert the agent's stance + confidence into a predicted P(win):
        - stance == 'buy'  → p_win = confidence
        - stance == 'sell' → p_win = 1 - confidence  (a sell that
                                  realized P&L > 0 was wrong; we anchor
                                  every prediction against "did the
                                  position make money?")
        - stance == 'hold' / 'abstain' → excluded from Brier + ECE
          (no directional bet)
      * Hit rate: fraction where pnl_pct > 0 on rows with stance ∈
        {buy, sell}. Wilson 95% CI bracket included.
      * Confidence bins: 5 bins on [0, 1) + 1.0 special; reports per-bin
        n + win rate + mean predicted p.

    Below ``min_n`` closed contributions, ALL metrics are set to None
    and the dataclass carries ``notes=['insufficient_sample_size_n_lt_<min_n>']``.
    """
    if decisions is None:
        decisions = _iter_closed_decisions(
            window_days, include_synthetic=include_synthetic,
        )

    by_agent: Dict[str, List[Dict[str, Any]]] = {
        a: [] for a in KNOWN_AGENTS
    }
    for d in decisions:
        for output in d.agent_outputs:
            if not isinstance(output, dict):
                continue
            name = str(output.get("agent") or "")
            if name not in by_agent:
                continue
            stance = _stance_norm(output.get("stance"))
            conf = _confidence_norm(output.get("confidence"))
            if conf is None:
                continue
            by_agent[name].append({
                "stance": stance,
                "confidence": conf,
                "pnl_pct": d.pnl_pct,
                "win": d.win,
                "decision_ts": d.decision_timestamp,
            })

    out: List[AgentCalibration] = []
    for name in KNOWN_AGENTS:
        rows = by_agent[name]
        # The "directional vote" subset — only buy/sell count toward
        # calibration math. Hold/abstain are tracked separately as by_stance.
        directional = [r for r in rows if r["stance"] in ("buy", "sell")]
        n = len(directional)

        # Build by_stance summary always (so the UI can show "macro
        # abstained on 12 / 19 closed cycles" even when n_closed < min_n).
        by_stance: Dict[str, Dict[str, Any]] = {}
        for s_name in ("buy", "sell", "hold", "abstain"):
            subset = [r for r in rows if r["stance"] == s_name]
            if not subset:
                by_stance[s_name] = {"n": 0}
                continue
            pnls = [r["pnl_pct"] for r in subset]
            wins = sum(r["win"] for r in subset)
            by_stance[s_name] = {
                "n": len(subset),
                "hit_rate": round(wins / len(subset), 4),
                "mean_pnl_pct": round(statistics.fmean(pnls), 4),
            }

        sample_age = (
            max(
                0,
                (datetime.utcnow() - min(r["decision_ts"] for r in rows)).days,
            )
            if rows else None
        )

        notes: List[str] = []
        if n < min_n:
            notes.append(f"insufficient_sample_size_n_lt_{min_n}")
            out.append(AgentCalibration(
                agent=name,
                n_closed=n,
                by_stance=by_stance,
                sample_age_days=sample_age,
                notes=notes,
            ))
            continue
        if sample_age is not None and sample_age > stale_after_days:
            notes.append("stale_calibration")

        pnls = [r["pnl_pct"] for r in directional]
        wins = sum(r["win"] for r in directional)
        hit_rate = wins / n
        wlow, whi = _wilson_interval(wins, n)
        confs = [r["confidence"] for r in directional]

        probs: List[float] = []
        outcomes: List[int] = []
        for r in directional:
            if r["stance"] == "buy":
                probs.append(r["confidence"])
            else:    # sell
                probs.append(1.0 - r["confidence"])
            outcomes.append(r["win"])
        brier = brier_score(probs, outcomes)
        ece_val = ece(probs, outcomes, bins=confidence_bins)

        bins: List[Dict[str, Any]] = []
        # Equal-width predicted-probability bins on [0, 1].
        width = 1.0 / confidence_bins
        for i in range(confidence_bins):
            lo = i * width
            hi = (i + 1) * width
            in_bin = [
                (p, o) for p, o in zip(probs, outcomes)
                if (p >= lo and (p < hi or (i == confidence_bins - 1 and p <= 1.0)))
            ]
            if not in_bin:
                bins.append({
                    "bin": f"{round(lo, 2)}-{round(hi, 2)}",
                    "n": 0,
                    "mean_predicted_p": None,
                    "realized_win_rate": None,
                })
                continue
            mean_p = sum(p for p, _ in in_bin) / len(in_bin)
            win_rate = sum(o for _, o in in_bin) / len(in_bin)
            bins.append({
                "bin": f"{round(lo, 2)}-{round(hi, 2)}",
                "n": len(in_bin),
                "mean_predicted_p": round(mean_p, 4),
                "realized_win_rate": round(win_rate, 4),
            })

        out.append(AgentCalibration(
            agent=name,
            n_closed=n,
            hit_rate=round(hit_rate, 4),
            hit_rate_wilson_lower=(
                round(wlow, 4) if wlow is not None else None
            ),
            hit_rate_wilson_upper=(
                round(whi, 4) if whi is not None else None
            ),
            mean_pnl_pct=round(statistics.fmean(pnls), 4),
            median_pnl_pct=round(statistics.median(pnls), 4),
            confidence_mean=round(statistics.fmean(confs), 4),
            brier_score=(round(brier, 4) if brier is not None else None),
            ece=(round(ece_val, 4) if ece_val is not None else None),
            by_stance=by_stance,
            by_confidence_bin=bins,
            sample_age_days=sample_age,
            notes=notes,
        ))

    return out


# ── Per-axis calibration ──────────────────────────────────────────────


def _axis_score(cb: Dict[str, Any], axis: str) -> Optional[float]:
    """Pull the per-axis score off a confidence_breakdown blob.

    The 15.D ``ConfidenceBreakdown`` stores per-axis scores as floats
    in [0, 1] (mean of weighted votes). We rescale to [0, 100] so the
    high/low thresholds line up with the operator convention used
    everywhere else (the 16.C scorecard, the cockpit gauges, etc.).
    """
    if not isinstance(cb, dict):
        return None
    raw = cb.get(axis)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v < 0.0:
        return 0.0
    # 15.D stores [0, 1]; rescale to [0, 100] to match the operator
    # convention used by the cockpit gauges + the 16.C scorecard.
    if v <= 1.0:
        v = v * 100.0
    if v > 100.0:
        v = 100.0
    return v


def compute_axis_calibration(
    *, window_days: int = DEFAULT_WINDOW_DAYS,
    min_n: int = DEFAULT_MIN_N_AXIS,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    high_threshold: float = DEFAULT_HIGH_AXIS_THRESHOLD,
    low_threshold: float = DEFAULT_LOW_AXIS_THRESHOLD,
    decisions: Optional[List[_ClosedDecision]] = None,
    include_synthetic: bool = False,
) -> List[AxisCalibration]:
    """Per-axis calibration: does the axis score predict realized P&L?

    For each axis in ``KNOWN_AXES``:

      * Pull (axis_score, pnl_pct) pairs from every closed decision
        whose confidence_breakdown carries the axis.
      * Spearman ρ between axis_score and pnl_pct.
      * "Discrimination" = mean(pnl when axis ≥ high_threshold) -
        mean(pnl when axis ≤ low_threshold). Positive ⇒ the axis is
        predictive (high readings paid off more than low readings).

    Same min_n + staleness guardrails as ``compute_agent_calibration``.
    """
    if decisions is None:
        decisions = _iter_closed_decisions(
            window_days, include_synthetic=include_synthetic,
        )

    out: List[AxisCalibration] = []
    for axis in KNOWN_AXES:
        pairs: List[Tuple[float, float, datetime]] = []
        for d in decisions:
            score = _axis_score(d.confidence_breakdown, axis)
            if score is None:
                continue
            pairs.append((score, d.pnl_pct, d.decision_timestamp))
        n = len(pairs)
        sample_age = (
            max(
                0,
                (datetime.utcnow() - min(p[2] for p in pairs)).days,
            )
            if pairs else None
        )
        notes: List[str] = []
        if n < min_n:
            notes.append(f"insufficient_sample_size_n_lt_{min_n}")
            out.append(AxisCalibration(
                axis=axis, n_closed=n,
                sample_age_days=sample_age, notes=notes,
            ))
            continue
        if sample_age is not None and sample_age > stale_after_days:
            notes.append("stale_calibration")

        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        rho = _spearman(xs, ys)

        high_pnl = [y for x, y, _ in pairs if x >= high_threshold]
        low_pnl = [y for x, y, _ in pairs if x <= low_threshold]
        high_mean = (
            statistics.fmean(high_pnl) if high_pnl else None
        )
        low_mean = statistics.fmean(low_pnl) if low_pnl else None
        if high_mean is not None and low_mean is not None:
            disc = high_mean - low_mean
        else:
            disc = None

        out.append(AxisCalibration(
            axis=axis,
            n_closed=n,
            spearman_corr=(round(rho, 4) if rho is not None else None),
            high_axis_pnl_mean=(
                round(high_mean, 4) if high_mean is not None else None
            ),
            low_axis_pnl_mean=(
                round(low_mean, 4) if low_mean is not None else None
            ),
            high_axis_n=len(high_pnl),
            low_axis_n=len(low_pnl),
            discrimination=(round(disc, 4) if disc is not None else None),
            sample_age_days=sample_age,
            notes=notes,
        ))

    return out


# ── Per-strategy calibration ──────────────────────────────────────────


def _provenance_breakdown(
    items: List[_ClosedDecision],
) -> Dict[str, int]:
    """Count how many decisions in the bucket came from each provenance
    tag (see ``_ClosedDecision`` docstring). Used by the strategy
    calibrator to surface fallback vs entry-side attribution counts
    so the operator can spot calibrations that are mostly fallback."""
    out: Dict[str, int] = {}
    for d in items:
        key = d.strategy_provenance or "unknown"
        out[key] = out.get(key, 0) + 1
    return out


def compute_strategy_calibration(
    *, window_days: int = DEFAULT_WINDOW_DAYS,
    min_n: int = DEFAULT_MIN_N_STRATEGY,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    decisions: Optional[List[_ClosedDecision]] = None,
    include_synthetic: bool = False,
) -> List[StrategyCalibration]:
    """Per-strategy calibration, stratified by RegimeVector.trend.

    Groups closed decisions by the 15.C entry-side template name
    extracted from ``decision_provenance.strategy_matrix_json`` (see
    ``_resolve_strategy_attribution`` for the fallback chain). Closed
    rows with no matrix attribution surface under the ``UNATTRIBUTED``
    sentinel bucket so the operator can see how much of the closed
    trade history pre-dates 15.C / lacks entry-side provenance.

    For each strategy with ≥ min_n closed trades, reports:

      * hit_rate + Wilson 95% CI
      * mean / median pnl_pct
      * by_regime: same numbers per trend bucket (trending_up,
        trending_down, ranging, etc.)
      * provenance_breakdown: how many rows from strategy_matrix
        vs fallback vs UNATTRIBUTED (18-FU Gap 2 honesty surface).

    Honest-attribution note: an ``UNATTRIBUTED`` bucket below min_n
    is normal — it just records "we have N closed trades whose entry
    decision did not write a strategy_matrix_json". An UNATTRIBUTED
    bucket ABOVE min_n is a wake-up: the operator should investigate
    why so many entries lack matrix provenance.
    """
    if decisions is None:
        decisions = _iter_closed_decisions(
            window_days, include_synthetic=include_synthetic,
        )

    by_strategy: Dict[str, List[_ClosedDecision]] = {}
    for d in decisions:
        key = d.strategy_name or UNATTRIBUTED_STRATEGY
        by_strategy.setdefault(key, []).append(d)

    out: List[StrategyCalibration] = []
    for strategy, items in by_strategy.items():
        n = len(items)
        sample_age = max(
            0,
            (datetime.utcnow() - min(d.decision_timestamp for d in items)).days,
        )
        prov_breakdown = _provenance_breakdown(items)
        if n < min_n:
            notes_low: List[str] = [
                f"insufficient_sample_size_n_lt_{min_n}",
            ]
            if strategy == UNATTRIBUTED_STRATEGY:
                # Make the operator aware that this bucket is the
                # 18-FU Gap 2 sentinel — not a real strategy.
                notes_low.append("no_strategy_matrix_at_entry")
            out.append(StrategyCalibration(
                strategy_name=strategy,
                n_closed=n,
                sample_age_days=sample_age,
                notes=notes_low,
                provenance_breakdown=prov_breakdown,
            ))
            continue
        notes: List[str] = []
        if sample_age > stale_after_days:
            notes.append("stale_calibration")
        if strategy == UNATTRIBUTED_STRATEGY:
            notes.append("no_strategy_matrix_at_entry")

        pnls = [d.pnl_pct for d in items]
        wins = sum(d.win for d in items)
        wlow, whi = _wilson_interval(wins, n)
        hit_rate = wins / n

        # Stratify by regime trend bucket.
        by_regime: Dict[str, List[_ClosedDecision]] = {}
        for d in items:
            bucket = d.regime_trend or "unknown"
            by_regime.setdefault(bucket, []).append(d)
        regime_block: Dict[str, Dict[str, Any]] = {}
        for bucket, sub in by_regime.items():
            sub_pnls = [d.pnl_pct for d in sub]
            sub_wins = sum(d.win for d in sub)
            regime_block[bucket] = {
                "n": len(sub),
                "hit_rate": round(sub_wins / len(sub), 4),
                "mean_pnl_pct": round(statistics.fmean(sub_pnls), 4),
            }

        out.append(StrategyCalibration(
            strategy_name=strategy,
            n_closed=n,
            hit_rate=round(hit_rate, 4),
            hit_rate_wilson_lower=(
                round(wlow, 4) if wlow is not None else None
            ),
            hit_rate_wilson_upper=(
                round(whi, 4) if whi is not None else None
            ),
            mean_pnl_pct=round(statistics.fmean(pnls), 4),
            median_pnl_pct=round(statistics.median(pnls), 4),
            by_regime=regime_block,
            sample_age_days=sample_age,
            notes=notes,
            provenance_breakdown=prov_breakdown,
        ))

    return out


# ── Composite report ──────────────────────────────────────────────────


def compute_attribution_report(
    *, window_days: int = DEFAULT_WINDOW_DAYS,
    min_n_agent: int = DEFAULT_MIN_N_AGENT,
    min_n_axis: int = DEFAULT_MIN_N_AXIS,
    min_n_strategy: int = DEFAULT_MIN_N_STRATEGY,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    include_synthetic: bool = False,
) -> Dict[str, Any]:
    """Single-pass composite. Computes the closed-decision list ONCE
    and feeds it to all three calibrators. Returns a dict the
    persistence layer + route surface can both consume directly.

    Total cost: one query against decision_provenance + one query
    against trades + decode-once, fan-out math. Fits comfortably
    inside the engine's cycle-time budget for any realistic window.
    """
    decisions = _iter_closed_decisions(
        window_days, include_synthetic=include_synthetic,
    )
    agents = compute_agent_calibration(
        window_days=window_days, min_n=min_n_agent,
        stale_after_days=stale_after_days,
        decisions=decisions,
    )
    axes = compute_axis_calibration(
        window_days=window_days, min_n=min_n_axis,
        stale_after_days=stale_after_days,
        decisions=decisions,
    )
    strategies = compute_strategy_calibration(
        window_days=window_days, min_n=min_n_strategy,
        stale_after_days=stale_after_days,
        decisions=decisions,
    )
    return {
        "computed_at": datetime.utcnow().isoformat(),
        "window_days": window_days,
        "n_closed_decisions": len(decisions),
        "min_n_agent": min_n_agent,
        "min_n_axis": min_n_axis,
        "min_n_strategy": min_n_strategy,
        "stale_after_days": stale_after_days,
        "include_synthetic": bool(include_synthetic),
        "agents": [a.to_dict() for a in agents],
        "axes": [a.to_dict() for a in axes],
        "strategies": [s.to_dict() for s in strategies],
    }
