"""MITS Phase 6 (P6.3) — Self-disabling detector suggestion engine.

Walks every detector's out-of-sample posterior. For each:

  * If currently ENABLED and posterior < TUNABLES.detector_suggest_
    disable_posterior AND sample_size > TUNABLES.detector_suggest_
    disable_min_n → create a DetectorSuggestion (reason=low_posterior)
    when no unresolved suggestion exists for this detector.

  * If currently DISABLED and live posterior >= TUNABLES.detector_
    suggest_reenable_posterior AND sample_size >=
    detector_suggest_reenable_min_n → create a suggestion
    (reason=recovered_posterior) so the operator can re-enable.

Cooldown: when a suggestion is dismissed, no new low_posterior
suggestion is generated for the same detector within
TUNABLES.detector_suggestion_cooldown_days (default 14d).
recovered_posterior suggestions are NOT subject to the cooldown — when
edge returns, we want to know immediately.

Idempotent. Safe to re-run multiple times per day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.detector_config import DetectorConfig
from backend.models.detector_suggestion import (
    DetectorSuggestion,
    REASON_LOW_POSTERIOR,
    REASON_RECOVERED_POSTERIOR,
    SUGGESTION_STATUS_ACCEPTED,
    SUGGESTION_STATUS_DISMISSED,
    SUGGESTION_STATUS_PENDING,
)
from backend.models.knowledge_graph_cell import KnowledgeGraphCell

logger = logging.getLogger(__name__)


def _enabled_state(session, detector_name: str) -> bool:
    """Default-true: detectors with no DetectorConfig row are treated
    as enabled (matches the runtime behaviour)."""
    row = session.execute(
        select(DetectorConfig).where(
            DetectorConfig.name == detector_name)
    ).scalar_one_or_none()
    if row is None:
        return True
    return bool(row.enabled)


def _largest_out_of_sample_cell(session, detector_name: str
                                              ) -> Optional[KnowledgeGraphCell]:
    """Find the cell with the largest sample_size for this detector in
    the out_of_sample split. We don't pin to a specific (ticker, regime)
    cohort because the suggestion is detector-wide.
    """
    return session.execute(
        select(KnowledgeGraphCell)
        .where(KnowledgeGraphCell.pattern == detector_name)
        .where(KnowledgeGraphCell.sample_split == "out_of_sample")
        .order_by(KnowledgeGraphCell.sample_size.desc())
        .limit(1)
    ).scalar_one_or_none()


def _has_unresolved_suggestion(session, detector_name: str,
                                          reason: str) -> bool:
    row = session.execute(
        select(DetectorSuggestion)
        .where(DetectorSuggestion.detector_name == detector_name)
        .where(DetectorSuggestion.reason == reason)
        .where(DetectorSuggestion.status == SUGGESTION_STATUS_PENDING)
        .limit(1)
    ).scalar_one_or_none()
    return row is not None


def _in_dismissed_cooldown(session, detector_name: str, reason: str,
                                       cooldown_days: int) -> bool:
    """Return True when a dismissed suggestion for this detector + reason
    was resolved within the cooldown window."""
    if cooldown_days <= 0:
        return False
    cutoff = datetime.utcnow() - timedelta(days=cooldown_days)
    row = session.execute(
        select(DetectorSuggestion)
        .where(DetectorSuggestion.detector_name == detector_name)
        .where(DetectorSuggestion.reason == reason)
        .where(DetectorSuggestion.status == SUGGESTION_STATUS_DISMISSED)
        .where(DetectorSuggestion.resolved_at >= cutoff)
        .order_by(DetectorSuggestion.resolved_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row is not None


def _create_suggestion(session, detector_name: str, reason: str,
                              posterior: Optional[float],
                              sample_size: Optional[int]) -> DetectorSuggestion:
    row = DetectorSuggestion(
        detector_name=detector_name,
        reason=reason,
        out_of_sample_posterior=posterior,
        sample_size=sample_size,
        status=SUGGESTION_STATUS_PENDING,
    )
    session.add(row)
    session.flush()
    return row


def _candidate_detector_names() -> List[str]:
    """Every name with a DetectorConfig row OR a registered detector OR
    an out_of_sample knowledge cell. Dedupes."""
    names: set = set()
    try:
        from backend.bot.detectors import all_detectors
        for d in all_detectors():
            names.add(d.pattern)
    except Exception:
        logger.debug("detector registry unavailable", exc_info=True)
    with session_scope() as s:
        for r in s.execute(
            select(DetectorConfig.name)).scalars().all():
            if r:
                names.add(r)
        for r in s.execute(
            select(KnowledgeGraphCell.pattern)
            .where(KnowledgeGraphCell.sample_split == "out_of_sample")
        ).scalars().all():
            if r:
                names.add(r)
    return sorted(names)


def run_suggestions_pass() -> Dict[str, Any]:
    """Nightly entry point. Returns a stats dict."""
    stats: Dict[str, Any] = {
        "detectors_considered": 0,
        "low_posterior_suggested": 0,
        "low_posterior_skipped_cooldown": 0,
        "low_posterior_skipped_existing": 0,
        "recovered_suggested": 0,
        "recovered_skipped_existing": 0,
        "no_cell": 0,
    }
    disable_post = float(TUNABLES.detector_suggest_disable_posterior)
    disable_min_n = int(TUNABLES.detector_suggest_disable_min_n)
    reenable_post = float(TUNABLES.detector_suggest_reenable_posterior)
    reenable_min_n = int(TUNABLES.detector_suggest_reenable_min_n)
    cooldown_days = int(TUNABLES.detector_suggestion_cooldown_days)

    names = _candidate_detector_names()
    stats["detectors_considered"] = len(names)

    with session_scope() as s:
        for name in names:
            cell = _largest_out_of_sample_cell(s, name)
            if cell is None:
                stats["no_cell"] += 1
                continue
            posterior = float(cell.posterior_win_rate
                                       if cell.posterior_win_rate is not None
                                       else 0.0)
            sample_size = int(cell.sample_size or 0)
            enabled = _enabled_state(s, name)
            if enabled:
                # Low-posterior path → suggest disable.
                if (posterior < disable_post and
                          sample_size > disable_min_n):
                    if _has_unresolved_suggestion(
                            s, name, REASON_LOW_POSTERIOR):
                        stats["low_posterior_skipped_existing"] += 1
                        continue
                    if _in_dismissed_cooldown(
                            s, name, REASON_LOW_POSTERIOR, cooldown_days):
                        stats["low_posterior_skipped_cooldown"] += 1
                        continue
                    _create_suggestion(s, name, REASON_LOW_POSTERIOR,
                                              posterior, sample_size)
                    stats["low_posterior_suggested"] += 1
            else:
                # Recovered path → suggest re-enable.
                if (posterior > reenable_post and
                          sample_size >= reenable_min_n):
                    if _has_unresolved_suggestion(
                            s, name, REASON_RECOVERED_POSTERIOR):
                        stats["recovered_skipped_existing"] += 1
                        continue
                    _create_suggestion(s, name, REASON_RECOVERED_POSTERIOR,
                                              posterior, sample_size)
                    stats["recovered_suggested"] += 1
    return stats


__all__ = ["run_suggestions_pass"]
