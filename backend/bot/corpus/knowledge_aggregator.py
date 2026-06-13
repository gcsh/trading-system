"""MITS Phase 0 — knowledge aggregator.

`recompute_cells(ticker=None)` walks every (ticker, pattern, regime,
vol_state, time_bucket, horizon) cohort and computes:

  * sample_size — count of observations with outcomes for this horizon
  * win_rate — wins / sample_size (frequentist)
  * posterior_win_rate — Bayesian shrinkage with the matching prior
  * avg_return_pct — mean of return_pct
  * avg_hold_minutes — coarse mapping of horizon to minutes
  * confidence_lower / confidence_upper — Wilson 95% CI

Idempotent UPSERT keyed on the composite cohort tuple.

Bayesian shrinkage formula (mirrors ``scorecard.vote_weights``):

    posterior = (wins + prior_weight * prior_wr) / (n + prior_weight)
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.knowledge_graph_history import KnowledgeGraphHistory
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.pattern_prior import PatternPrior

logger = logging.getLogger(__name__)


HORIZON_HOLD_MIN: Dict[str, float] = {
    "5min": 5.0,
    "30min": 30.0,
    "60min": 60.0,
    "1d": 60.0 * 24,
    "5d": 60.0 * 24 * 5,
    "20d": 60.0 * 24 * 20,
}


# ── Bayesian + Wilson helpers ─────────────────────────────────────────


def _wilson_interval(wins: int, n: int, z: float = 1.96
                            ) -> Tuple[Optional[float], Optional[float]]:
    """Wilson 95% CI for a binomial proportion."""
    if n == 0:
        return None, None
    p_hat = wins / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z * z / (4.0 * n * n)))) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _lookup_prior(priors_by_pattern: Dict[str, List[Dict[str, Any]]],
                       pattern: str, regime: str) -> Tuple[float, int]:
    """Find the best-matching prior. Exact-cohort match preferred,
    falls back to ``any``, then to (0.5, 10) global default."""
    rows = priors_by_pattern.get(pattern) or []
    best_exact = None
    best_any = None
    for row in rows:
        desc = (row["cohort_descriptor"] or "any").lower()
        if desc == regime.lower():
            best_exact = row
            break
        if desc == "any":
            best_any = row
    chosen = best_exact or best_any
    if chosen is None:
        return 0.5, 10
    return (float(chosen["prior_win_rate"] or 0.5),
              int(chosen["prior_weight"] or 10))


def _load_priors() -> Dict[str, List[Dict[str, Any]]]:
    """Group priors by pattern for fast lookup. Returns plain dicts so
    the session can close cleanly before the aggregator iterates."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with session_scope() as s:
            for row in s.execute(select(
                PatternPrior.pattern, PatternPrior.cohort_descriptor,
                PatternPrior.prior_win_rate, PatternPrior.prior_weight,
            )).all():
                pat, cohort, wr, w = row
                out.setdefault(pat, []).append({
                    "cohort_descriptor": cohort, "prior_win_rate": wr,
                    "prior_weight": w,
                })
    except Exception:
        logger.debug("priors load failed", exc_info=True)
    return out


# ── core aggregation ──────────────────────────────────────────────────


# MITS Phase 1 — sample-split constants. Mirror the values written to
# `KnowledgeGraphCell.sample_split` so UI filters can be exhaustive.
SAMPLE_SPLIT_IN = "in_sample"
SAMPLE_SPLIT_OUT = "out_of_sample"
SAMPLE_SPLIT_COMBINED = "combined"
SAMPLE_SPLITS = (SAMPLE_SPLIT_IN, SAMPLE_SPLIT_OUT, SAMPLE_SPLIT_COMBINED)

# Observation provenance → sample_split bucket.
# Anything stamped `historical_replay` is in-sample; live runs are
# out-of-sample. Unknown provenance defaults to in_sample so cells
# remain conservative.
# MITS Phase 6 (P6.1): `live_trade` joins the live family — closed
# Trade rows ingested via `live_outcome_ingest.ingest_closed_trade`
# get this provenance.
_LIVE_SOURCES = {"live_engine", "live", "live_trade"}

# MITS Phase 13 Fix 3 — time-based walk-forward split fraction. The
# last `WALK_FORWARD_OOS_FRAC` of observations (by timestamp, per
# cohort) becomes out-of-sample. Previous frequency-based partitioning
# only yielded 580 OOS cells with avg N=9 across the corpus.
WALK_FORWARD_OOS_FRAC = 0.20

