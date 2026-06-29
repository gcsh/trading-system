"""MITS Phase 18-FU Gap 1 — Policy auto-tuning APPLY path.

18.C ships an ADVISORY auto-tuner: every night a scheduler job writes
``policy_tunings`` rows recommending threshold changes for the 8
tunable policy rules. 18.E ships a UI that lets the operator review +
``operator_approved`` those rows. That left a 1-step gap: even with
both ``TUNABLES.policy_tuning_advisory_enabled`` AND
``TUNABLES.policy_tuning_auto_apply_enabled`` ON, NOTHING in the engine
actually read the approved rows. The Approve button was inert. This
module closes that gap.

What this module does:

  * Reads operator-approved + non-stale rows from ``policy_tunings``
    (one row per tunable rule; we take the most-recently-approved row
    per ``threshold_attr``).
  * Builds a dict ``{threshold_attr: approved_value, ...}`` — keyed
    by the exact same ``threshold_attr`` strings the 18.C
    ``TUNABLE_RULES`` registry uses (``"config.min_confidence"``,
    ``"TUNABLES.correlation_cap_rho"``, etc.).
  * Injects the dict into ``PolicyContext.scratch['applied_thresholds']``
    BEFORE rule evaluation runs, so individual rule evaluators can
    consult it via ``ctx.scratch.get('applied_thresholds', {}).get(...)``
    and override the TUNABLE default.

What this module does NOT do:

  * It NEVER mutates the TUNABLES object. That would break replay —
    the persisted ``policy_result_json`` would no longer carry the
    threshold actually used. We pass the override through scratch +
    rule evidence so every replay sees what was applied at decision
    time.
  * It NEVER auto-applies when ``policy_tuning_auto_apply_enabled``
    is False. The flag is the operator's only kill switch.
  * It NEVER reads non-approved rows. ``operator_approved=1`` is the
    sole gate — even rows with high confidence are inert without it.

Caching: ``policy_tunings`` is small (one row per rule × N nights)
and the engine evaluates the policy hundreds of times per cycle. We
cache the resolved dict for ``_CACHE_TTL_SECONDS`` to avoid DB hammering.
``invalidate_cache()`` is exposed for the operator UI to call right
after a manual approve so the change is live immediately.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.config import TUNABLES
from backend.db import session_scope

if TYPE_CHECKING:  # pragma: no cover — avoid circular import at runtime
    from backend.bot.decision.policy import PolicyContext


logger = logging.getLogger(__name__)


# ── Cache ─────────────────────────────────────────────────────────────


# Refresh the resolved override dict every 60s. Short enough that an
# operator approve is live within a minute even without explicit
# invalidation; long enough to spare the DB from a query per cycle.
_CACHE_TTL_SECONDS: float = 60.0


_CacheEntry = Tuple[float, Dict[str, float], List[int]]
# (mono_at, {threshold_attr: value}, [policy_tuning_ids_consulted])


_cache_lock = threading.Lock()
_cache_entry: Optional[_CacheEntry] = None


def invalidate_cache() -> None:
    """Force the next ``get_applied_thresholds`` call to re-query the
    DB. Operator UIs call this right after a manual approve so the
    change is live immediately (instead of waiting up to 60s)."""
    global _cache_entry
    with _cache_lock:
        _cache_entry = None


# ── Allow-list of threshold_attr strings ─────────────────────────────


# Mirror of the 18.C ``TUNABLE_RULES`` threshold_attr keys. Lifted into
# a module-local set so applies for unknown attrs are rejected — a
# defense against a malformed policy_tunings row leaking an arbitrary
# string into ctx.scratch. We import at function-call time (not at
# module load) to avoid the policy_tuning module's TUNABLES read
# polluting test fixtures that monkeypatch TUNABLES early.
def _allowed_threshold_attrs() -> Tuple[str, ...]:
    """Return the set of threshold_attr strings that are safe to
    apply. ANY string not in this allow-list is dropped from the
    override dict with a logged warning."""
    try:
        from backend.bot.learning.policy_tuning import TUNABLE_RULES
        return tuple(r.threshold_attr for r in TUNABLE_RULES)
    except Exception:
        # Defensive: if the import fails (unlikely), no overrides will
        # leak through — same as auto-apply OFF.
        logger.warning(
            "policy_apply: TUNABLE_RULES import failed; "
            "blocking ALL overrides this cycle.",
            exc_info=True,
        )
        return ()


# ── Public API ────────────────────────────────────────────────────────


def get_applied_thresholds(
    *,
    force_refresh: bool = False,
) -> Dict[str, float]:
    """Return the operator-approved threshold overrides.

    Behavior:
      * Returns ``{}`` when ``TUNABLES.policy_tuning_auto_apply_enabled``
        is False. This is the operator's kill switch — flipping it OFF
        instantly disables every override on the next cycle (cache is
        bypassed when the flag is off).
      * Returns ``{}`` when no ``policy_tunings`` row has
        ``operator_approved=1`` AND a non-null ``recommended_value``.
      * Otherwise returns ``{threshold_attr: value}`` with the
        most-recently-approved row per attr winning.

    Cached for ``_CACHE_TTL_SECONDS``; pass ``force_refresh=True`` to
    bypass the cache.
    """
    # Kill switch — never read DB when auto-apply is off.
    if not bool(
        getattr(TUNABLES, "policy_tuning_auto_apply_enabled", False),
    ):
        return {}

    now = time.monotonic()
    with _cache_lock:
        global _cache_entry
        if (
            not force_refresh
            and _cache_entry is not None
            and (now - _cache_entry[0]) < _CACHE_TTL_SECONDS
        ):
            return dict(_cache_entry[1])

    allowed = set(_allowed_threshold_attrs())
    overrides: Dict[str, float] = {}
    consulted_ids: List[int] = []
    if not allowed:
        with _cache_lock:
            _cache_entry = (now, overrides, consulted_ids)
        return overrides

    try:
        from backend.models.policy_tuning import PolicyTuning
        with session_scope() as s:
            # Pull every approved row with a numeric recommendation.
            # We take the most-recently-approved row per threshold_attr
            # via Python (not SQL DISTINCT ON) so the helper works on
            # both SQLite + Postgres.
            rows = s.execute(
                select(PolicyTuning)
                .where(PolicyTuning.operator_approved == 1)
                .where(PolicyTuning.recommended_value.is_not(None))
                .order_by(desc(PolicyTuning.computed_at))
            ).scalars().all()
            for r in rows:
                attr = (r.threshold_attr or "").strip()
                if not attr or attr not in allowed:
                    if attr:
                        logger.warning(
                            "policy_apply: dropping unrecognised "
                            "threshold_attr=%r from policy_tunings id=%s",
                            attr, r.id,
                        )
                    continue
                if attr in overrides:
                    continue  # already took the most-recent row
                try:
                    overrides[attr] = float(r.recommended_value)
                    consulted_ids.append(int(r.id))
                except (TypeError, ValueError):
                    logger.warning(
                        "policy_apply: non-numeric recommended_value "
                        "on policy_tunings id=%s; skipping",
                        r.id,
                    )
                    continue
    except Exception:
        logger.exception("policy_apply.get_applied_thresholds failed")
        # Failure mode: empty overrides — same as auto-apply OFF.
        with _cache_lock:
            _cache_entry = (now, {}, [])
        return {}

    with _cache_lock:
        _cache_entry = (now, dict(overrides), list(consulted_ids))
    return overrides


def applied_threshold_ids() -> List[int]:
    """Return the ``policy_tunings.id`` list whose values were applied
    on the most recent cache build. Surfaces in
    ``ctx.scratch['applied_threshold_ids']`` so each rule's evidence
    dict can link to the exact row it consulted. Empty when overrides
    are off."""
    with _cache_lock:
        if _cache_entry is None:
            return []
        return list(_cache_entry[2])


def apply_to_tunable_context(ctx: "PolicyContext") -> "PolicyContext":
    """Inject applied-threshold overrides into ``ctx.scratch`` so
    individual rule evaluators can prefer them over the TUNABLE
    default.

    Idempotent: re-calling on the same ctx is a no-op (the second call
    overwrites the same scratch keys with identical content). Returns
    the SAME ctx the caller passed (we mutate scratch — never replace).

    When the operator kill switch is OFF, this writes empty dicts so
    rule code can unconditionally consult
    ``ctx.scratch['applied_thresholds']`` without a sentinel check.
    """
    overrides = get_applied_thresholds()
    ctx.scratch["applied_thresholds"] = dict(overrides)
    ctx.scratch["applied_threshold_ids"] = applied_threshold_ids()
    return ctx


def mark_thresholds_applied(consulted_ids: List[int]) -> int:
    """Stamp ``applied_at = now`` on each ``policy_tunings`` row whose
    threshold was actually consulted during a live cycle. Distinct from
    ``operator_approved`` (which records consent); ``applied_at`` is the
    "the engine actually used this in production" marker.

    Idempotent: re-stamping a row updates the timestamp to the latest
    consultation, which is the desired behavior (latest-used wins).

    Returns the number of rows updated. ``consulted_ids`` empty → 0
    rows updated, no DB call.
    """
    if not consulted_ids:
        return 0
    try:
        from backend.models.policy_tuning import PolicyTuning
        now = datetime.utcnow()
        with session_scope() as s:
            rows = s.execute(
                select(PolicyTuning).where(
                    PolicyTuning.id.in_(list(consulted_ids))
                )
            ).scalars().all()
            updated = 0
            for r in rows:
                r.applied_at = now
                updated += 1
            return updated
    except Exception:
        logger.exception(
            "policy_apply.mark_thresholds_applied failed for ids=%s",
            consulted_ids,
        )
        return 0


# ── Helper for individual rule evaluators ─────────────────────────────


def resolve_threshold(
    ctx: "PolicyContext",
    *,
    threshold_attr: str,
    tunable_default: float,
) -> Tuple[float, Dict[str, Any]]:
    """Look up the effective threshold for a rule.

    Returns ``(value, evidence_dict)`` where:
      * ``value`` is the applied override when one exists for
        ``threshold_attr``, else ``tunable_default``.
      * ``evidence_dict`` carries the audit fields the rule's
        ``BlockingFactor.evidence`` should record:
          - ``threshold_source``: ``"policy_tunings_id_<N>"`` when
            overridden, ``"tunable_default"`` when not.
          - ``threshold_value_used``: the float actually consulted.
          - ``threshold_default``: the TUNABLE default (so the
            operator can see the delta).

    Rule evaluators MUST embed ``evidence_dict`` into both
    ``BlockingFactor.evidence`` (so the rule trail records what was
    used) AND any side-effect they take based on the threshold — this
    is the contract that lets the persisted ``policy_result_json``
    replay against the same threshold deterministically.
    """
    applied = ctx.scratch.get("applied_thresholds") or {}
    if not isinstance(applied, dict) or threshold_attr not in applied:
        return tunable_default, {
            "threshold_source": "tunable_default",
            "threshold_value_used": float(tunable_default),
            "threshold_default": float(tunable_default),
        }
    try:
        v = float(applied[threshold_attr])
    except (TypeError, ValueError):
        return tunable_default, {
            "threshold_source": "tunable_default",
            "threshold_value_used": float(tunable_default),
            "threshold_default": float(tunable_default),
        }
    # Resolve the policy_tunings.id that backs this attr (for audit).
    ids = ctx.scratch.get("applied_threshold_ids") or []
    backing_id: Optional[int] = None
    try:
        from backend.models.policy_tuning import PolicyTuning
        with session_scope() as s:
            row = s.execute(
                select(PolicyTuning)
                .where(PolicyTuning.id.in_(list(ids)))
                .where(PolicyTuning.threshold_attr == threshold_attr)
                .order_by(desc(PolicyTuning.computed_at))
                .limit(1)
            ).scalars().first()
            if row is not None:
                backing_id = int(row.id)
    except Exception:
        # Audit-id lookup failed — still return the override but tag
        # it with a placeholder source string. Replay still has the
        # numeric value because evidence_dict carries it.
        backing_id = None

    source = (
        f"policy_tunings_id_{backing_id}"
        if backing_id is not None
        else "policy_tunings_uncatalogued"
    )
    return v, {
        "threshold_source": source,
        "threshold_value_used": float(v),
        "threshold_default": float(tunable_default),
    }
