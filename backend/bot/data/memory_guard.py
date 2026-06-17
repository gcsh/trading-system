"""MITS Phase 11.1 — process-wide memory pressure guard.

A SINGLE source of truth for "is it safe to launch / continue another
heavy backfill / embed / ferry job right now?". Every long-running
batch process should consult :func:`memory_pressure_ok` at safe
yielding points (e.g. between ticker iterations, between embed
batches, between ferry batches). On YELLOW the caller should slow
down; on RED the caller should bail and rely on the resumable
watermark to pick up later.

Two thresholds, both operator-tunable via TUNABLES:

  * ``backfill_memory_pause_pct`` (default 85.0) — return ``False``
    above this.
  * ``backfill_memory_warn_pct`` (default 70.0) — return ``True`` but
    mark the result yellow.

We deliberately don't try to GC / drop caches here — the caller
knows what state to clear.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryStatus:
    """A single memory-pressure observation."""
    percent: float
    available_gb: float
    total_gb: float
    color: str   # "green" | "yellow" | "red"
    ok: bool     # safe to proceed?

    def to_dict(self) -> dict:
        return {
            "percent": round(self.percent, 1),
            "available_gb": round(self.available_gb, 2),
            "total_gb": round(self.total_gb, 2),
            "color": self.color,
            "ok": self.ok,
        }


def _read_psutil() -> Optional[MemoryStatus]:
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        vm = psutil.virtual_memory()
    except Exception:
        return None
    pct = float(vm.percent)
    pause_pct = float(getattr(TUNABLES, "backfill_memory_pause_pct", 85.0))
    warn_pct = float(getattr(TUNABLES, "backfill_memory_warn_pct", 70.0))
    if pct >= pause_pct:
        color = "red"
    elif pct >= warn_pct:
        color = "yellow"
    else:
        color = "green"
    return MemoryStatus(
        percent=pct,
        available_gb=float(vm.available) / (1024 ** 3),
        total_gb=float(vm.total) / (1024 ** 3),
        color=color,
        ok=(pct < pause_pct),
    )


def memory_status() -> MemoryStatus:
    """Always return a non-None status. If psutil is missing or the
    probe fails we conservatively report 'green' rather than 'red' to
    avoid permanently stalling the backfills.
    """
    s = _read_psutil()
    if s is not None:
        return s
    return MemoryStatus(
        percent=0.0,
        available_gb=0.0,
        total_gb=0.0,
        color="green",
        ok=True,
    )


def memory_pressure_ok() -> bool:
    """Cheap accessor — True when it's safe to launch / continue heavy
    work. Used by ``bin/launch_backfill.py``, ``bin/bronze_ferry.py``,
    ``bin/embed_namespace.py``, and the scheduler's nightly passes."""
    return memory_status().ok


def wait_until_ok(*, max_seconds: int = 300,
                       sleep_seconds: int = 30) -> bool:
    """Block until memory_pressure_ok() turns True, with a hard cap of
    ``max_seconds``. Returns True if pressure cleared, False if we
    timed out (caller should bail and rely on the watermark).
    """
    waited = 0
    while waited < max_seconds:
        if memory_pressure_ok():
            return True
        logger.warning(
            "memory_guard: pressure too high (%.1f%%) — sleeping %ds",
            memory_status().percent, sleep_seconds,
        )
        time.sleep(sleep_seconds)
        waited += sleep_seconds
    return memory_pressure_ok()


__all__ = [
    "MemoryStatus",
    "memory_status",
    "memory_pressure_ok",
    "wait_until_ok",
]