# MITS Phase 13 Fix 4 — sentinel ticker/regime values for persisted
# hierarchical parent rows. The aggregator writes one parent row per
# (pattern, regime) pool (ticker="__ALL__") and one global parent row
# per (pattern) pool (ticker="__ALL__", regime="__ALL__"). Cell rows
# keep parent_type="cell". The consumer fallback queries these rows
# instead of recomputing the pools on every request.
SENTINEL_TICKER_ALL = "__ALL__"
SENTINEL_REGIME_ALL = "__ALL__"
PARENT_TYPE_CELL = "cell"
PARENT_TYPE_PR = "pattern_regime_parent"
PARENT_TYPE_P = "pattern_parent"

# MITS Phase 13 Fix 5 — axis sentinel for dropped aggregation axes.
# When the per-detector axes tunable removes vol_state / time_bucket,
# the cell row records "__ANY__" on the dropped axis. Consumer
# fallback treats __ANY__ as a wildcard match.
AXIS_SENTINEL_ANY = "__ANY__"


def _detector_axes() -> Dict[str, List[str]]:
    """Parse the per-detector aggregation-axes tunable.

    Default is the full 4-axis split. Detectors listed here can drop
    axes from the cohort key so they collect enough observations per
    cell to clear the N>=30 confidence floor.
    """
    import json as _json
    try:
        raw = TUNABLES.detector_aggregation_axes_json or "{}"
        parsed = _json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): list(v) for k, v in parsed.items()
                    if isinstance(v, list)}
    except Exception:
        logger.debug("detector_aggregation_axes parse failed", exc_info=True)
    return {}


# MITS Phase 13 Pass 3 — default axes are (ticker, regime). Pooling
# across vol_state AND time_bucket gives every cohort enough mass for
# an 80/20 walk-forward split to leave N>=30 in the OOS bucket. Pre-
# Pass-3 the 4-axis grid yielded 0 cells with N>=150 (the floor needed
# to clear N>=30 in OOS after a 20% time-slice). Per-detector overrides
# can RE-INTRODUCE axes for detectors that have enough data, via
# TUNABLES.detector_aggregation_axes_json (e.g. broaden vwap detectors
# which fire many times per day).
FULL_AXES = ("ticker", "regime")


def _axis_value(member: Dict[str, Any], axis: str, kept: List[str]) -> str:
    """Return the cohort axis value for a member; sentinel when the
    axis is dropped for this pattern's aggregation policy."""
    if axis in kept:
        v = member.get(axis)
        return v if v is not None else "unknown"
    return AXIS_SENTINEL_ANY


def _classify_split(source: Optional[str],
                          *,
                          timestamp: Optional[Any] = None,
                          cutoff_by_ticker: Optional[Dict[str, Any]] = None,
                          ticker: Optional[str] = None) -> str:
    """Classify an observation into in_sample vs out_of_sample.

    MITS Phase 2 (P2.5 refinement): when the ticker has a recorded
    `first_live_observation_at` cutoff (computed during the same
    `recompute_cells` run), use TIMESTAMP-based splitting:
      - timestamp < cutoff → in_sample
      - timestamp >= cutoff → out_of_sample

    When no cutoff is available (ticker has never had a live
    observation), fall back to source-based splitting (Phase 1
    behaviour, preserves backward compat).
    """
    if cutoff_by_ticker and ticker:
        cutoff = cutoff_by_ticker.get(ticker)
        if cutoff is not None and timestamp is not None:
            try:
                if timestamp >= cutoff:
                    return SAMPLE_SPLIT_OUT
                return SAMPLE_SPLIT_IN
            except Exception:
                pass
    # Phase 1 fallback — source-based.
    if source and source in _LIVE_SOURCES:
        return SAMPLE_SPLIT_OUT
    return SAMPLE_SPLIT_IN


