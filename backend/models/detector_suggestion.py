"""MITS Phase 6 (P6.3) — DetectorSuggestion.

Self-disabling detectors (suggest, don't force). The nightly
`_detector_suggestions_pass` walks every detector's out-of-sample
posterior and creates a DetectorSuggestion row whenever:

  * a currently-enabled detector's posterior drops below
    `TUNABLES.detector_suggest_disable_posterior` (default 0.45) AND
    its sample size is at least `detector_suggest_disable_min_n`
    (default 100). The suggestion's `reason` is `low_posterior`.

  * a currently-DISABLED detector's recent live posterior climbs back
    above `TUNABLES.detector_suggest_reenable_posterior` (default
    0.60). The suggestion's `reason` is `recovered_posterior`.

The operator resolves each suggestion via POST endpoints:
  * `accept` — flips DetectorConfig.enabled, marks suggestion accepted.
  * `dismiss` — marks suggestion dismissed; a fresh suggestion for the
    same detector won't be created for
    `TUNABLES.detector_suggestion_cooldown_days` (default 14d).

Idempotent: there's at most one `pending` row per (detector_name,
reason_family) tuple. The nightly pass calls `find_pending_for_detector`
before inserting.

This row is *external-cache-shaped* (derived from corpus posteriors).
It survives `fresh_start` so the operator's accepted/dismissed history
is preserved across paper-trial resets.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


# Canonical status strings.
SUGGESTION_STATUS_PENDING = "pending"
SUGGESTION_STATUS_ACCEPTED = "accepted"
SUGGESTION_STATUS_DISMISSED = "dismissed"

# Canonical reasons.
REASON_LOW_POSTERIOR = "low_posterior"
REASON_RECOVERED_POSTERIOR = "recovered_posterior"


class DetectorSuggestion(Base):
    __tablename__ = "detector_suggestions"
    __table_args__ = (
        Index("ix_det_suggestion_status",
                "detector_name", "status"),
        Index("ix_det_suggestion_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detector_name: Mapped[str] = mapped_column(String, index=True)
    # One of REASON_*. `low_posterior` recommends disable;
    # `recovered_posterior` recommends re-enable.
    reason: Mapped[str] = mapped_column(String, default=REASON_LOW_POSTERIOR)
    # Snapshot of the posterior + N at the moment we fired the
    # suggestion. Lets the UI explain the rationale even if the cohort
    # has since shifted.
    out_of_sample_posterior: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String, default=SUGGESTION_STATUS_PENDING, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "detector_name": self.detector_name,
            "reason": self.reason,
            "out_of_sample_posterior": self.out_of_sample_posterior,
            "sample_size": self.sample_size,
            "status": self.status,
            "created_at": (self.created_at.isoformat()
                                  if self.created_at else None),
            "resolved_at": (self.resolved_at.isoformat()
                                  if self.resolved_at else None),
        }
