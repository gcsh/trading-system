"""Adaptive option exit manager (EXIT.1 + MITS Phase 17.E).

Replaces the legacy +50%/-50% hard rule with a peak-tracking trailing
exit that NEVER caps the upside. The model has two zones:

  Below +15% gain — "early phase"
    The position is still developing. Apply only the catastrophe stop
    (DTE-adjusted) and defer to the AI Brain for thesis-based exits.

  Above +15% gain — "monitor mode"
    Track the high-water mark of the per-share premium. Exit when the
    current premium drops more than ``trailing_distance(...)`` from peak.
    The trailing distance is NOT a fixed number — it widens with the
    size of the gain (so runners get room to breathe) and tightens with
    DTE shrinking and IV crushing (so we don't give back gains to theta
    or vol collapse).

There is no upper ceiling. A position at +500% with healthy momentum
and 21 DTE remaining can keep running. The exit fires only when the
peak-to-current drawdown crosses the adaptive threshold OR when the
AI Brain / council says exit OR when the catastrophe stop fires.

MITS Phase 17.E refactor — internal architecture only, public contract
preserved:

* The procedural decision cascade (dte_cliff → catastrophe_stop →
  trailing_stop) is now delegated to ``ExitPolicy`` so every cycle's
  full set of triggers (not just the first one to fire) is recorded
  in the ``exit_rule_evaluations`` ledger.
* ``decide_exit()`` keeps the same signature + ExitDecision return
  type — engine.py + paper_executor.py callers see ZERO behavioural
  change. Internally it builds an ExitContext, runs the policy, and
  flattens the result back into an ExitDecision.
* ``decide_exit_with_policy()`` is the new rich-result helper. The
  engine's close path can read this directly to populate
  ``Trade.exit_policy_result_json`` so the cockpit can render every
  concurrent trigger.

Tunables (with sensible defaults, all configurable via env):

  TB_OPT_EXIT_MONITOR_FLOOR_PCT   = 15.0   # gain % below which we don't trail yet
  TB_OPT_EXIT_HARD_STOP_PCT       = 50.0   # catastrophe stop, gets DTE-adjusted
  TB_OPT_EXIT_DTE_CLIFF           = 3      # DTE at which "any profit" becomes the rule
  TB_OPT_EXIT_IV_CRUSH_RATIO      = 0.75   # current IV / entry IV → tighter trail
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Tuple

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """Verdict for a single option position.

    Always returned (never None) so callers see WHY we're holding or
    closing — including the current trailing-stop level for UI display.
    """
    should_exit: bool
    reason: str
    # Diagnostic state for UI surfacing — operator sees these even when
    # the position is being held.
    gain_pct: float                       # current premium vs entry
    drawdown_from_peak_pct: float         # how far below peak we are now
    trailing_floor_pct: Optional[float]   # if monitor-mode is active, the
                                          # gain % below which we'd exit
    hard_stop_pct: float                  # DTE-adjusted catastrophe stop
    monitor_active: bool                  # True when we're in trailing mode
    iv_crush_detected: bool


def _f(name: str, default: float) -> float:
    return float(getattr(TUNABLES, name, default))


def _dte_adjusted_hard_stop_pct(dte: int) -> float:
    """Catastrophe stop tightens as expiry approaches.

    The intuition: a -50% drawdown with 30 DTE is recoverable; the same
    drawdown with 4 DTE is mathematically not (theta accelerates).
    """
    base = _f("opt_exit_hard_stop_pct", 50.0)
    if dte <= 1:
        return min(base, 15.0)
    if dte <= 3:
        return min(base, 25.0)
    if dte <= 7:
        return min(base, 35.0)
    if dte <= 14:
        return min(base, 45.0)
    return base


def _trailing_distance_pct(gain_pct: float, dte: int,
                           iv_crushing: bool) -> float:
    """How far below the peak premium we tolerate before exiting.

    The shape is intentionally gain-aware: a position up +20% gets a
    tight 10-pt trail (because giving back 10 from a small gain hurts);
    a position up +200% gets a wider 35-pt trail (because we want big
    winners to breathe through noise).

    DTE and IV-crush both *tighten* the trail — when time decay or vol
    collapse is eating the position, we need to be more vigilant about
    drawdowns from peak.
    """
    if gain_pct < 25:
        base = 10.0
    elif gain_pct < 50:
        base = 15.0
    elif gain_pct < 100:
        base = 25.0
    elif gain_pct < 200:
        base = 30.0
    else:
        base = 35.0  # gain > +200%

    # DTE urgency multiplier (< 1.0 = tighter).
    if dte <= 1:
        dte_mult = 0.30
    elif dte <= 3:
        dte_mult = 0.50
    elif dte <= 7:
        dte_mult = 0.70
    elif dte <= 14:
        dte_mult = 0.85
    else:
        dte_mult = 1.00

    crush_mult = 0.70 if iv_crushing else 1.00
    return max(5.0, base * dte_mult * crush_mult)


def _build_exit_context(
    *,
    entry_premium_per_share: float,
    current_premium_per_share: float,
    peak_premium_per_share: Optional[float],
    dte: int,
    entry_iv: Optional[float],
    current_iv: Optional[float],
    position_id: Optional[int] = None,
    ticker: Optional[str] = None,
):
    """Compute the derived gain/peak/floor numbers + assemble ExitContext.

    Pulled out of the legacy ``decide_exit`` body verbatim so the
    declarative rules consume the exact same math the procedural code
    did. Returns ``(context, monitor_floor_pct, hold_reason_factory)``
    where the factory builds the 3-flavour HOLD string the legacy
    diagnostic block emitted (used when no trigger fires).
    """
    # Local import — avoids a top-level circular pull when the engine
    # module is in the process of importing exit_manager during boot.
    from backend.bot.decision.exit_policy import ExitContext

    entry = max(1e-6, float(entry_premium_per_share))
    cur = float(current_premium_per_share)
    peak = float(peak_premium_per_share if peak_premium_per_share is not None
                 else max(entry, cur))
    # Defensive: a future cycle should never see peak < current; if it
    # does (e.g. peak wasn't persisted yet), promote on the fly.
    if cur > peak:
        peak = cur

    gain_pct = ((cur - entry) / entry) * 100.0
    peak_gain_pct = ((peak - entry) / entry) * 100.0
    drawdown_from_peak = (
        ((peak - cur) / peak) * 100.0 if peak > 0 else 0.0
    )

    hard_stop = _dte_adjusted_hard_stop_pct(dte)
    monitor_floor = _f("opt_exit_monitor_floor_pct", 15.0)
    iv_crush_ratio = _f("opt_exit_iv_crush_ratio", 0.75)
    dte_cliff = int(getattr(TUNABLES, "opt_exit_dte_cliff", 3))

    # IV crush detection: only meaningful when we have both entry and
    # current IV. Treat current/entry below the ratio as crushing.
    iv_crushing = False
    if entry_iv is not None and current_iv is not None and entry_iv > 0:
        iv_crushing = (current_iv / entry_iv) < iv_crush_ratio

    monitor_active = peak_gain_pct >= monitor_floor
    trailing_floor: Optional[float] = None
    if monitor_active:
        trail = _trailing_distance_pct(peak_gain_pct, dte, iv_crushing)
        # Translate trailing distance into a floor expressed as gain %
        # vs entry, so the operator sees "exit if gain drops below X%".
        # floor_premium = peak * (1 - trail/100)
        floor_premium = peak * (1.0 - trail / 100.0)
        # Never let the floor go below break-even once we've crossed the
        # monitor band — the whole point is to lock in some win.
        floor_premium = max(floor_premium, entry)
        trailing_floor = ((floor_premium - entry) / entry) * 100.0

    ctx = ExitContext(
        entry_premium_per_share=entry,
        current_premium_per_share=cur,
        peak_premium_per_share=peak,
        dte=dte,
        entry_iv=entry_iv,
        current_iv=current_iv,
        gain_pct=gain_pct,
        peak_gain_pct=peak_gain_pct,
        drawdown_from_peak_pct=drawdown_from_peak,
        hard_stop_pct=hard_stop,
        monitor_floor_pct=monitor_floor,
        monitor_active=monitor_active,
        trailing_floor_pct=trailing_floor,
        iv_crush_detected=iv_crushing,
        dte_cliff=dte_cliff,
        position_id=position_id,
        ticker=ticker,
    )

    # The HOLD-path diagnostic strings the legacy emitter produced when
    # nothing fired. Round-trips byte-for-byte so persisted Trade.reason
    # values are unchanged for hold cycles.
    def _hold_reason() -> str:
        if monitor_active and trailing_floor is not None:
            return (
                f"monitoring: gain +{gain_pct:.1f}%, "
                f"peak +{peak_gain_pct:.1f}%, "
                f"trailing floor +{trailing_floor:.1f}% (drawdown "
                f"{drawdown_from_peak:.1f}% from peak)"
            )
        if gain_pct < 0:
            return (
                f"holding: {gain_pct:.1f}%, catastrophe stop "
                f"-{hard_stop:.0f}%"
            )
        return (
            f"early phase: +{gain_pct:.1f}% < monitor floor "
            f"+{monitor_floor:.0f}%"
        )

    return ctx, _hold_reason


def decide_exit_with_policy(
    *,
    entry_premium_per_share: float,
    current_premium_per_share: float,
    peak_premium_per_share: Optional[float],
    dte: int,
    entry_iv: Optional[float],
    current_iv: Optional[float],
    position_id: Optional[int] = None,
    ticker: Optional[str] = None,
) -> Tuple["ExitDecision", "object"]:
    """Rich-result twin of :func:`decide_exit`.

    Returns ``(ExitDecision, ExitPolicyResult)``. Callers that need
    full per-rule telemetry (cockpit persistence, the engine's close
    path) use this; pure-function callers (paper_executor's
    diagnostic state, unit tests) keep using :func:`decide_exit`
    which is implemented in terms of this helper.
    """
    from backend.bot.decision.exit_rules import build_default_policy

    ctx, hold_reason_factory = _build_exit_context(
        entry_premium_per_share=entry_premium_per_share,
        current_premium_per_share=current_premium_per_share,
        peak_premium_per_share=peak_premium_per_share,
        dte=dte,
        entry_iv=entry_iv,
        current_iv=current_iv,
        position_id=position_id,
        ticker=ticker,
    )
    policy = build_default_policy()
    result = policy.evaluate(ctx)

    # Map the policy result back into the ExitDecision contract the
    # legacy callers consume. The headline reason is the chosen
    # trigger's reason on a close; otherwise the HOLD diagnostic
    # string (round-trips with the pre-refactor body).
    if result.should_close and result.chosen is not None:
        reason = result.chosen.reason
    else:
        reason = hold_reason_factory()

    decision = ExitDecision(
        should_exit=result.should_close,
        reason=reason,
        gain_pct=ctx.gain_pct,
        drawdown_from_peak_pct=ctx.drawdown_from_peak_pct,
        trailing_floor_pct=ctx.trailing_floor_pct,
        hard_stop_pct=ctx.hard_stop_pct,
        monitor_active=ctx.monitor_active,
        iv_crush_detected=ctx.iv_crush_detected,
    )

    # Invariant (mirrors 16.A): if should_close is True at least one
    # hard trigger MUST be in the result. Catching a registration-order
    # violation here is cheaper than chasing it in production.
    if result.should_close and not any(
        t.severity == "hard" for t in result.triggers
    ):
        logger.error(
            "ExitPolicy invariant violated: should_close=True with no "
            "hard trigger fired — registration-order regression?"
        )

    return decision, result


def decide_exit(
    *,
    entry_premium_per_share: float,
    current_premium_per_share: float,
    peak_premium_per_share: Optional[float],
    dte: int,
    entry_iv: Optional[float],
    current_iv: Optional[float],
) -> ExitDecision:
    """Compute the exit decision for an open option position.

    Pure function — the caller is responsible for persisting any updated
    peak / IV state. Returns an ``ExitDecision`` describing the verdict
    and the diagnostic state behind it.

    Args:
        entry_premium_per_share: Premium paid at entry, per share.
        current_premium_per_share: Current mark, per share.
        peak_premium_per_share: High-water mark since entry. ``None`` on
            the first cycle — treated as max(entry, current).
        dte: Days to expiration (>=0).
        entry_iv: IV captured at entry (annualized decimal). None if
            unknown — IV-crush detection then skipped.
        current_iv: Current IV from latest chain mark. None if unknown.

    Back-compat note (MITS Phase 17.E): this function's signature +
    return type are FROZEN. Internally it delegates to
    ``decide_exit_with_policy`` so the new declarative rule engine
    drives every decision, but callers see the original ExitDecision
    contract unchanged. Tests in tests/unit/test_option_exit_manager.py
    are the back-compat regression gate.
    """
    decision, _result = decide_exit_with_policy(
        entry_premium_per_share=entry_premium_per_share,
        current_premium_per_share=current_premium_per_share,
        peak_premium_per_share=peak_premium_per_share,
        dte=dte,
        entry_iv=entry_iv,
        current_iv=current_iv,
    )
    return decision


def persist_exit_evaluations(
    *,
    result: "object",
    position_id: Optional[int],
    ticker: Optional[str],
) -> None:
    """Best-effort write of each ExitRuleEvaluation row to the ledger.

    Called from the engine's close path (and the helper that builds
    the rich result for any caller that wants telemetry). NEVER raises
    — telemetry failures must not block the close-the-position path,
    even if the DB is briefly unavailable.
    """
    try:
        from backend.db import session_scope
        from backend.models.exit_rule_evaluation import ExitRuleEvaluation
    except Exception:
        return
    try:
        rows = getattr(result, "rule_evaluations", None) or []
        if not rows:
            return
        evaluated_at = datetime.utcnow()
        with session_scope() as session:
            for row in rows:
                try:
                    session.add(ExitRuleEvaluation(
                        evaluated_at=evaluated_at,
                        position_id=position_id,
                        ticker=ticker,
                        rule_name=row.rule_name,
                        severity=row.severity,
                        fired=bool(row.fired),
                        legacy_action=row.legacy_action,
                        reason=row.reason,
                        evidence_json=(
                            json.dumps(row.evidence) if row.evidence else None
                        ),
                    ))
                except Exception:
                    # Skip the bad row, keep the others — partial
                    # telemetry is better than none.
                    logger.debug(
                        "exit_rule_evaluations row drop ticker=%s rule=%s",
                        ticker, getattr(row, "rule_name", "?"),
                        exc_info=True,
                    )
    except Exception:
        logger.debug(
            "exit_rule_evaluations persist failed ticker=%s",
            ticker, exc_info=True,
        )


def compute_dte(expiration: str, today: Optional[date] = None) -> int:
    """Convenience — parse ISO date and return DTE clamped to >=0."""
    today = today or date.today()
    try:
        exp = date.fromisoformat(str(expiration))
    except Exception:
        return 0
    return max(0, (exp - today).days)