def _fetch_obs_with_outcomes(ticker: Optional[str]) -> List[Dict[str, Any]]:
    """Pull all (obs + outcome) joined rows in a single query.

    Returns flat dicts so the caller can group without touching ORM
    sessions later. The `source` column AND `timestamp` ride along so
    the aggregator can split observations into in_sample vs
    out_of_sample cohorts via either source-based or
    TIMESTAMP-based partitioning (MITS Phase 2 P2.5).

    MITS Phase 13 Fix 7 — `direction` rides along so the aggregator can
    compute direction-aware Wilson CIs (separate long/short bounds).
    """
    out: List[Dict[str, Any]] = []
    try:
        with session_scope() as s:
            q = select(
                MarketObservation.ticker,
                MarketObservation.pattern,
                MarketObservation.regime,
                MarketObservation.vol_state,
                MarketObservation.time_bucket,
                MarketOutcome.horizon,
                MarketOutcome.return_pct,
                MarketOutcome.was_winner,
                MarketObservation.source,
                MarketObservation.timestamp,
                MarketObservation.direction,
            ).join(MarketOutcome,
                      MarketOutcome.observation_id == MarketObservation.id)
            if ticker:
                q = q.where(MarketObservation.ticker == ticker.upper().strip())
            for row in s.execute(q).all():
                (tkr, pattern, regime, vol_state, time_bucket, horizon,
                 ret, won, source, ts, direction) = row
                out.append({
                    "ticker": tkr, "pattern": pattern, "regime": regime,
                    "vol_state": vol_state, "time_bucket": time_bucket,
                    "horizon": horizon,
                    "return_pct": float(ret) if ret is not None else 0.0,
                    "was_winner": bool(won) if won is not None else None,
                    "source": source,
                    "timestamp": ts,
                    "direction": direction or "long",
                })
    except Exception:
        logger.exception("obs-with-outcomes fetch failed")
    return out


def _compute_first_live_per_ticker(ticker: Optional[str] = None
                                              ) -> Dict[str, Any]:
    """Recompute `first_live_observation_at` cutoff for each ticker.

    Walks `market_observations` for source IN _LIVE_SOURCES and takes
    the MIN(timestamp) per ticker. Persists onto `corpus_status` AND
    returns the in-memory map for the aggregator to use in the same
    pass (so we don't have to do two write passes).
    """
    from sqlalchemy import func as _func
    cutoffs: Dict[str, Any] = {}
    try:
        with session_scope() as s:
            q = (select(MarketObservation.ticker,
                              _func.min(MarketObservation.timestamp))
                       .where(MarketObservation.source.in_(_LIVE_SOURCES))
                       .group_by(MarketObservation.ticker))
            if ticker:
                q = q.where(MarketObservation.ticker == ticker.upper().strip())
            for tkr, mn in s.execute(q).all():
                if mn is None:
                    continue
                cutoffs[tkr] = mn
            # Persist onto corpus_status (idempotent UPSERT).
            for tkr, cutoff in cutoffs.items():
                row = s.execute(
                    select(CorpusStatus).where(CorpusStatus.ticker == tkr)
                ).scalar_one_or_none()
                if row is None:
                    row = CorpusStatus(ticker=tkr, status="building")
                    s.add(row)
                    s.flush()
                row.first_live_observation_at = cutoff
    except Exception:
        logger.debug("first_live cutoff compute failed", exc_info=True)
    return cutoffs


def _upsert_cell(session, cell_key: Tuple[str, str, str, str, str, str, str],
                     values: Dict[str, Any]) -> str:
    """UPSERT a KnowledgeGraphCell row. Returns "inserted" | "updated".

    MITS Phase 1: cohort key now includes `sample_split` so each (in_sample,
    out_of_sample, combined) row lives in its own slot."""
    (tkr, pattern, regime, vol_state, time_bucket, horizon,
     sample_split) = cell_key
    row = session.execute(
        select(KnowledgeGraphCell)
        .where(KnowledgeGraphCell.ticker == tkr)
        .where(KnowledgeGraphCell.pattern == pattern)
        .where(KnowledgeGraphCell.regime == regime)
        .where(KnowledgeGraphCell.vol_state == vol_state)
        .where(KnowledgeGraphCell.time_bucket == time_bucket)
        .where(KnowledgeGraphCell.horizon == horizon)
        .where(KnowledgeGraphCell.sample_split == sample_split)
    ).scalar_one_or_none()
    action = "updated"
    if row is None:
        row = KnowledgeGraphCell(
            ticker=tkr, pattern=pattern, regime=regime,
            vol_state=vol_state, time_bucket=time_bucket, horizon=horizon,
            sample_split=sample_split,
        )
        session.add(row)
        action = "inserted"
    for k, v in values.items():
        setattr(row, k, v)
    row.last_updated = datetime.utcnow()
    return action


# MITS Phase 12.H — confidence-level thresholds.
CONFIDENCE_HIGH_N = 100
CONFIDENCE_MEDIUM_N = 30
CONFIDENCE_LOW_N = 10


def _classify_confidence(n: int) -> str:
    if n >= CONFIDENCE_HIGH_N:
        return "high"
    if n >= CONFIDENCE_MEDIUM_N:
        return "medium"
    if n >= CONFIDENCE_LOW_N:
        return "low"
    return "thin"


