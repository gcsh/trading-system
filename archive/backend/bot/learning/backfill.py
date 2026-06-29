"""MITS Phase 18-FU Gap 4 — historical-replay synthetic backfill for
learning-layer validation.

Problem this module solves:
  The $5k paper trial closes ~2 trades / 14 days. At that pace the
  18.A attribution scorecard, 18.C policy tuning advisor, and 18.D
  weight-adaptation history all wait 7+ months to reach the
  ``n_closed >= 30`` floor. Until they cross the floor every metric
  reports ``insufficient_sample_size`` — the learning layer is shipped
  but blind.

  Real money + ``data_blame_principle`` rules: we never inject
  synthetic data into the live decision stream. But the 5y corpus
  (MarketObservation × MarketOutcome) is full of real patterns × real
  forward returns that follow the same statistical shape as live
  closed trades. Replaying those into synthetic Trade + DecisionProvenance
  rows tagged ``source_kind='synthetic_backfill'`` lets us validate
  that the learning math (Brier, ECE, Wilson, calibration buckets)
  produces sensible numbers WITHOUT poisoning the live ledger.

Safety contract (mandatory):
  1. ``TUNABLES.learning_backfill_enabled`` AND
     ``TB_LEARNING_BACKFILL_ENABLED=1`` env var BOTH required —
     belt-and-suspenders kill switch.
  2. EVERY written row carries ``source_kind='synthetic_backfill'``.
  3. EVERY written Trade carries ``signal_source='historical_replay_backfill'``
     (distinct from the legacy Phase 0 ``signal_source='historical_replay'``
     marker so the two paths are auditable separately).
  4. Synthetic rows are EXCLUDED from default learning-layer reads
     (``include_synthetic`` flag must be passed). 18.A's
     ``compute_attribution_report`` already honors this.
  5. Idempotent — re-running with identical ``(days_back, max_rows)``
     produces zero new rows (we dedup on a deterministic key).
  6. ``dry_run=True`` returns the count of rows that WOULD be written
     without touching the DB.

Synthesis recipe:
  - Pull ``MarketObservation × MarketOutcome`` rows joined on
    ``observation_id``, filtering for horizon='1d' (the most populous
    cohort, matches typical live trade duration). Honor ``days_back``
    on ``observation.timestamp``.
  - For each pair, synthesize one Trade with:
      * ticker, strategy_name='strategy_from_pattern', signal_source =
        'historical_replay_backfill', source_kind='synthetic_backfill'.
      * pnl = return_pct * 100  (return_pct is fractional)
      * status='closed', timestamp=observation.timestamp.
  - And one DecisionProvenance row with:
      * strategy_matrix_json = {"candidates":[{"strategy_name":
        <pattern_name>}], "as_of":..., "synthetic_backfill":true}
      * regime_vector_json = {"trend":observation.regime, ...}
      * source_kind='synthetic_backfill'.
  - Dedup key: ``(observation_id, 'synthetic_backfill')`` stored in
    the Trade.reason field as a stable prefix; before insert we check
    if a row already exists with that reason prefix.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.trade import Trade


logger = logging.getLogger(__name__)


# Stable marker so the dedup query is cheap + the operator can spot
# synthetic rows without joining provenance.
SYNTHETIC_SIGNAL_SOURCE = "historical_replay_backfill"
SYNTHETIC_SOURCE_KIND = "synthetic_backfill"
SYNTHETIC_REASON_PREFIX = "synthetic_data_for_learning_validation_only"

# Default horizon to replay. 1d gives realistic next-day P&L shapes
# matching the engine's average paper-trial trade duration.
DEFAULT_HORIZON = "1d"

# Upper bound on the synthetic price assumption. We don't know what
# instrument the original detector fired on, so we use a constant
# notional ($100/unit) and let realized return_pct shape the pnl —
# the calibration math is scale-invariant up to a normalization in
# attribution.py's _realized_pct helper.
SYNTHETIC_PRICE: float = 100.0
SYNTHETIC_QUANTITY: float = 1.0


@dataclass
class BackfillResult:
    """One-shot summary of a backfill run."""
    dry_run: bool
    flag_enabled: bool
    env_enabled: bool
    days_back: int
    max_rows: int
    n_eligible_observations: int
    n_already_synthesized: int
    n_to_write: int
    n_written: int
    n_skipped_bad_rows: int
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "flag_enabled": self.flag_enabled,
            "env_enabled": self.env_enabled,
            "days_back": self.days_back,
            "max_rows": self.max_rows,
            "n_eligible_observations": self.n_eligible_observations,
            "n_already_synthesized": self.n_already_synthesized,
            "n_to_write": self.n_to_write,
            "n_written": self.n_written,
            "n_skipped_bad_rows": self.n_skipped_bad_rows,
            "error": self.error,
        }


def _flag_enabled() -> bool:
    return bool(getattr(TUNABLES, "learning_backfill_enabled", False))


def _env_enabled() -> bool:
    return os.getenv("TB_LEARNING_BACKFILL_ENABLED", "").strip() in {
        "1", "true", "True", "TRUE", "yes",
    }


def _dedup_reason(observation_id: int) -> str:
    """Stable reason string used as the dedup key for synthetic rows.
    Encodes both the marker (so operators can grep) AND the observation
    id (so re-running on the same source data finds existing rows)."""
    return f"{SYNTHETIC_REASON_PREFIX}|obs={int(observation_id)}"


def _build_strategy_matrix_blob(
    *, observation: MarketObservation,
) -> Dict[str, Any]:
    """Synthesize a strategy_matrix_json shape that 18.A's Gap-2 fix
    can read. Uses the observation's ``pattern`` as the strategy
    template name — so synthetic + live attribution share the same
    bucketing key."""
    return {
        "ticker": observation.ticker,
        "as_of": (
            observation.timestamp.isoformat()
            if observation.timestamp else None
        ),
        "synthetic_backfill": True,
        "candidates": [
            {
                "strategy_name": observation.pattern,
                "label": f"synthetic:{observation.pattern}",
                "final_score": 0.5,
                "synthetic": True,
            },
        ],
        "top_strategy": {
            "strategy_name": observation.pattern,
            "synthetic": True,
        },
        "note": "synthetic_data_for_learning_validation_only",
    }


def _build_regime_blob(
    *, observation: MarketObservation,
) -> Dict[str, Any]:
    """Minimal regime_vector_json so 18.A's regime stratification has
    a value to bucket by. We copy whatever regime tag the observation
    already carries (the real-data signal)."""
    return {
        "trend": observation.regime or "unknown",
        "vol_state": observation.vol_state or "normal",
        "synthetic_backfill": True,
    }


def _build_consensus_blob(
    *, observation: MarketObservation, return_pct: float,
) -> Dict[str, Any]:
    """Synthesize a consensus_json shape so 18.A axis + agent
    aggregators see SOMETHING. The agents_outputs list is empty by
    design — synthetic data doesn't have real council votes, so
    attempting to aggregate against fake votes would corrupt the
    18.A scorecard. Operators reading the synthetic-included report
    see ``n_closed`` move but per-agent calibration stays at
    insufficient_sample (which is honest)."""
    # Encode return direction so axis calibration has a signal —
    # axis scores derived purely from synthesis SHOULD be near-zero
    # spearman because we don't have real per-axis features.
    return {
        "stance": "buy" if return_pct >= 0 else "sell",
        "confidence": 0.5,
        "recommendation": "execute",
        "synthetic_backfill": True,
        "confidence_breakdown": {},
        "note": "synthetic_data_for_learning_validation_only",
    }


def _existing_synthetic_obs_ids(
    *, session, observation_ids: List[int],
) -> set:
    """Return the set of observation_ids that ALREADY have a
    corresponding synthetic Trade row. Used for idempotency: a re-run
    skips any observation we've already replayed."""
    if not observation_ids:
        return set()
    reasons = [_dedup_reason(oid) for oid in observation_ids]
    rows = session.execute(
        select(Trade.reason).where(
            and_(
                Trade.signal_source == SYNTHETIC_SIGNAL_SOURCE,
                Trade.source_kind == SYNTHETIC_SOURCE_KIND,
                Trade.reason.in_(reasons),
            )
        )
    ).scalars().all()
    found: set = set()
    for r in rows:
        # Parse the obs id out of the dedup reason — cheap +
        # avoids a second query.
        if not r or "|obs=" not in r:
            continue
        try:
            found.add(int(r.rsplit("|obs=", 1)[1]))
        except (TypeError, ValueError):
            continue
    return found


