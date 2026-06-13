"""Trade & account invariants — the safety net that catches bad data BEFORE it
hits the DB.

We learned the hard way: silent string mistakes (strike = stock price,
``instrument="spread"`` for a CSP) and unbookkept account writes (synthetic
test data that bumped realized_pnl) corrupted live state and only got noticed
when the user spotted a +191% return that couldn't be right.

This module is the second layer of defense. Each ``check_*`` function raises
``AuditViolation`` when an invariant fails. Callers decide whether to
**block** (paper) or **warn** (live) — see ``audit_order_plan`` and
``audit_account_write``.

Invariants are intentionally narrow and obvious. If one is wrong, fix the
invariant — don't loosen it to make a broken trade pass.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


class AuditViolation(Exception):
    """Raised when a hard invariant fails. Carries the invariant name + payload
    so the caller (and the UI) can show exactly what went wrong."""

    def __init__(self, name: str, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.name = name
        self.payload = payload or {}


@dataclass
class AuditResult:
    ok: bool
    violations: List[Dict[str, Any]]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "violations": self.violations}


# ── invariants ──────────────────────────────────────────────────────────────


# Action sets — duplicated from engine.py to avoid an import cycle. Keep in sync
# (the engine raises ImportError if either side drifts so any rename is caught).
_LONG_OPT = {"BUY_CALL", "BUY_PUT"}
_SHORT_OPT = {"SELL_CSP", "SELL_COVERED_CALL"}
_SPREAD_OPT = {"BULL_CALL_SPREAD", "BUY_STRADDLE", "IRON_CONDOR",
                "RATIO_SPREAD", "COLLAR"}
_STOCK = {"BUY_STOCK", "SELL_STOCK"}


def check_strike_is_snapped(strike: float, price_hint: Optional[float] = None) -> None:
    """A strike must round to a recognized chain interval — never the raw
    stock price. Triggered when ``round(price, 2)`` leaks back in."""
    if strike is None or strike <= 0:
        raise AuditViolation("strike_missing",
                              "option order has no strike", {"strike": strike})
    bands = getattr(TUNABLES, "strike_intervals", None) or [
        (25.0, 0.50), (100.0, 1.0), (500.0, 5.0), (float("inf"), 10.0),
    ]
    # Pick the right band by the strike itself; price_hint is a sanity ref.
    interval = next((step for upper, step in bands if strike < float(upper)),
                     float(bands[-1][1]))
    remainder = round(strike / interval, 6) - round(strike / interval)
    # allow tiny float noise
    if abs(remainder) > 1e-4 and abs(strike % interval) > 1e-4:
        raise AuditViolation("strike_not_snapped",
                              f"strike {strike} not aligned to ${interval} interval",
                              {"strike": strike, "interval": interval,
                                "price_hint": price_hint})


def check_instrument_matches_action(action: str, instrument: str) -> None:
    """The persisted ``instrument`` column must match the action category.
    Triggered when a CSP was being stored as ``spread`` etc."""
    action = (action or "").upper()
    instrument = (instrument or "").lower()
    if action in _STOCK:
        expect = "stock"
    elif action in _LONG_OPT or action in _SHORT_OPT:
        expect = "option"
    elif action in _SPREAD_OPT:
        expect = "spread"
    elif action == "CLOSE_OPTION":
        expect = "option"
    else:
        expect = instrument        # unknown action — let it pass for now
    if expect and instrument != expect:
        raise AuditViolation("instrument_mismatch",
                              f"action {action} expects instrument='{expect}', got '{instrument}'",
                              {"action": action, "instrument": instrument, "expect": expect})


def check_option_has_required_fields(action: str, instrument: str,
                                       strike: Any, expiration: Any) -> None:
    """Any non-stock order must carry strike + expiration. Triggered when an
    option was being written with both fields NULL."""
    if instrument == "stock" or action in _STOCK:
        return
    if not strike or not expiration:
        raise AuditViolation("option_fields_missing",
                              f"{action} needs strike + expiration; got "
                              f"strike={strike} expiration={expiration}",
                              {"action": action, "strike": strike, "expiration": expiration})


def check_expiration_in_future(expiration: str) -> None:
    """An options entry order can never carry an expiration in the past.
    Triggered if a strategy supplies a stale chain expiration."""
    if not expiration:
        return
    try:
        from datetime import date
        exp = date.fromisoformat(str(expiration))
        today = date.today()
    except Exception:
        return    # malformed — covered by check_option_has_required_fields
    if exp < today:
        raise AuditViolation("expiration_in_past",
                              f"expiration {expiration} is before today {today.isoformat()}",
                              {"expiration": expiration, "today": today.isoformat()})


# ── order-plan audit ────────────────────────────────────────────────────────


def audit_order_plan(action: str, plan: Dict[str, Any]) -> AuditResult:
    """Run every invariant on a fully-built order plan. Returns the violation
    list; callers (engine) decide whether to block."""
    violations: List[Dict[str, Any]] = []
    instrument = plan.get("instrument", "stock")

    def _try(fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except AuditViolation as v:
            violations.append({"name": v.name, "message": str(v), **v.payload})

    _try(check_instrument_matches_action, action, instrument)
    _try(check_option_has_required_fields, action, instrument,
          plan.get("strike"), plan.get("expiration"))
    if instrument in ("option", "spread"):
        _try(check_strike_is_snapped, plan.get("strike"))
        # Allow future-only expirations for fresh orders; the exit manager
        # handles closing things that have already expired.
        if action != "CLOSE_OPTION":
            _try(check_expiration_in_future, plan.get("expiration"))
    return AuditResult(ok=not violations, violations=violations)


# ── account-write audit ─────────────────────────────────────────────────────


def audit_account_write(reason: str, cash_delta: float, realized_delta: float) -> None:
    """Every change to ``account.cash`` or ``account.realized_pnl`` must carry
    a ``reason`` string. We log the write so a synthetic credit later in the
    session is searchable. Raises if ``TB_LOCK_ACCOUNT_WRITES`` is set and the
    reason looks synthetic (used by tests)."""
    if cash_delta == 0 and realized_delta == 0:
        return
    if not reason:
        raise AuditViolation("account_write_no_reason",
                              "account.cash / realized_pnl write missing a reason")
    suspicious = reason.lower().startswith(("test", "synth", "plant", "verify"))
    if os.getenv("TB_LOCK_ACCOUNT_WRITES") and suspicious:
        raise AuditViolation("account_write_synthetic",
                              f"refusing synthetic account write (reason='{reason}') "
                              f"with TB_LOCK_ACCOUNT_WRITES set",
                              {"reason": reason, "cash_delta": cash_delta,
                                "realized_delta": realized_delta})
    logger.info("[audit] cash %+.2f  realized %+.2f  reason=%s",
                 cash_delta, realized_delta, reason)


# ── account reconciliation (called from /audit/health) ──────────────────────


def reconcile_account(cash: float, realized_pnl: float,
                       positions_market_value: float, portfolio_value: float,
                       tolerance: float = 0.50) -> AuditResult:
    """Cash + Σ position market value should equal the reported portfolio value
    within a small tolerance. A larger drift means one of the three sources is
    out of sync — surfaced in the UI as a red audit light."""
    violations: List[Dict[str, Any]] = []
    expected_pv = round(cash + positions_market_value, 2)
    drift = round(portfolio_value - expected_pv, 2)
    if abs(drift) > tolerance:
        violations.append({
            "name": "account_pv_drift",
            "message": f"portfolio_value ${portfolio_value:.2f} != cash + positions ${expected_pv:.2f}",
            "drift": drift,
            "cash": cash,
            "positions_market_value": positions_market_value,
            "portfolio_value": portfolio_value,
        })
    return AuditResult(ok=not violations, violations=violations)


# ── trade-row writability ───────────────────────────────────────────────────


# Signal-source pseudo-strategies that legitimately appear in `Trade.strategy`
# but aren't in STRATEGY_REGISTRY. These are the system layers that open/close
# trades on top of (or outside) the named strategy registry.
_SYSTEM_STRATEGY_SOURCES = frozenset({
    "adaptive",
    "exit_manager",
    "ai_brain",
    "live_engine",
    "historical_replay",
    # Internal close/sweep paths the engine writes Trade rows for.
    "eod_sweep",
    "thesis_health",
    "assignment",
})


def _known_strategies() -> set:
    """Build the union of registered strategies + system signal-sources.

    Imported lazily to avoid an import cycle (audit.py is loaded BEFORE the
    strategies module on engine boot)."""
    try:
        from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY
        registered = set(STRATEGY_REGISTRY.keys())
    except Exception:    # never let a registry import break the writability check
        registered = set()
    return registered | set(_SYSTEM_STRATEGY_SOURCES)


def verify_trade_writable(trade: Any) -> None:
    """Reject Trade rows that would pollute the live DB.

    Two invariants:
    1. ``ticker`` must not begin with ``_`` — that prefix is reserved for
       test-suite sentinels and never appears on a real fill.
    2. ``strategy`` must be either a registered strategy or one of the
       known system signal-source pseudo-strategies. A typo (e.g.
       ``"rsi_mean_revision"`` instead of ``"rsi_mean_reversion"``) would
       silently break per-strategy attribution; we'd rather raise.

    Raises ``AuditViolation`` on failure. Wire into the persist path BEFORE
    ``session.add(trade)`` so a bad row never lands."""
    ticker = getattr(trade, "ticker", None) or ""
    if isinstance(ticker, str) and ticker.startswith("_"):
        raise AuditViolation(
            "trade_ticker_sentinel",
            f"refusing trade write with sentinel ticker '{ticker}'",
            {"ticker": ticker},
        )
    strategy = getattr(trade, "strategy", None) or ""
    if strategy and strategy not in _known_strategies():
        raise AuditViolation(
            "trade_strategy_unknown",
            f"strategy '{strategy}' not in registry or system sources",
            {"strategy": strategy,
              "known": sorted(_known_strategies())},
        )


def audit_open_options(positions: List[dict]) -> AuditResult:
    """An OPEN option position whose expiration has passed shouldn't exist —
    the exit manager runs every cycle and force-closes at DTE ≤ 0. If we see
    one here, the exit manager isn't running."""
    from datetime import date
    today = date.today()
    violations: List[Dict[str, Any]] = []
    for pos in positions:
        if pos.get("kind") != "option":
            continue
        expiration = pos.get("expiration")
        if not expiration:
            continue
        try:
            exp = date.fromisoformat(str(expiration))
        except Exception:
            continue
        if exp < today:
            violations.append({
                "name": "option_expired_still_open",
                "message": f"{pos.get('ticker')} {pos.get('option_type')} "
                            f"strike {pos.get('strike')} expired {expiration} but still open",
                "position": {k: pos.get(k) for k in
                              ("ticker", "strike", "expiration", "option_type", "quantity")},
            })
    return AuditResult(ok=not violations, violations=violations)