def _aggregate_members(members: List[Dict[str, Any]],
                                pattern: str, regime: str, horizon: str,
                                priors_by_pattern: Dict[str, List[Dict[str, Any]]],
                                *,
                                split: str = SAMPLE_SPLIT_COMBINED,
                                hierarchical_priors: Optional[Dict[str, Any]] = None,
                                ) -> Optional[Dict[str, Any]]:
    """Compute the aggregate stats dict for a single group of members.

    Returns None when the group is empty so the caller skips persisting
    a zero-sample row.

    MITS Phase 6 (P6.1): when ``split == 'combined'`` the posterior uses
    the live-weight multiplier from TUNABLES — each live observation
    counts as ``live_outcome_weight_multiplier`` historical observations
    in the Beta-Binomial update. When live N >= live_n_authoritative
    _floor, the combined cell's primary posterior is computed from
    live observations only (with historical kept as context). For
    `in_sample` / `out_of_sample` rows we keep the legacy
    unweighted formula so each split remains internally honest.
    """
    n = len(members)
    if n == 0:
        return None
    wins = sum(1 for m in members if m["was_winner"])
    returns = [m["return_pct"] for m in members]
    win_rate = (wins / n) if n else None
    prior_wr, prior_weight = _lookup_prior(priors_by_pattern, pattern, regime)

    # MITS Phase 12.H — hierarchical Bayesian shrinkage.
    #
    # When the cohort cell is thin (N < CONFIDENCE_HIGH_N) we shrink
    # toward two parent distributions in sequence:
    #
    #   (1) (pattern, regime) cross-ticker mean — the "what does this
    #       pattern do in this regime across ALL tickers?" estimate.
    #   (2) (pattern) global mean — the "what does this pattern do
    #       overall?" fallback when (1) is also thin.
    #
    # Shrinkage weight K is the effective sample size of the parent.
    # We blend with weight N / (N + K) toward the local estimate.
    #
    # This collapses the corpus's 83 percent N<30 cell problem: a
    # 5-observation cell now borrows from the 800-observation
    # (pattern, regime) parent instead of being driven by the academic
    # prior alone.
    if hierarchical_priors is not None and n < CONFIDENCE_HIGH_N:
        pr_key = (pattern, regime)
        pr_parent = hierarchical_priors.get("pattern_regime", {}).get(pr_key)
        p_parent = hierarchical_priors.get("pattern", {}).get(pattern)
        # Prefer (pattern, regime) parent; if too thin (< 30 obs)
        # fall through to (pattern) parent.
        chosen_parent = None
        if pr_parent is not None and pr_parent["n"] >= CONFIDENCE_MEDIUM_N:
            chosen_parent = pr_parent
        elif p_parent is not None and p_parent["n"] >= CONFIDENCE_MEDIUM_N:
            chosen_parent = p_parent
        if chosen_parent is not None:
            # Use the parent's win rate as the prior_wr and an effective
            # sample size capped at CONFIDENCE_HIGH_N to avoid drowning
            # out genuine local signal in very large parents.
            prior_wr = float(chosen_parent["win_rate"])
            prior_weight = min(int(chosen_parent["n"]), CONFIDENCE_HIGH_N)

    # Default unweighted Beta-Binomial posterior — what every split row
    # has used since Phase 1.
    posterior = ((wins + prior_weight * prior_wr) /
                    (n + prior_weight)) if (n + prior_weight) > 0 else None

    # MITS Phase 6 — when computing the COMBINED row, blend in the
    # live-weight multiplier (and override to live-only when live N
    # crosses the authoritative floor).
    if split == SAMPLE_SPLIT_COMBINED:
        try:
            from backend.bot.corpus.live_outcome_ingest import (
                apply_live_weighted_posterior,
                split_observations_by_provenance,
            )
            hist_members, live_members = split_observations_by_provenance(members)
            live_wins = sum(1 for m in live_members if m["was_winner"])
            hist_wins = sum(1 for m in hist_members if m["was_winner"])
            outcome = apply_live_weighted_posterior(
                historical_n=len(hist_members),
                historical_wins=hist_wins,
                live_n=len(live_members),
                live_wins=live_wins,
                prior_wr=prior_wr,
                prior_weight=float(prior_weight),
            )
            primary = outcome.get("primary_posterior")
            if primary is not None:
                posterior = primary
        except Exception:
            logger.debug("live-weighted posterior compute failed",
                                exc_info=True)

    avg_return = sum(returns) / n if returns else None
    avg_hold = HORIZON_HOLD_MIN.get(horizon)
    lo, hi = _wilson_interval(wins, n)

    # MITS Phase 13 Fix 7 — direction-aware Wilson CIs. Compute
    # separate long/short bounds when the cell has both directions
    # present; leave NULL when direction is uniform so consumers
    # cleanly fall back to confidence_lower/upper.
    lo_long = hi_long = lo_short = hi_short = None
    try:
        long_members = [m for m in members
                        if (m.get("direction") or "long") == "long"]
        short_members = [m for m in members
                         if (m.get("direction") or "long") == "short"]
        if long_members and short_members:
            n_long = len(long_members)
            wins_long = sum(1 for m in long_members if m["was_winner"])
            n_short = len(short_members)
            wins_short = sum(1 for m in short_members if m["was_winner"])
            lo_long, hi_long = _wilson_interval(wins_long, n_long)
            lo_short, hi_short = _wilson_interval(wins_short, n_short)
    except Exception:
        lo_long = hi_long = lo_short = hi_short = None

    return {
        "sample_size": n,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "posterior_win_rate": (round(posterior, 4)
                                    if posterior is not None else None),
        "avg_return_pct": (round(avg_return, 6)
                                  if avg_return is not None else None),
        "avg_hold_minutes": avg_hold,
        "confidence_lower": (round(lo, 4) if lo is not None else None),
        "confidence_upper": (round(hi, 4) if hi is not None else None),
        "confidence_lower_long": (round(lo_long, 4) if lo_long is not None else None),
        "confidence_upper_long": (round(hi_long, 4) if hi_long is not None else None),
        "confidence_lower_short": (round(lo_short, 4) if lo_short is not None else None),
        "confidence_upper_short": (round(hi_short, 4) if hi_short is not None else None),
        # MITS Phase 12.H — confidence-level label, used by consumers
        # to filter thin cells from decision paths.
        "confidence_level": _classify_confidence(n),
    }


