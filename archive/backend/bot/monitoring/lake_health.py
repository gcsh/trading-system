"""MITS Phase 9.5 — Lake health monitor logic.

Hourly cron job (registered in ``backend/bot/scheduler.py``) reads
``/lake/status``-equivalent stats from ``backend.bot.data.lake`` +
``backend.bot.ai.vector_store`` and writes a ``LakeHealthAlert`` row
whenever a threshold trips. Subsequent passes auto-resolve any
active alert whose condition has cleared.

Rules implemented (all tunables in ``backend/config.py:TUNABLES``):

  * ``bronze_stale`` — newest bronze object older than
    ``lake_alert_bronze_stale_hours`` (default 24h).
  * ``gold_stale`` — newest gold object older than
    ``lake_alert_gold_stale_hours`` (default 48h).
  * ``vector_shrink`` — vector total count fell vs. the previous
    snapshot (vectors are append-only; a drop is always anomalous).
  * ``write_failures`` — counter of failed bronze writes in the past
    24h exceeds ``lake_alert_write_failure_threshold`` (default 10).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.data import lake
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.lake_health_alert import LakeHealthAlert


logger = logging.getLogger(__name__)


# Last-seen vector count, kept in process memory so we can detect a
# shrink across consecutive passes without persisting it.
_LAST_VECTOR_COUNT: Optional[int] = None


# Counter of bronze-write failures since process boot. Other modules
# can bump it via ``record_bronze_failure()`` when they fail to write
# to S3. We deliberately keep it in-process rather than persisting —
# the alert is meant to surface "noisy day" patterns; a long-lived
# failure pattern will keep being reported across boots because the
# threshold is small.
_BRONZE_WRITE_FAILURES: List[datetime] = []


def record_bronze_failure() -> None:
    """Public helper so any module that writes to bronze can pop a
    counter increment in case the write fails. Cheap; bounded."""
    _BRONZE_WRITE_FAILURES.append(datetime.now(timezone.utc))
    # Keep at most ~1000 entries; older entries fall off naturally
    # because the count helper trims to the last 24h.
    if len(_BRONZE_WRITE_FAILURES) > 1000:
        del _BRONZE_WRITE_FAILURES[:200]


def _failures_last_24h() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return sum(1 for t in _BRONZE_WRITE_FAILURES if t >= cutoff)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        s = str(ts)
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        out = datetime.fromisoformat(s)
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out
    except Exception:
        return None


def _layer_age_hours(stat) -> Optional[float]:
    dt = _parse_iso(getattr(stat, "last_modified", None))
    if dt is None:
        return None
    age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    return max(0.0, age)


@dataclass
class HealthCheckResult:
    """Aggregated outcome of one pass. Returned for tests / cron logs."""
    fired: List[Dict[str, Any]]
    auto_resolved: List[int]
    samples: Dict[str, Any]


def _check_bronze(samples: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    stat = lake.stat_layer("bronze")
    samples["bronze"] = {
        "bytes": stat.bytes,
        "object_count": stat.object_count,
        "last_modified": stat.last_modified,
    }
    age = _layer_age_hours(stat)
    if age is None:
        return None
    threshold = float(TUNABLES.lake_alert_bronze_stale_hours)
    samples["bronze_age_hours"] = round(age, 2)
    if age > threshold:
        return {
            "kind": "bronze_stale",
            "severity": "warning",
            "detail": {
                "age_hours": round(age, 2),
                "threshold_hours": threshold,
                "last_modified": stat.last_modified,
            },
        }
    return None


def _check_gold(samples: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    stat = lake.stat_layer("gold")
    samples["gold"] = {
        "bytes": stat.bytes,
        "object_count": stat.object_count,
        "last_modified": stat.last_modified,
    }
    age = _layer_age_hours(stat)
    if age is None:
        return None
    threshold = float(TUNABLES.lake_alert_gold_stale_hours)
    samples["gold_age_hours"] = round(age, 2)
    if age > threshold:
        return {
            "kind": "gold_stale",
            "severity": "warning",
            "detail": {
                "age_hours": round(age, 2),
                "threshold_hours": threshold,
                "last_modified": stat.last_modified,
            },
        }
    return None


def _check_vectors(samples: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    global _LAST_VECTOR_COUNT
    total = 0
    try:
        from backend.bot.ai import vector_store
        stats = vector_store.namespace_stats() or {}
        # Accept either a dict-of-counts or a flat int.
        if isinstance(stats, dict):
            counts = []
            for v in stats.values():
                if isinstance(v, dict):
                    n = int(v.get("count") or v.get("total") or 0)
                else:
                    try:
                        n = int(v)
                    except Exception:
                        n = 0
                counts.append(n)
            total = sum(counts)
        elif isinstance(stats, (int, float)):
            total = int(stats)
    except Exception:
        return None
    samples["vector_total"] = total
    prev = _LAST_VECTOR_COUNT
    _LAST_VECTOR_COUNT = total
    if prev is not None and total < prev:
        return {
            "kind": "vector_shrink",
            "severity": "danger",
            "detail": {
                "previous_total": prev,
                "current_total": total,
                "delta": total - prev,
            },
        }
    return None


def _check_write_failures(samples: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    count = _failures_last_24h()
    samples["bronze_write_failures_24h"] = count
    threshold = int(TUNABLES.lake_alert_write_failure_threshold)
    if count > threshold:
        return {
            "kind": "write_failures",
            "severity": "warning",
            "detail": {
                "count_24h": count,
                "threshold": threshold,
            },
        }
    return None


# ── pass orchestrator ────────────────────────────────────────────────


def _serialize_detail(detail: Dict[str, Any]) -> str:
    return json.dumps(detail, default=str, sort_keys=True)


def run_health_check() -> HealthCheckResult:
    """Run one health-check pass. Idempotent: opening alerts dedupe on
    ``kind``; clearing alerts simply marks them resolved.
    """
    samples: Dict[str, Any] = {}
    candidates: List[Dict[str, Any]] = []
    for fn in (_check_bronze, _check_gold, _check_vectors, _check_write_failures):
        try:
            row = fn(samples)
            if row is not None:
                candidates.append(row)
        except Exception:
            logger.debug("lake-health check %s failed", fn.__name__, exc_info=True)

    fired: List[Dict[str, Any]] = []
    auto_resolved: List[int] = []
    with session_scope() as session:
        active = session.execute(
            select(LakeHealthAlert).where(LakeHealthAlert.resolved_at.is_(None))
        ).scalars().all()
        active_by_kind = {a.kind: a for a in active}

        for row in candidates:
            kind = row["kind"]
            if kind in active_by_kind:
                # Refresh detail; do not duplicate.
                ex = active_by_kind.pop(kind)
                ex.detail_json = _serialize_detail(row["detail"])
                ex.severity = row["severity"]
                continue
            alert = LakeHealthAlert(
                kind=kind,
                severity=row["severity"],
                detail_json=_serialize_detail(row["detail"]),
            )
            session.add(alert)
            session.flush()
            fired.append(alert.to_dict())

        # Any previously active alert whose kind is no longer
        # candidate gets auto-resolved.
        for kind, alert in active_by_kind.items():
            alert.resolved_at = datetime.utcnow()
            alert.resolved_by = "auto"
            auto_resolved.append(alert.id)

    return HealthCheckResult(
        fired=fired, auto_resolved=auto_resolved, samples=samples,
    )


__all__ = [
    "HealthCheckResult", "record_bronze_failure", "run_health_check",
]