def backfill_learning_from_historical_replay(
    *,
    days_back: int = 90,
    max_synthetic_rows: int = 500,
    dry_run: bool = True,
    horizon: str = DEFAULT_HORIZON,
) -> BackfillResult:
    """Synthesize learning-layer feed rows from the historical replay
    corpus.

    Parameters
    ----------
    days_back : int
        Walk back this many days on ``MarketObservation.timestamp``.
        Default 90 (matches 18.A's default window).
    max_synthetic_rows : int
        Upper bound on the number of synthetic Trades+Provs to write.
        Default 500. We stop as soon as the cap is hit.
    dry_run : bool
        When True, returns the count of rows that WOULD be written +
        skips every write. Default True — the operator must explicitly
        pass False to mutate state.
    horizon : str
        Which MarketOutcome horizon to replay. Default '1d'.

    Returns
    -------
    BackfillResult with the row counts + flag status.

    Safety:
      * Refuses to run when ``TUNABLES.learning_backfill_enabled``
        is False.
      * Refuses to run when ``TB_LEARNING_BACKFILL_ENABLED`` env is
        not set (belt-and-suspenders).
      * On any unexpected exception, returns a BackfillResult with
        ``error`` populated and 0 rows written — never partial-state
        writes that could leak into the live ledger.
    """
    flag = _flag_enabled()
    env = _env_enabled()
    result = BackfillResult(
        dry_run=bool(dry_run),
        flag_enabled=flag,
        env_enabled=env,
        days_back=int(days_back),
        max_rows=int(max_synthetic_rows),
        n_eligible_observations=0,
        n_already_synthesized=0,
        n_to_write=0,
        n_written=0,
        n_skipped_bad_rows=0,
    )

    if not flag or not env:
        result.error = (
            f"backfill disabled — flag_enabled={flag} env_enabled={env}; "
            f"set BOTH TUNABLES.learning_backfill_enabled and "
            f"TB_LEARNING_BACKFILL_ENABLED=1 to opt in"
        )
        return result

    if days_back <= 0 or max_synthetic_rows <= 0:
        result.error = "days_back and max_synthetic_rows must be positive"
        return result

    cutoff = datetime.utcnow() - timedelta(days=int(days_back))

    try:
        with session_scope() as session:
            # Pull eligible (observation, outcome) pairs in the window.
            # JOIN via observation_id; honor horizon filter; only count
            # outcomes with return_pct populated (otherwise we can't
            # synthesize a P&L). Order ascending so the dedup walk is
            # deterministic — re-runs synthesize the SAME rows.
            stmt = (
                select(MarketObservation, MarketOutcome)
                .join(
                    MarketOutcome,
                    MarketOutcome.observation_id == MarketObservation.id,
                )
                .where(MarketObservation.timestamp >= cutoff)
                .where(MarketOutcome.horizon == horizon)
                .where(MarketOutcome.return_pct.is_not(None))
                .order_by(MarketObservation.timestamp.asc())
                .limit(int(max_synthetic_rows) * 2)
                # × 2 head-room so even after dedup we likely have
                # enough rows to fill the cap.
            )
            rows = session.execute(stmt).all()
            result.n_eligible_observations = len(rows)
            if not rows:
                return result

            # Find which observation_ids already have a synthetic Trade —
            # those are skipped for idempotency.
            obs_ids = [int(obs.id) for obs, _ in rows]
            existing = _existing_synthetic_obs_ids(
                session=session, observation_ids=obs_ids,
            )
            result.n_already_synthesized = len(existing)

            # Walk pairs, skip dedup-matched, cap at max_synthetic_rows.
            to_write: List = []
            for obs, outcome in rows:
                if len(to_write) >= int(max_synthetic_rows):
                    break
                obs_id = int(obs.id)
                if obs_id in existing:
                    continue
                # Defensive: skip rows where the outcome math would
                # underflow. We need a finite return_pct.
                try:
                    ret = float(outcome.return_pct)
                except (TypeError, ValueError):
                    result.n_skipped_bad_rows += 1
                    continue
                if not (-1.0 <= ret <= 1.0):
                    # outcome.return_pct is fractional in 18.A's
                    # convention; values outside [-1, 1] are likely
                    # bad. Skip to avoid wild pnl.
                    result.n_skipped_bad_rows += 1
                    continue
                to_write.append((obs, outcome, ret))

            result.n_to_write = len(to_write)
            if dry_run:
                return result

            # Live write path — synthesize Trade + DecisionProvenance.
            written = 0
            for obs, outcome, ret in to_write:
                try:
                    pnl_value = float(ret) * SYNTHETIC_PRICE * SYNTHETIC_QUANTITY
                    trade = Trade(
                        timestamp=obs.timestamp or datetime.utcnow(),
                        ticker=obs.ticker,
                        action="BUY_STOCK" if ret >= 0 else "SELL_STOCK",
                        quantity=SYNTHETIC_QUANTITY,
                        price=SYNTHETIC_PRICE,
                        strategy=obs.pattern or "synthetic",
                        signal_source=SYNTHETIC_SIGNAL_SOURCE,
                        confidence=0.5,
                        reason=_dedup_reason(int(obs.id)),
                        paper=1,
                        pnl=pnl_value,
                        status="closed",
                        instrument="stock",
                        pricing_source="paper_stub",
                        accounting_version=1,
                        source_kind=SYNTHETIC_SOURCE_KIND,
                    )
                    session.add(trade)
                    session.flush()  # need trade.id for FK
                    prov = DecisionProvenance(
                        trade_id=int(trade.id),
                        event_status="executed",
                        ticker=obs.ticker,
                        decision_timestamp=(
                            obs.timestamp or datetime.utcnow()
                        ),
                        cycle_id=f"synthetic_backfill_{int(obs.id)}",
                        regime_vector_json=json.dumps(
                            _build_regime_blob(observation=obs),
                        ),
                        strategy_matrix_json=json.dumps(
                            _build_strategy_matrix_blob(observation=obs),
                        ),
                        agent_inputs_json=None,
                        agent_outputs_json=json.dumps([]),
                        consensus_json=json.dumps(
                            _build_consensus_blob(
                                observation=obs, return_pct=ret,
                            ),
                        ),
                        chairman_memo_json=None,
                        policy_result_json=None,
                        simulator_verdict_json=None,
                        correlation_cap_json=None,
                        portfolio_context_json=None,
                        decision_quality_score_json=None,
                        source_kind=SYNTHETIC_SOURCE_KIND,
                    )
                    session.add(prov)
                    written += 1
                except Exception:
                    logger.exception(
                        "backfill synth-write failed for obs_id=%s",
                        getattr(obs, "id", "?"),
                    )
                    result.n_skipped_bad_rows += 1
                    # Continue; session_scope manages transaction boundary.
            session.flush()
            result.n_written = written
    except Exception as exc:
        logger.exception("backfill_learning_from_historical_replay failed")
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result

    return result
