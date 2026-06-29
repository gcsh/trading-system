"""A/B routing — deterministic ticker-bucket assignment.

A "rollout" is the canonical institutional way to ship a new model: route
some percentage of traffic to the candidate, the rest to the control. The
assignment must be:

  • **Deterministic** — same ticker always lands in the same arm so the
    same trade isn't graded against two different models over its lifetime
  • **Uniform** — independent of how many tickers we have or which
    alphabetic prefix dominates the watchlist
  • **Reproducible** — bucket boundaries persist via a config record so
    everyone sees the same split

We use SHA-1 over (split_name + ticker) and take the first 4 bytes as the
bucket. Split definitions live in ``ABRecord`` JSON.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.ml.registry import REGISTRY_DIR

logger = logging.getLogger(__name__)

SPLITS_FILE = os.path.join(REGISTRY_DIR, "ab_splits.json")


@dataclass
class ABRecord:
    name: str                           # human label, e.g. "hist_gb_v3"
    control_version: str
    candidate_version: str
    candidate_share: float = 0.10       # fraction in [0, 1] routed to candidate
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── deterministic bucket ───────────────────────────────────────────────────


def bucket_for(split_name: str, ticker: str) -> float:
    """Return the bucket ∈ [0, 1) for ``(split_name, ticker)``. Used as the
    cumulative threshold against ``candidate_share`` — a value below the
    threshold means "candidate arm", above means "control"."""
    blob = f"{split_name}::{ticker.upper()}".encode()
    digest = hashlib.sha1(blob).digest()[:4]
    n = int.from_bytes(digest, "big")
    return n / 0xFFFFFFFF


def decide_arm(record: ABRecord, ticker: str) -> str:
    """Returns ``"candidate"`` or ``"control"`` based on the bucket math."""
    b = bucket_for(record.name, ticker)
    return "candidate" if b < max(0.0, min(1.0, record.candidate_share)) else "control"


# ── persistence ────────────────────────────────────────────────────────────


def _load_splits() -> List[ABRecord]:
    if not os.path.exists(SPLITS_FILE):
        return []
    try:
        with open(SPLITS_FILE) as f:
            raw = json.load(f)
        return [ABRecord(**r) for r in raw]
    except Exception:
        logger.debug("ab splits load failed", exc_info=True)
        return []


def _save_splits(splits: List[ABRecord]) -> None:
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    with open(SPLITS_FILE, "w") as f:
        json.dump([s.to_dict() for s in splits], f, indent=2)


def register_split(*, name: str, control_version: str,
                     candidate_version: str, candidate_share: float = 0.10,
                     notes: str = "") -> ABRecord:
    """Add a split (replacing one with the same name). Returns the canonical
    record after persistence."""
    splits = [s for s in _load_splits() if s.name != name]
    new = ABRecord(name=name, control_version=control_version,
                     candidate_version=candidate_version,
                     candidate_share=max(0.0, min(1.0, candidate_share)),
                     notes=notes)
    splits.append(new)
    _save_splits(splits)
    return new


def list_splits() -> List[Dict[str, Any]]:
    return [s.to_dict() for s in _load_splits()]


def get_split(name: str) -> Optional[ABRecord]:
    for s in _load_splits():
        if s.name == name:
            return s
    return None
