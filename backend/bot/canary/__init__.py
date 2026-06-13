"""Stage-8 canary state machine + kill-switch.

A canary is the controlled handoff from paper to real money. Three states,
one direction by default (forward only); rollback is automatic on gate
violation.

  PAPER  →  CANARY  →  SCALED

Promotion gates: every Stage-1.5 gate must be `pass` AND the gates added in
later stages (no SLO breach, no critical drift). Persistence is a single
JSON file so the state survives process restart.

Kill-switch is a separate file with one boolean — engine reads it every
cycle. ``kill_switch_active()`` is the single source of truth.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CANARY_DIR = os.getenv("TB_CANARY_DIR", "./ml/canary")
STATE_FILE = "state.json"
KILL_FILE = "kill_switch.json"

VALID_STATES = ("paper", "canary", "scaled", "halted")
DEFAULT_CANARY_CAPITAL = 500.0


@dataclass
class CanaryState:
    state: str = "paper"
    capital: float = 0.0
    promoted_at: Optional[str] = None
    rolled_back_at: Optional[str] = None
    rollback_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── persistence helpers ───────────────────────────────────────────────────


def _ensure_dir() -> None:
    os.makedirs(CANARY_DIR, exist_ok=True)


def _state_path() -> str:
    return os.path.join(CANARY_DIR, STATE_FILE)


def _kill_path() -> str:
    return os.path.join(CANARY_DIR, KILL_FILE)


def _load() -> CanaryState:
    path = _state_path()
    if not os.path.exists(path):
        return CanaryState()
    try:
        with open(path) as f:
            raw = json.load(f)
        return CanaryState(**raw)
    except Exception:
        logger.debug("canary load failed", exc_info=True)
        return CanaryState()


def _save(state: CanaryState) -> None:
    _ensure_dir()
    with open(_state_path(), "w") as f:
        json.dump(state.to_dict(), f, indent=2)


# ── public API ────────────────────────────────────────────────────────────


def get_state() -> CanaryState:
    return _load()


def promote(*, target: str, capital: float = DEFAULT_CANARY_CAPITAL,
              gates_summary: Optional[Dict[str, Any]] = None,
              force: bool = False) -> Dict[str, Any]:
    """Move to ``target`` (canary/scaled). Refuses unless gates pass OR
    ``force=True``. Returns a result dict — the API surface decides whether
    to 200 OK or 422 on a refusal."""
    if target not in ("canary", "scaled"):
        return {"ok": False, "reason": f"invalid target '{target}'"}
    if gates_summary is None:
        gates_summary = {"overall": "insufficient_data"}
    if gates_summary.get("overall") not in ("pass",) and not force:
        return {
            "ok": False,
            "reason": f"gates not green (overall='{gates_summary.get('overall')}')",
            "gates": gates_summary,
        }
    state = _load()
    state.state = target
    state.capital = float(capital)
    state.promoted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    state.rolled_back_at = None
    state.rollback_reason = None
    _save(state)
    return {"ok": True, "state": state.to_dict()}


def rollback(*, reason: str) -> Dict[str, Any]:
    state = _load()
    state.state = "paper"
    state.capital = 0.0
    state.rolled_back_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    state.rollback_reason = reason
    _save(state)
    return {"ok": True, "state": state.to_dict()}


def halt(*, reason: str) -> Dict[str, Any]:
    """Hard stop — distinct from rollback; signals operator intervention."""
    state = _load()
    state.state = "halted"
    state.capital = 0.0
    state.rolled_back_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    state.rollback_reason = reason
    _save(state)
    return {"ok": True, "state": state.to_dict()}


# ── kill switch ─────────────────────────────────────────────────────────


def kill_switch_active() -> bool:
    """Single source of truth — read every cycle. False unless explicitly set."""
    path = _kill_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            raw = json.load(f)
        return bool(raw.get("active"))
    except Exception:
        return False


def set_kill_switch(active: bool, *, reason: str = "") -> Dict[str, Any]:
    _ensure_dir()
    payload = {"active": bool(active), "reason": reason,
                "set_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(_kill_path(), "w") as f:
        json.dump(payload, f, indent=2)
    return payload


def kill_switch_status() -> Dict[str, Any]:
    path = _kill_path()
    if not os.path.exists(path):
        return {"active": False, "reason": "", "set_at": None}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"active": False, "reason": "load failed", "set_at": None}