def _compute_hierarchical_priors(rows: List[Dict[str, Any]]
                                            ) -> Dict[str, Any]:
    """MITS Phase 12.H — compute parent distributions.

    Returns two nested dicts keyed by:

      * ``("pattern", "regime")`` → {"n": int, "win_rate": float}
      * ``("pattern",)``           → {"n": int, "win_rate": float}

    The (pattern, regime) tier pools across all tickers + vol_states +
    time_buckets for a single (pattern, regime). The (pattern) tier is
    the global pattern fallback.

    Cells with N < CONFIDENCE_HIGH_N (100) borrow from these parents
    via the Beta-Binomial update inside ``_aggregate_members``.
    """
    pr_pool: Dict[Tuple[str, str], List[bool]] = {}
    p_pool: Dict[str, List[bool]] = {}
    for r in rows:
        won = r.get("was_winner")
        if won is None:
            continue
        # Restrict to 5d horizon for the parent pool — same horizon
        # used by the audit + by detector/edge endpoint downstream.
        # Cells at other horizons compute their own parents below if
        # needed.
        horizon = r.get("horizon")
        if horizon != "5d":
            continue
        pat = r.get("pattern")
        reg = r.get("regime")
        if not pat or reg is None:
            continue
        pr_pool.setdefault((pat, reg), []).append(bool(won))
        p_pool.setdefault(pat, []).append(bool(won))
    pr_out: Dict[Tuple[str, str], Dict[str, float]] = {}
    for key, wins_list in pr_pool.items():
        n = len(wins_list)
        if n == 0:
            continue
        pr_out[key] = {"n": n, "win_rate": sum(wins_list) / n}
    p_out: Dict[str, Dict[str, float]] = {}
    for pat, wins_list in p_pool.items():
        n = len(wins_list)
        if n == 0:
            continue
        p_out[pat] = {"n": n, "win_rate": sum(wins_list) / n}
    return {"pattern_regime": pr_out, "pattern": p_out}


