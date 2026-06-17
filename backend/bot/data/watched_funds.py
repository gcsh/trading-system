"""MITS Phase 11.E — watched fund roster loader.

Reads ``backend/bot/data/watched_funds.json`` (or a deployed copy at
``/opt/trading-bot/backend/bot/data/watched_funds.json``) and exposes
the canonical 100-fund roster used by the 13F backfill + the
smart-money feature layer.

Dedupe is done at load time — the source JSON intentionally over-lists
names (some funds share CIKs because they're parent / subsidiary entries
in our manual roster). The loader emits unique funds keyed by
``(cik, name)`` pairs so a duplicated CIK across multiple names doesn't
collapse the roster.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_DEFAULT_PATHS = (
    os.environ.get("TB_WATCHED_FUNDS_PATH") or "",
    "/opt/trading-bot/backend/bot/data/watched_funds.json",
    os.path.join(os.path.dirname(__file__), "watched_funds.json"),
)


@dataclass(frozen=True)
class WatchedFund:
    cik: str       # zero-padded 10-digit
    name: str
    category: str

    def to_dict(self) -> dict:
        return {"cik": self.cik, "name": self.name, "category": self.category}


_LOCK = threading.RLock()
_CACHE: Optional[List[WatchedFund]] = None
_PATH_USED: Optional[str] = None


def _resolve_path() -> str:
    for p in _DEFAULT_PATHS:
        if p and os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError(
        "watched_funds.json not found in: " + ", ".join(p for p in _DEFAULT_PATHS if p)
    )


def _normalize_cik(raw: object) -> str:
    s = str(raw or "").strip()
    if not s.isdigit():
        return ""
    return s.zfill(10)


def _parse(path: str) -> List[WatchedFund]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    funds_raw = payload.get("funds") or []
    if not isinstance(funds_raw, list):
        raise ValueError("watched_funds.json `funds` must be a list")
    seen: set = set()
    out: List[WatchedFund] = []
    for row in funds_raw:
        if not isinstance(row, dict):
            continue
        cik = _normalize_cik(row.get("cik"))
        name = str(row.get("name") or "").strip()
        category = str(row.get("category") or "uncategorized").strip()
        if not cik or not name:
            continue
        key = (cik, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(WatchedFund(cik=cik, name=name, category=category))
    return out


def load_watched_funds() -> List[WatchedFund]:
    global _CACHE, _PATH_USED
    with _LOCK:
        if _CACHE is not None:
            return list(_CACHE)
        path = _resolve_path()
        _CACHE = _parse(path)
        _PATH_USED = path
        logger.info(
            "watched_funds loaded: count=%d path=%s",
            len(_CACHE), _PATH_USED,
        )
        return list(_CACHE)


def watched_fund_ciks() -> List[str]:
    """Unique list of CIKs across the roster (some CIKs appear under
    multiple names — e.g. a parent advisor with named sub-strategies)."""
    seen: set = set()
    out: List[str] = []
    for f in load_watched_funds():
        if f.cik in seen:
            continue
        seen.add(f.cik)
        out.append(f.cik)
    return out


def lookup_fund_name(cik: str) -> Optional[str]:
    """First name encountered for ``cik`` in the roster, or None."""
    target = _normalize_cik(cik)
    for f in load_watched_funds():
        if f.cik == target:
            return f.name
    return None


def reload() -> int:
    """Drop the cache so the next call re-reads the file. Returns the
    fresh count."""
    global _CACHE, _PATH_USED
    with _LOCK:
        _CACHE = None
        _PATH_USED = None
    funds = load_watched_funds()
    return len(funds)


__all__ = [
    "WatchedFund",
    "load_watched_funds",
    "watched_fund_ciks",
    "lookup_fund_name",
    "reload",
]
