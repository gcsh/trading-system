"""MITS Phase 6 (P6.1) — Live outcomes recalibrate the corpus.

The recursive-learning closing piece. Each closed Trade becomes a
high-weight MarketObservation + MarketOutcome pair so the knowledge
graph reflects what's actually working in live trading — not just the
2026-pre-trial historical replay.

Public entry points:

  * ``ingest_closed_trade(trade_id)`` — convert one closed Trade row
    into a corpus pair. Idempotent (skips if a `live_engine`- or
    legacy `live_trade`-sourced observation for the same trade is
    already present).

  * ``ingest_live_outcomes()`` — nightly walker driven by the
    `IngestWatermark` row. Walks every closed Trade with id >
    last_ingested_trade_id, calls `ingest_closed_trade` for each,
    triggers cell recompute for affected (ticker, pattern) tuples.

  * ``apply_live_weight_to_aggregate(historical_n, historical_wins,
    live_n, live_wins, ...)`` — Beta-Binomial helper used by the
    knowledge aggregator. When live_n >= TUNABLES.live_n_authoritative
    _floor the live posterior wins; below that floor we blend live
    (weighted by `live_outcome_weight_multiplier`) into the historical
    aggregate.

Data-blame principle (memory entry): the source-tagged observations
make it possible to isolate posterior shifts to a specific
signal_source (eod_bias / brain / strategy). Without this, a bad
EOD-bias batch would silently poison the strategy-level priors.
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.ingest_watermark import IngestWatermark
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# Observation provenance tag for engine-cycle rows. The knowledge
# aggregator's `_classify_split` already treats anything other than
# `historical_replay` as out-of-sample, so this name fits the
# in_sample/out_of_sample split scheme.
#
# 16-followup Y1: canonical tag is `live_engine`. The legacy
# `live_trade` value (1,163 rows pre-flip) is preserved for
# back-compat: aggregator's `_LIVE_SOURCES` accepts both, the
# idempotency lookup below checks both, and
# `split_observations_by_provenance` routes both into the live bucket.
LIVE_ENGINE_SOURCE = "live_engine"
LEGACY_LIVE_TRADE_SOURCE = "live_trade"
_LIVE_SOURCES_FOR_IDEMPOTENCY = (LIVE_ENGINE_SOURCE, LEGACY_LIVE_TRADE_SOURCE)

WATERMARK_SOURCE = "live_outcome_ingest"


# ── pattern derivation ────────────────────────────────────────────────


def _derive_pattern(trade: Trade) -> Optional[str]:
    """Decide what pattern label to attach to the observation.

    Priority (most specific first):
      1. ``trade.detail_json["eod_bias"]["top_pattern"]`` — the EOD
         pass already named the pattern this trade was hypothesised
         to play.
      2. ``trade.detail_json["pattern"]`` — explicit pattern tag set
         by upstream callers (e.g. detector-fired trades).
      3. ``trade.strategy`` when not empty — used as a coarse
         strategy-level cohort label.
      4. ``trade.signal_source`` as a last resort (so eod_bias trades
         without a top_pattern still aggregate into a meaningful
         cohort instead of "unknown").

    Returns None when no pattern could be derived — caller skips the
    observation in that case.
    """
    if not trade or not trade.detail_json:
        # Fall back to strategy / signal_source.
        return (
            (trade.strategy or "").strip()
            or (trade.signal_source or "").strip()
            or None
        )
    try:
        detail = json.loads(trade.detail_json)
    except Exception:
        detail = {}
    if isinstance(detail, dict):
        eod = detail.get("eod_bias") or {}
        if isinstance(eod, dict):
            tp = eod.get("top_pattern")
            if tp and isinstance(tp, str) and tp.strip():
                return tp.strip()
        pat = detail.get("pattern")
        if pat and isinstance(pat, str) and pat.strip():
            return pat.strip()
    return (
        (trade.strategy or "").strip()
        or (trade.signal_source or "").strip()
        or None
    )


def _classify_horizon(days_held: float) -> str:
    """Map calendar-days-held to one of the canonical corpus horizons."""
    if days_held <= 0:
        return "60min"
    if days_held < 1:
        return "60min"
    if days_held < 3:
        return "1d"
    if days_held < 10:
        return "5d"
    return "20d"


def _cost_basis(trade: Trade) -> float:
    """Best-effort cost basis for a closed trade.

    For options, prefer ``price * contracts * 100`` because Trade.price
    is per-share. For stocks, ``price * quantity``. Falls back to the
    realized P&L denominator if we can't reconstruct.
    """
    try:
        if trade.instrument == "option" and trade.contracts:
            return abs(float(trade.price or 0.0) * float(trade.contracts or 0) * 100.0)
        return abs(float(trade.price or 0.0) * float(trade.quantity or 0.0))
    except Exception:
        return 0.0


def _days_held(trade: Trade) -> float:
    """Compute days-held from the trade's open timestamp + reasonable
    fallback to one day so a same-day close still aggregates somewhere."""
    if not trade or not trade.timestamp:
        return 1.0
    try:
        # We don't have an explicit close timestamp on Trade rows yet;
        # use realized_pnl row's recorded `timestamp` as the close
        # marker — the trade row is rewritten on close.
        # Use today as the close baseline; minimum 1 day.
        now = datetime.utcnow()
        delta = now - trade.timestamp
        days = max(0.0, delta.total_seconds() / 86400.0)
        return max(1.0, days)
    except Exception:
        return 1.0


# ── single-trade ingestion ────────────────────────────────────────────


def _existing_live_observation_id(session, trade: Trade) -> Optional[int]:
    """Return the observation_id of any prior live ingest for this trade,
    or None.

    We key on (ticker, pattern, timestamp, timeframe=1d, source ∈
    {live_engine, live_trade}) — both source tags resolve to the same
    live-ingest row so re-running ingest_closed_trade after the Y1 flip
    won't double-write trades that were previously tagged `live_trade`.
    """
    pat = _derive_pattern(trade)
    if not pat:
        return None
    row = session.execute(
        select(MarketObservation.id)
        .where(MarketObservation.ticker == (trade.ticker or "").upper())
        .where(MarketObservation.pattern == pat)
        .where(MarketObservation.timestamp == trade.timestamp)
        .where(MarketObservation.source.in_(_LIVE_SOURCES_FOR_IDEMPOTENCY))
    ).scalar_one_or_none()
    return row


def ingest_closed_trade(trade_id: int) -> Dict[str, Any]:
    """Convert a closed Trade row into a (MarketObservation, MarketOutcome)
    pair tagged ``source='live_trade'``. Idempotent.

    Returns a stats dict {observation_id, outcome_id, skipped, reason}.
    """
    stats: Dict[str, Any] = {
        "trade_id": trade_id, "observation_id": None,
        "outcome_id": None, "skipped": False, "reason": None,
    }
    with session_scope() as s:
        trade = s.get(Trade, int(trade_id))
        if trade is None:
            stats["skipped"] = True
            stats["reason"] = "trade_not_found"
            return stats
        if (trade.status or "").lower() == "open":
            stats["skipped"] = True
            stats["reason"] = "trade_open"
            return stats
        if trade.pnl is None:
            stats["skipped"] = True
            stats["reason"] = "no_pnl"
            return stats

        existing_id = _existing_live_observation_id(s, trade)
        if existing_id is not None:
            stats["skipped"] = True
            stats["reason"] = "already_ingested"
            stats["observation_id"] = existing_id
            return stats

        pat = _derive_pattern(trade)
        if not pat:
            stats["skipped"] = True
            stats["reason"] = "no_pattern"
            return stats

        days = _days_held(trade)
        horizon = _classify_horizon(days)
        cost = _cost_basis(trade)
        if cost <= 0.0:
            # Avoid divide-by-zero when computing return_pct. Use raw
            # P&L as a coarse signed return so the winner flag is still
            # captured.
            return_pct = 1.0 if (trade.pnl or 0) > 0 else (
                -1.0 if (trade.pnl or 0) < 0 else 0.0)
        else:
            return_pct = float(trade.pnl or 0.0) / cost

        # Build the observation row. We store the trade_id + signal_source
        # in features so downstream analytics can isolate by source
        # (data-blame principle). MITS Phase 13 Fix 1 — spec-mandated
        # keys: trade_id, pnl_pct, closed_at, live_weight (=5x via
        # TUNABLES.live_outcome_weight_multiplier).
        try:
            live_weight = float(TUNABLES.live_outcome_weight_multiplier)
        except Exception:
            live_weight = 5.0
        closed_at_iso = None
        try:
            # Trade.timestamp is rewritten to the close moment when the
            # row closes; use it as the canonical closed_at marker.
            closed_at_iso = (trade.timestamp.isoformat()
                             if trade.timestamp else None)
        except Exception:
            closed_at_iso = None
        features = {
            "trade_id": int(trade.id),
            "pnl_pct": float(return_pct),
            "closed_at": closed_at_iso,
            "live_weight": live_weight,
            "signal_source": trade.signal_source,
            "strategy": trade.strategy,
            "instrument": trade.instrument,
            "pnl_dollars": float(trade.pnl or 0.0),
            "cost_basis": float(cost),
            "days_held": float(days),
        }
        # Derive direction from trade action/instrument. Stock buy +
        # long calls / short puts all express bullish bias → "long";
        # stock sell-short + long puts / short calls → "short". Fall
        # back to "long" (the model default) when ambiguous.
        direction = "long"
        try:
            act = (trade.action or "").lower()
            inst = (trade.instrument or "").lower()
            opt = (trade.option_type or "").lower()
            if inst == "option":
                if opt == "put" and act in ("buy", "long"):
                    direction = "short"
                elif opt == "call" and act in ("sell", "short"):
                    direction = "short"
                else:
                    direction = "long"
            else:
                if act in ("sell", "short"):
                    direction = "short"
                else:
                    direction = "long"
        except Exception:
            direction = "long"

        obs = MarketObservation(
            ticker=(trade.ticker or "").upper(),
            pattern=pat,
            timestamp=trade.timestamp or datetime.utcnow(),
            timeframe="1d",
            regime="live",
            vol_state="normal",
            time_bucket="rth",
            spot=float(trade.price or 0.0),
            iv_rank=None,
            gex_state=None,
            features=json.dumps(features),
            source=LIVE_ENGINE_SOURCE,
            direction=direction,
        )
        s.add(obs)
        s.flush()
        logger.info(
            "live_outcome_ingest: trade %d → obs %d (%s/%s, horizon=%s)",
            trade.id, obs.id, obs.ticker, pat, horizon,
        )

        outcome = MarketOutcome(
            observation_id=obs.id,
            horizon=horizon,
            entry_price=float(trade.price or 0.0),
            exit_price=float(trade.price or 0.0) + (
                float(trade.pnl or 0.0) / max(1.0, float(trade.quantity or 1.0))
            ),
            return_pct=float(return_pct),
            was_winner=bool((trade.pnl or 0.0) > 0),
        )
        s.add(outcome)
        s.flush()
        stats["observation_id"] = obs.id
        stats["outcome_id"] = outcome.id
        stats["pattern"] = pat
        stats["horizon"] = horizon

    return stats


# ── nightly batch ─────────────────────────────────────────────────────


def _get_or_create_watermark(session) -> IngestWatermark:
    row = session.execute(
        select(IngestWatermark).where(
            IngestWatermark.source == WATERMARK_SOURCE)
    ).scalar_one_or_none()
    if row is None:
        row = IngestWatermark(
            source=WATERMARK_SOURCE,
            last_ingested_trade_id=0,
            rows_ingested_total=0,
        )
        session.add(row)
        session.flush()
    return row


def _closed_trade_ids_since(session, last_id: int,
                                limit: int = 5000) -> List[int]:
    rows = session.execute(
        select(Trade.id)
        .where(Trade.id > last_id)
        .where(Trade.status.in_(("closed", "filled_closed",
                                   "closed_by_exit_manager",
                                   "closed_by_thesis_health")))
        .where(Trade.pnl.is_not(None))
        .order_by(Trade.id.asc())
        .limit(limit)
    ).scalars().all()
    return list(rows)


def ingest_live_outcomes(*, limit: int = 5000,
                              recompute: bool = True) -> Dict[str, Any]:
    """Nightly batch — walk every Trade closed since the last run and
    ingest. Updates the watermark + (optionally) triggers a corpus
    recompute for the affected (ticker, pattern, regime) tuples.
    """
    stats: Dict[str, Any] = {
        "trades_considered": 0,
        "trades_ingested": 0,
        "trades_skipped": 0,
        "skip_reasons": {},
        "affected_tickers": set(),
        "affected_patterns": set(),
        "recompute_calls": 0,
        "last_ingested_trade_id": 0,
    }
    with session_scope() as s:
        wm = _get_or_create_watermark(s)
        last_id = int(wm.last_ingested_trade_id or 0)
        ids = _closed_trade_ids_since(s, last_id, limit=limit)
    stats["trades_considered"] = len(ids)
    max_id = last_id
    for tid in ids:
        r = ingest_closed_trade(int(tid))
        if r.get("skipped"):
            stats["trades_skipped"] += 1
            reason = r.get("reason") or "unknown"
            stats["skip_reasons"][reason] = (
                stats["skip_reasons"].get(reason, 0) + 1)
        else:
            stats["trades_ingested"] += 1
            # Track which (ticker, pattern) tuples got new evidence so
            # the recompute pass can be scoped.
            # Re-fetch the trade for stats only.
            with session_scope() as s2:
                t = s2.get(Trade, int(tid))
                if t is not None:
                    stats["affected_tickers"].add((t.ticker or "").upper())
                    pat = _derive_pattern(t)
                    if pat:
                        stats["affected_patterns"].add(pat)
        if int(tid) > max_id:
            max_id = int(tid)

    # Update the watermark even when nothing ingested — we processed
    # the rows once and don't want to re-consider them.
    with session_scope() as s:
        wm = _get_or_create_watermark(s)
        wm.last_ingested_trade_id = max_id
        wm.rows_ingested_total = int(wm.rows_ingested_total or 0) + int(
            stats["trades_ingested"])
        wm.last_run_at = datetime.utcnow()
    stats["last_ingested_trade_id"] = max_id

    # Optionally fire a scoped recompute. We call recompute_cells per
    # affected ticker — the aggregator handles cohort partitioning.
    if recompute and stats["trades_ingested"] > 0:
        try:
            from backend.bot.corpus.knowledge_aggregator import recompute_cells
            for tkr in sorted(stats["affected_tickers"]):
                try:
                    recompute_cells(ticker=tkr)
                    stats["recompute_calls"] += 1
                except Exception:
                    logger.debug("recompute_cells(%s) failed", tkr,
                                       exc_info=True)
        except Exception:
            logger.debug("knowledge_aggregator import failed", exc_info=True)

    # Sets aren't JSON-serializable — flatten for the return value.
    stats["affected_tickers"] = sorted(stats["affected_tickers"])
    stats["affected_patterns"] = sorted(stats["affected_patterns"])
    return stats


# ── Beta-Binomial helper used by the knowledge aggregator ────────────


def split_observations_by_provenance(rows: Iterable[Dict[str, Any]]
                                              ) -> Tuple[List[Dict[str, Any]],
                                                              List[Dict[str, Any]]]:
    """Partition aggregator rows into (historical, live) by source."""
    hist: List[Dict[str, Any]] = []
    live: List[Dict[str, Any]] = []
    for r in rows:
        src = (r.get("source") or "").lower()
        if src in {LIVE_ENGINE_SOURCE, LEGACY_LIVE_TRADE_SOURCE}:
            live.append(r)
        else:
            hist.append(r)
    return hist, live


def apply_live_weighted_posterior(
    *,
    historical_n: int,
    historical_wins: int,
    live_n: int,
    live_wins: int,
    prior_wr: float = 0.5,
    prior_weight: float = 10.0,
    live_weight_multiplier: Optional[float] = None,
    live_authoritative_floor: Optional[int] = None,
) -> Dict[str, Any]:
    """Beta-Binomial posterior with live-weight + authoritative-floor.

    Three regimes:

      * live_n >= floor → primary = live posterior (Beta-Binomial with
        prior). Historical kept as secondary.
      * 0 < live_n < floor → primary = blended posterior with live
        observations multiplied by `live_weight_multiplier`.
      * live_n == 0 → primary = historical posterior (no change).

    Returns:
        dict(primary, secondary, mode, live_n, historical_n,
             live_posterior, historical_posterior, combined_posterior)
    """
    mult = float(live_weight_multiplier
                       if live_weight_multiplier is not None
                       else TUNABLES.live_outcome_weight_multiplier)
    floor = int(live_authoritative_floor
                       if live_authoritative_floor is not None
                       else TUNABLES.live_n_authoritative_floor)

    def _post(wins: float, n: float) -> Optional[float]:
        denom = n + prior_weight
        if denom <= 0:
            return None
        return (wins + prior_weight * prior_wr) / denom

    hist_post = _post(historical_wins, historical_n)
    live_post = _post(live_wins, live_n)

    # Blended posterior: historical + live*mult counted together.
    blended_wins = float(historical_wins) + float(live_wins) * mult
    blended_n = float(historical_n) + float(live_n) * mult
    combined_post = _post(blended_wins, blended_n)

    if live_n >= floor and live_n > 0:
        primary = live_post
        secondary = hist_post
        mode = "live_authoritative"
    elif live_n > 0:
        primary = combined_post
        secondary = hist_post
        mode = "live_weighted"
    else:
        primary = hist_post
        secondary = None
        mode = "historical_only"

    return {
        "primary_posterior": primary,
        "secondary_posterior": secondary,
        "mode": mode,
        "live_n": int(live_n),
        "live_wins": int(live_wins),
        "historical_n": int(historical_n),
        "historical_wins": int(historical_wins),
        "live_posterior": live_post,
        "historical_posterior": hist_post,
        "combined_posterior": combined_post,
        "live_weight_multiplier": mult,
        "live_authoritative_floor": floor,
    }


__all__ = [
    "LIVE_ENGINE_SOURCE",
    "LEGACY_LIVE_TRADE_SOURCE",
    "WATERMARK_SOURCE",
    "ingest_closed_trade",
    "ingest_live_outcomes",
    "split_observations_by_provenance",
    "apply_live_weighted_posterior",
]