def recompute_cells(ticker: Optional[str] = None) -> Dict[str, Any]:
    """Recompute KnowledgeGraphCell rows from the obs + outcomes tables.

    Pass ``ticker`` to scope to one symbol; ``None`` scans the whole
    corpus.

    MITS Phase 1: writes THREE rows per 6-axis cohort:
      * `in_sample`     — observations sourced from `historical_replay`
      * `out_of_sample` — observations sourced from `live_engine`
      * `combined`      — every observation regardless of provenance

    The UI defaults to `out_of_sample` so traders see live edge first,
    with toggles for in_sample (training) and combined.

    MITS Phase 3: observations whose pattern is in the operator-
    disabled set (`detector_config.enabled = False`) are EXCLUDED
    from the aggregation pass. Existing cells for disabled patterns
    stay on disk so re-enabling restores them on the next pass.
    """
    stats = {"cells_inserted": 0, "cells_updated": 0, "cohorts": 0,
              "observations_seen": 0,
              "in_sample_cells": 0, "out_of_sample_cells": 0,
              "combined_cells": 0,
              "disabled_patterns_skipped": 0,
              # MITS Phase 12.H — confidence-level cell counts.
              "cells_high": 0, "cells_medium": 0, "cells_low": 0,
              "cells_thin": 0,
              "hierarchical_parents_pr": 0,
              "hierarchical_parents_pattern": 0}
    rows = _fetch_obs_with_outcomes(ticker)
    stats["observations_seen"] = len(rows)
    if not rows:
        return stats

    # MITS Phase 3 — mask observations for operator-disabled patterns.
    try:
        from backend.bot.detectors import disabled_patterns as _disabled_fn
        disabled = _disabled_fn()
    except Exception:
        disabled = set()
    if disabled:
        before = len(rows)
        rows = [r for r in rows if r["pattern"] not in disabled]
        stats["disabled_patterns_skipped"] = before - len(rows)
        if not rows:
            return stats

    priors_by_pattern = _load_priors()

    # MITS Phase 12.H — hierarchical parent distributions (pattern,
    # regime) and (pattern). Thin (N<100) cells shrink toward these
    # before the academic prior kicks in, which fixes the audit
    # finding that 83 percent of cells had N<30 statistically thin.
    hierarchical_priors = _compute_hierarchical_priors(rows)
    stats["hierarchical_parents_pr"] = len(hierarchical_priors["pattern_regime"])
    stats["hierarchical_parents_pattern"] = len(hierarchical_priors["pattern"])

    # MITS Phase 2 (P2.5 refinement): compute per-ticker cutoffs FIRST so
    # the partitioning below can use TIMESTAMP-based splitting when the
    # ticker has a live observation history. Tickers without any live
    # observations stay on source-based splitting.
    cutoff_by_ticker = _compute_first_live_per_ticker(ticker)
    stats["tickers_with_live_cutoff"] = len(cutoff_by_ticker)

    # MITS Phase 13 Fix 5 — per-detector axis tunable. Detectors with
    # an override drop axes (e.g. time_bucket) so each cell collects
    # enough observations to clear the N>=30 confidence floor.
    axes_overrides = _detector_axes()

    # Group by the (per-detector) cohort key (no split yet — we
    # partition below). For detectors NOT in the overrides map we
    # keep the legacy full 4-axis grouping (ticker, regime, vol_state,
    # time_bucket); for overridden detectors we substitute "__ANY__"
    # on the dropped axes so all observations collapse into a single
    # cell for the kept axes.
    groups: Dict[Tuple[str, str, str, str, str, str],
                       List[Dict[str, Any]]] = {}
    for r in rows:
        pat = r["pattern"]
        kept = axes_overrides.get(pat) or list(FULL_AXES)
        tkr = _axis_value(r, "ticker", kept)
        reg = _axis_value(r, "regime", kept)
        vs = _axis_value(r, "vol_state", kept)
        tb = _axis_value(r, "time_bucket", kept)
        key = (tkr, pat, reg, vs, tb, r["horizon"])
        groups.setdefault(key, []).append(r)

    stats["cohorts"] = len(groups)
    try:
        with session_scope() as s:
            for key, members in groups.items():
                tkr = key[0]
                # MITS Phase 13 Fix 3 — TIME-based walk-forward split
                # PER COHORT. Sort by timestamp; the 80th percentile of
                # timestamps becomes the in/out cutoff. Anything at or
                # after the cutoff goes out_of_sample. We compute it
                # per cohort because pattern frequencies vary by ticker
                # and a global cutoff would starve small cohorts.
                # When the cohort has <5 obs we fall back to source-
                # based splitting so we don't fabricate OOS from noise.
                cohort_cutoff = None
                ts_sorted = []
                try:
                    ts_sorted = sorted(
                        [m.get("timestamp") for m in members
                         if m.get("timestamp") is not None],
                    )
                except Exception:
                    ts_sorted = []
                if len(ts_sorted) >= 5:
                    cut_idx = max(
                        1, int(round(len(ts_sorted)
                                     * (1.0 - WALK_FORWARD_OOS_FRAC))),
                    )
                    cut_idx = min(cut_idx, len(ts_sorted) - 1)
                    cohort_cutoff = ts_sorted[cut_idx]

                in_members: List[Dict[str, Any]] = []
                out_members: List[Dict[str, Any]] = []
                if cohort_cutoff is not None:
                    for m in members:
                        ts = m.get("timestamp")
                        if ts is None:
                            in_members.append(m)
                            continue
                        try:
                            if ts >= cohort_cutoff:
                                out_members.append(m)
                            else:
                                in_members.append(m)
                        except Exception:
                            in_members.append(m)
                else:
                    # Cohort too small for time-split — use source tag.
                    for m in members:
                        if _classify_split(
                            m.get("source"),
                            timestamp=m.get("timestamp"),
                            cutoff_by_ticker=cutoff_by_ticker,
                            ticker=tkr,
                        ) == SAMPLE_SPLIT_OUT:
                            out_members.append(m)
                        else:
                            in_members.append(m)
                buckets = {
                    SAMPLE_SPLIT_IN: in_members,
                    SAMPLE_SPLIT_OUT: out_members,
                    SAMPLE_SPLIT_COMBINED: members,
                }
                for split, bucket_members in buckets.items():
                    values = _aggregate_members(
                        bucket_members, key[1], key[2], key[5],
                        priors_by_pattern, split=split,
                        hierarchical_priors=hierarchical_priors,
                    )
                    if values is None:
                        continue
                    # MITS Phase 13 Fix 4 — explicit parent_type so an
                    # upsert can't inherit a stale value from a prior
                    # row written under a different policy.
                    values["parent_type"] = PARENT_TYPE_CELL
                    full_key = key + (split,)
                    action = _upsert_cell(s, full_key, values)
                    if action == "inserted":
                        stats["cells_inserted"] += 1
                    else:
                        stats["cells_updated"] += 1
                    if split == SAMPLE_SPLIT_IN:
                        stats["in_sample_cells"] += 1
                    elif split == SAMPLE_SPLIT_OUT:
                        stats["out_of_sample_cells"] += 1
                    else:
                        stats["combined_cells"] += 1
                    # MITS Phase 12.H — confidence-level bookkeeping.
                    conf = values.get("confidence_level")
                    if conf == "high":
                        stats["cells_high"] += 1
                    elif conf == "medium":
                        stats["cells_medium"] += 1
                    elif conf == "low":
                        stats["cells_low"] += 1
                    else:
                        stats["cells_thin"] += 1
    except Exception:
        logger.exception("cell upsert failed")

    # ── MITS Phase 13 Fix 4 — persist hierarchical parent rows. ──
    # The aggregator already computed `hierarchical_priors` for the
    # 5d horizon (used by Beta-Binomial shrinkage inside cells).
    # Persist those parents as queryable rows in knowledge_graph so
    # consumers can SELECT them by ticker="__ALL__" instead of having
    # to recompute the pools on every fallback fetch.
    #
    # Parent rows are scoped to (pattern, regime, horizon='5d',
    # sample_split='combined') and (pattern, horizon='5d',
    # sample_split='combined'); vol_state / time_bucket use the
    # AXIS_SENTINEL_ANY value. They live in the same table because the
    # consumer fallback already SELECTs from knowledge_graph, and
    # adding a separate table would duplicate the schema.
    try:
        with session_scope() as s:
            for (pat, reg), parent in hierarchical_priors[
                "pattern_regime"].items():
                n = int(parent.get("n") or 0)
                if n <= 0:
                    continue
                wins = int(round(float(parent["win_rate"]) * n))
                lo, hi = _wilson_interval(wins, n)
                values = {
                    "sample_size": n,
                    "win_rate": round(float(parent["win_rate"]), 4),
                    "posterior_win_rate": round(float(parent["win_rate"]), 4),
                    "avg_return_pct": None,
                    "avg_hold_minutes": HORIZON_HOLD_MIN.get("5d"),
                    "confidence_lower": (round(lo, 4)
                                          if lo is not None else None),
                    "confidence_upper": (round(hi, 4)
                                          if hi is not None else None),
                    "confidence_level": _classify_confidence(n),
                    "parent_type": PARENT_TYPE_PR,
                }
                full_key = (SENTINEL_TICKER_ALL, pat, reg,
                            AXIS_SENTINEL_ANY, AXIS_SENTINEL_ANY,
                            "5d", SAMPLE_SPLIT_COMBINED)
                action = _upsert_cell(s, full_key, values)
                if action == "inserted":
                    stats["cells_inserted"] += 1
                else:
                    stats["cells_updated"] += 1
            for pat, parent in hierarchical_priors["pattern"].items():
                n = int(parent.get("n") or 0)
                if n <= 0:
                    continue
                wins = int(round(float(parent["win_rate"]) * n))
                lo, hi = _wilson_interval(wins, n)
                values = {
                    "sample_size": n,
                    "win_rate": round(float(parent["win_rate"]), 4),
                    "posterior_win_rate": round(float(parent["win_rate"]), 4),
                    "avg_return_pct": None,
                    "avg_hold_minutes": HORIZON_HOLD_MIN.get("5d"),
                    "confidence_lower": (round(lo, 4)
                                          if lo is not None else None),
                    "confidence_upper": (round(hi, 4)
                                          if hi is not None else None),
                    "confidence_level": _classify_confidence(n),
                    "parent_type": PARENT_TYPE_P,
                }
                full_key = (SENTINEL_TICKER_ALL, pat, SENTINEL_REGIME_ALL,
                            AXIS_SENTINEL_ANY, AXIS_SENTINEL_ANY,
                            "5d", SAMPLE_SPLIT_COMBINED)
                action = _upsert_cell(s, full_key, values)
                if action == "inserted":
                    stats["cells_inserted"] += 1
                else:
                    stats["cells_updated"] += 1
            stats["parent_rows_persisted_pr"] = len(
                hierarchical_priors["pattern_regime"])
            stats["parent_rows_persisted_pattern"] = len(
                hierarchical_priors["pattern"])
    except Exception:
        logger.exception("hierarchical parent persistence failed")

    # Update corpus_status cell_count when scoped to a ticker.
    if ticker:
        try:
            tkr = ticker.upper().strip()
            with session_scope() as s2:
                row = s2.execute(
                    select(CorpusStatus).where(CorpusStatus.ticker == tkr)
                ).scalar_one_or_none()
                if row is None:
                    row = CorpusStatus(ticker=tkr, status="building")
                    s2.add(row)
                    s2.flush()
                from sqlalchemy import func
                row.cell_count = int(s2.execute(
                    select(func.count(KnowledgeGraphCell.id))
                    .where(KnowledgeGraphCell.ticker == tkr)
                ).scalar_one() or 0)
        except Exception:
            logger.debug("corpus_status cell_count update failed", exc_info=True)
    return stats


