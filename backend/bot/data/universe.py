"""MITS Phase 11.A — A-grade 40-ticker universe loader.

Single source of truth for the universe of tickers the bot trades, watches,
and backfills against. Reads ``/opt/trading-bot/universe.json`` (or a
local repo-relative copy during dev) and exposes:

  - ``load_universe()`` -> ``List[str]`` — canonical ordered list of tickers.
  - ``is_in_universe(ticker)`` -> ``bool`` — quick membership check.
  - ``universe_buckets()`` -> ``Dict[str, List[str]]`` — sector buckets.
  - ``reload_if_changed()`` -> ``bool`` — mtime-based refresh hook.

Cached at process startup. The mtime guard means an operator can edit
universe.json on disk and the next ``load_universe()`` call picks it up
without a service restart — useful during the corpus rebuild when the
universe might widen by a few tickers.

Why a separate module instead of a config dict: the universe drives
every backfill loop, the EOD pass, the scheduler scan list, and the
13F target list. Keeping a typed, single-purpose loader avoids the
"is this the canonical list or just a watchlist subset?" ambiguity
that bit us during Phase 5.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# Repo root may be deployed at /opt/trading-bot (EC2) or at the dev
# checkout root. We check both, preferring the env-overridable path so
# operators can pin a non-default path without code changes.
_DEFAULT_PATHS = (
    os.environ.get("TB_UNIVERSE_PATH") or "",
    "/opt/trading-bot/universe.json",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "universe.json"),
)


# Symbol grammar matches CBOE/OPRA root-symbol rules: 1-6 alphanumeric
# characters optionally followed by a single "." plus 1-2 letters
# (e.g. ``BRK.B``). We accept that one weird shape because it's the
# canonical ticker for Berkshire-B. Rejecting it would force the
# operator to keep two lists in sync.
_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z]{1,2})?$")


@dataclass
class UniverseSnapshot:
    """One frozen read of universe.json — what the bot is acting on."""
    version: str
    description: str
    tickers: List[str]
    buckets: Dict[str, List[str]]
    count: int
    criteria: List[str]
    source_path: str
    source_mtime: float

    def is_member(self, ticker: str) -> bool:
        if not ticker:
            return False
        return ticker.strip().upper() in self._set

    def __post_init__(self) -> None:
        self._set = {t.upper() for t in self.tickers}


_LOCK = threading.RLock()
_CACHE: Optional[UniverseSnapshot] = None


# ── loader ─────────────────────────────────────────────────────────────


def _candidate_paths() -> List[str]:
    return [p for p in _DEFAULT_PATHS if p]


def _resolve_path() -> str:
    for path in _candidate_paths():
        if os.path.exists(path):
            return os.path.abspath(path)
    # Fall through — caller will hit a FileNotFoundError with the most
    # useful diagnostic ("we looked here:") instead of a default that
    # masks the misconfiguration.
    raise FileNotFoundError(
        "universe.json not found in any of: " + ", ".join(_candidate_paths())
    )


def _parse_universe_file(path: str) -> UniverseSnapshot:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    tickers_raw = payload.get("tickers") or []
    if not isinstance(tickers_raw, list):
        raise ValueError(
            f"universe.json `tickers` must be a list, got {type(tickers_raw).__name__}"
        )
    tickers = [str(t).strip().upper() for t in tickers_raw if str(t).strip()]
    seen: List[str] = []
    duplicates: List[str] = []
    for t in tickers:
        if t in seen:
            duplicates.append(t)
        else:
            seen.append(t)
    if duplicates:
        raise ValueError(f"universe.json contains duplicate tickers: {duplicates}")
    invalid = [t for t in tickers if not _SYMBOL_RE.match(t)]
    if invalid:
        raise ValueError(f"universe.json contains invalid symbols: {invalid}")
    declared = payload.get("count")
    if declared is not None and int(declared) != len(tickers):
        raise ValueError(
            f"universe.json count={declared} disagrees with ticker list length "
            f"({len(tickers)})"
        )
    buckets_raw = payload.get("buckets") or {}
    buckets: Dict[str, List[str]] = {}
    for name, syms in buckets_raw.items():
        if not isinstance(syms, list):
            continue
        buckets[str(name)] = [str(s).strip().upper() for s in syms
                              if str(s).strip()]
    return UniverseSnapshot(
        version=str(payload.get("version") or ""),
        description=str(payload.get("description") or ""),
        tickers=tickers,
        buckets=buckets,
        count=len(tickers),
        criteria=list(payload.get("criteria") or []),
        source_path=path,
        source_mtime=os.path.getmtime(path),
    )


def get_snapshot() -> UniverseSnapshot:
    """Return the cached snapshot, loading on first call."""
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            path = _resolve_path()
            _CACHE = _parse_universe_file(path)
            logger.info(
                "universe loaded: version=%s count=%d path=%s",
                _CACHE.version, _CACHE.count, _CACHE.source_path,
            )
        return _CACHE


def reload_if_changed() -> bool:
    """Re-read universe.json if its mtime has advanced. Returns True when
    the cache was refreshed. Cheap to call every cycle — a stat() is the
    only cost on the steady-state path."""
    global _CACHE
    with _LOCK:
        try:
            path = _resolve_path()
            mtime = os.path.getmtime(path)
        except Exception:
            return False
        if _CACHE is not None and mtime <= _CACHE.source_mtime \
                and path == _CACHE.source_path:
            return False
        try:
            new_snap = _parse_universe_file(path)
        except Exception:
            logger.exception("universe reload failed; keeping previous snapshot")
            return False
        old_count = _CACHE.count if _CACHE else 0
        _CACHE = new_snap
        logger.info(
            "universe reloaded: %d -> %d tickers (mtime=%s)",
            old_count, new_snap.count, mtime,
        )
        return True


def load_universe() -> List[str]:
    """Canonical ordered list of tickers in the universe.

    Calls ``reload_if_changed()`` first so an edited universe.json is
    picked up without a service restart.
    """
    reload_if_changed()
    return list(get_snapshot().tickers)


def is_in_universe(ticker: str) -> bool:
    if not ticker:
        return False
    return get_snapshot().is_member(ticker)


def universe_buckets() -> Dict[str, List[str]]:
    return dict(get_snapshot().buckets)


def universe_version() -> str:
    return get_snapshot().version


def universe_count() -> int:
    return get_snapshot().count


# ── watchlist seeding ──────────────────────────────────────────────────


def seed_watchlist(list_name: str = "default",
                   *, extras: Optional[Sequence[str]] = None) -> Dict[str, int]:
    """Ensure every universe ticker exists on the ``list_name`` watchlist.

    Idempotent: tickers already on the watchlist are left alone. Returns
    ``{"added": N, "already_present": M, "extras_added": K}`` so the
    caller can confirm what happened.

    ``extras`` is an optional list of additional tickers to seed alongside
    the universe (for legacy operator picks that aren't in the A-grade
    list yet).
    """
    from sqlalchemy import select
    from backend.db import session_scope
    from backend.models.watchlist import WatchlistItem

    tickers = load_universe()
    extras_norm = [t.strip().upper() for t in (extras or []) if t and str(t).strip()]
    target = list(tickers)
    for t in extras_norm:
        if t not in target:
            target.append(t)

    added = 0
    already = 0
    extras_added = 0
    with session_scope() as session:
        existing_rows = session.execute(
            select(WatchlistItem.ticker)
            .where(WatchlistItem.list_name == list_name)
        ).scalars().all()
        existing = {t.upper() for t in existing_rows if t}
        for ticker in target:
            if ticker in existing:
                already += 1
                continue
            session.add(WatchlistItem(
                list_name=list_name, ticker=ticker,
                notes=("MITS Phase 11 universe seed"
                       if ticker in set(tickers) else "operator extra"),
            ))
            if ticker in set(tickers):
                added += 1
            else:
                extras_added += 1
    return {
        "added": added,
        "already_present": already,
        "extras_added": extras_added,
        "list_name": list_name,
        "universe_size": len(tickers),
    }


__all__ = [
    "UniverseSnapshot",
    "get_snapshot",
    "load_universe",
    "is_in_universe",
    "universe_buckets",
    "universe_version",
    "universe_count",
    "reload_if_changed",
    "seed_watchlist",
]
