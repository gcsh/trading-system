"""Stage-10 drift-triggered strategy halt.

When PSI on a key predictive feature exceeds 0.25 for a strategy's cohort,
auto-halt new entries for that strategy until either:
  • a retrain runs and posts new metrics with PSI back below the watch band, OR
  • the operator manually clears the halt

Halt list is persisted to disk so it survives restart. Cleared automatically
when the next drift check sees PSI back under 0.10.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.drift import psi, severity_for

logger = logging.getLogger(__name__)

HALT_DIR = os.getenv("TB_DRIFT_HALT_DIR", "./ml/drift")
HALT_FILE = "halt_list.json"


@dataclass
class HaltRecord:
    strategy: str
    triggered_at: str
    feature: str
    psi: float
    severity: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── persistence ──────────────────────────────────────────────────────────


def _path() -> str:
    return os.path.join(HALT_DIR, HALT_FILE)


def _load() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(_path()):
        return {}
    try:
        with open(_path()) as f:
            return json.load(f) or {}
    except Exception:
        logger.debug("halt list load failed", exc_info=True)
        return {}


def _save(state: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(HALT_DIR, exist_ok=True)
    with open(_path(), "w") as f:
        json.dump(state, f, indent=2)


# ── public API ───────────────────────────────────────────────────────────


def is_halted(strategy: str) -> bool:
    return strategy in _load()


def list_halts() -> List[Dict[str, Any]]:
    return list(_load().values())


def halt_strategy(*, strategy: str, feature: str, psi_value: float,
                    reason: str = "") -> Dict[str, Any]:
    """Mark ``strategy`` halted because ``feature`` drifted."""
    state = _load()
    rec = HaltRecord(
        strategy=strategy, feature=feature, psi=round(float(psi_value), 4),
        severity=severity_for(psi_value),
        triggered_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        reason=reason or f"PSI {psi_value:.3f} on '{feature}'",
    )
    state[strategy] = rec.to_dict()
    _save(state)
    return rec.to_dict()


def clear_halt(strategy: str) -> bool:
    state = _load()
    if strategy not in state:
        return False
    del state[strategy]
    _save(state)
    return True


def check_and_update_halts(*,
                              baseline_by_strategy: Dict[str, Dict[str, List[float]]],
                              current_by_strategy: Dict[str, Dict[str, List[float]]],
                              psi_threshold: float = 0.25,
                              clear_threshold: float = 0.10,
                              ) -> Dict[str, Any]:
    """Walk every (strategy, feature) pair; halt or clear based on PSI vs
    thresholds. Returns a summary report."""
    state = _load()
    halted_now: List[str] = []
    cleared_now: List[str] = []
    for strategy, baseline_features in baseline_by_strategy.items():
        cur_features = current_by_strategy.get(strategy, {})
        worst_psi = 0.0
        worst_feature = ""
        for feat, baseline in baseline_features.items():
            score = psi(baseline, cur_features.get(feat, []))
            if score is None:
                continue
            if score > worst_psi:
                worst_psi = score
                worst_feature = feat
        if worst_psi >= psi_threshold and strategy not in state:
            halt_strategy(strategy=strategy, feature=worst_feature,
                            psi_value=worst_psi,
                            reason=f"auto-halt: {worst_feature} PSI {worst_psi:.3f} ≥ {psi_threshold}")
            halted_now.append(strategy)
        elif worst_psi <= clear_threshold and strategy in state:
            clear_halt(strategy)
            cleared_now.append(strategy)
    return {
        "halted_now": halted_now,
        "cleared_now": cleared_now,
        "current_halts": list_halts(),
    }