def snapshot_cells_to_history(snapshot_date=None) -> Dict[str, Any]:
    """MITS Phase 1 — snapshot every current KnowledgeGraphCell into the
    KnowledgeGraphHistory table for the given calendar date.

    Idempotent on the (cohort + snapshot_date) unique key — running this
    job twice on the same day overwrites the row instead of duplicating.

    Returns a stats dict ({inserted, updated, errors}).
    """
    from datetime import date as _date_cls
    stats = {"inserted": 0, "updated": 0, "errors": 0}
    snap_date = snapshot_date or _date_cls.today()
    try:
        with session_scope() as s:
            cells = s.execute(select(KnowledgeGraphCell)).scalars().all()
            for c in cells:
                try:
                    existing = s.execute(
                        select(KnowledgeGraphHistory)
                        .where(KnowledgeGraphHistory.ticker == c.ticker)
                        .where(KnowledgeGraphHistory.pattern == c.pattern)
                        .where(KnowledgeGraphHistory.regime == c.regime)
                        .where(KnowledgeGraphHistory.vol_state == c.vol_state)
                        .where(KnowledgeGraphHistory.time_bucket == c.time_bucket)
                        .where(KnowledgeGraphHistory.horizon == c.horizon)
                        .where(KnowledgeGraphHistory.sample_split == c.sample_split)
                        .where(KnowledgeGraphHistory.snapshot_date == snap_date)
                    ).scalar_one_or_none()
                    if existing is None:
                        existing = KnowledgeGraphHistory(
                            ticker=c.ticker, pattern=c.pattern, regime=c.regime,
                            vol_state=c.vol_state, time_bucket=c.time_bucket,
                            horizon=c.horizon, sample_split=c.sample_split,
                            snapshot_date=snap_date,
                        )
                        s.add(existing)
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1
                    existing.sample_size = c.sample_size
                    existing.win_rate = c.win_rate
                    existing.posterior_win_rate = c.posterior_win_rate
                    existing.avg_return_pct = c.avg_return_pct
                    existing.confidence_lower = c.confidence_lower
                    existing.confidence_upper = c.confidence_upper
                except Exception:
                    stats["errors"] += 1
                    logger.debug("history snapshot row failed",
                                        exc_info=True)
    except Exception:
        logger.exception("snapshot_cells_to_history failed")
    return stats
