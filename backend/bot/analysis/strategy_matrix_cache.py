"""MITS Phase 18-FU Gap R3 — TTL cache for per-cycle StrategyMatrix.

The matcher itself (``backend.bot.analysis.strategy_matrix.build_strategy_matrix``)
is pure compute plus a pgvector ``retrieve_analogs`` call plus 10 cohort
DB lookups (one per template). Cold build is ~100-200 ms. Within a
5-minute window for the same ticker + regime the result barely moves,
so this module wraps the build in an LRU+TTL cache.

Cache key: ``(TICKER, regime_vector_hash, time_bucket)``.

* ``regime_vector_hash`` collapses the RegimeVector to a 12-char hex
  digest of its ``to_dict()`` JSON so two cycles with identical regime
  hit the same slot. Two cycles with a flipped regime miss and rebuild.
* ``time_bucket = int(time.time() // ttl_sec)`` so bucket transitions
  happen on wall-clock boundaries — not per-key staleness — which keeps
  the eviction story simple and the cache hit rate observable.

Eviction: ``collections.OrderedDict`` with LRU shedding once
``max_size`` is exceeded. Default ``max_size`` is sized for ~40 tickers
× 5 buckets with a cushion (200). Thread-safe via a ``threading.Lock``.

Public surface:
    - ``get_or_build(ticker, regime_vector, signal, analytics)`` — main
      entry point. Returns ``(matrix_dict, top_strategy_dict)`` exactly
      like the legacy ``_build_engine_strategy_matrix`` did, or ``(None,
      None)`` if the build raised.
    - ``stats()`` — returns ``{"hits", "misses", "size", "max_size",
      "ttl_sec"}`` for the observability gate (Gate D + funnel
      recompute).
    - ``clear()`` — drops all entries; only tests and operator forced
      resets should call it.

Failure mode: ALWAYS fail-open. A pgvector outage, a template-loader
hiccup, or a posterior-DB exception must NEVER block a decision; the
function logs at DEBUG, increments ``_build_errors``, and returns
``(None, None)`` so the engine falls back to "no strategy matrix this
cycle" — the policy chain then proceeds normally and the consensus rule
will try its own build (also wrapped here, also fail-open).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


_STATE_LOCK = threading.Lock()
_CACHE: "OrderedDict[Tuple[str, str, int], Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]" = OrderedDict()
_HITS = 0
_MISSES = 0
_BUILD_ERRORS = 0


def _ttl_sec() -> int:
    """Operator-tunable bucket width. Defaults to 300 (5 min)."""
    return max(1, int(getattr(TUNABLES, "strategy_matrix_cache_ttl_sec", 300)))


def _max_size() -> int:
    """LRU shed threshold. Defaults to 200 (40 tickers × 5 buckets + cushion)."""
    return max(1, int(getattr(TUNABLES, "strategy_matrix_cache_max_size", 200)))


def _regime_hash(regime_vector) -> str:
    """Collapse the RegimeVector to a stable 12-char digest. Failure
    bypasses the cache by returning a unique sentinel — that's the
    fail-open contract."""
    try:
        if regime_vector is None:
            return "none"
        if hasattr(regime_vector, "to_dict"):
            payload = regime_vector.to_dict()
        elif isinstance(regime_vector, dict):
            payload = regime_vector
        else:
            return f"unhashable-{id(regime_vector)}"
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    except Exception:
        # Per-call unique sentinel — guarantees a miss, never a stale hit.
        return f"err-{time.time_ns()}"


def _bucket(now: Optional[float] = None) -> int:
    """Wall-clock TTL bucket. Externalized for tests."""
    return int((now if now is not None else time.time()) // _ttl_sec())


def _put(key: Tuple[str, str, int], value: Tuple[Dict[str, Any], Optional[Dict[str, Any]]]) -> None:
    """Insert with LRU eviction. Caller MUST hold ``_STATE_LOCK``."""
    _CACHE[key] = value
    _CACHE.move_to_end(key, last=True)
    cap = _max_size()
    while len(_CACHE) > cap:
        _CACHE.popitem(last=False)


def _get(key: Tuple[str, str, int]) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """LRU-promoting fetch. Caller MUST hold ``_STATE_LOCK``."""
    if key not in _CACHE:
        return None
    val = _CACHE[key]
    _CACHE.move_to_end(key, last=True)
    return val


def get_or_build(
    *,
    ticker: str,
    regime_vector,
    signal,
    analytics: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return ``(matrix_dict, top_strategy_dict)`` for this ticker.

    The signature mirrors the legacy ``_build_engine_strategy_matrix``
    in ``backend/bot/decision/rules.py`` so callers can swap in cleanly.

    * Cache HIT: returns the cached pair (LRU re-promote).
    * Cache MISS: invokes ``build_strategy_matrix`` with the same input
      assembly the legacy helper did, stores the result, returns the
      pair.
    * Any exception inside the build: returns ``(None, None)``. The
      ``_build_errors`` counter ticks so operators can audit silent
      failures via ``stats()``.
    """
    global _HITS, _MISSES, _BUILD_ERRORS
    tk = (ticker or "").upper().strip() or "UNKNOWN"
    rh = _regime_hash(regime_vector)
    key = (tk, rh, _bucket())

    with _STATE_LOCK:
        hit = _get(key)
        if hit is not None:
            _HITS += 1
            return hit
        _MISSES += 1

    # Build OUTSIDE the lock — pgvector + posterior DB calls take real
    # milliseconds; holding the lock would serialize cycles unnecessarily.
    try:
        matrix_dict, top_strategy_dict = _do_build(
            ticker=tk, regime_vector=regime_vector,
            signal=signal, analytics=analytics,
        )
    except Exception:
        with _STATE_LOCK:
            _BUILD_ERRORS += 1
        logger.debug(
            "strategy_matrix_cache build raised for %s — failing open",
            tk, exc_info=True,
        )
        return None, None

    if matrix_dict is None:
        # Builder returned nothing actionable (e.g. zero candidates). Don't
        # poison the cache with empty results — next call rebuilds.
        return None, None

    with _STATE_LOCK:
        _put(key, (matrix_dict, top_strategy_dict))
    return matrix_dict, top_strategy_dict


def _do_build(
    *,
    ticker: str,
    regime_vector,
    signal,
    analytics: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Assemble the same inputs the legacy engine helper did and call
    ``build_strategy_matrix``. Kept package-private so test mocks can
    target ``strategy_matrix_cache._do_build``."""
    from backend.bot.analysis.strategy_matrix import build_strategy_matrix
    from backend.bot.corpus.analog_retrieval import retrieve_analogs

    pattern_label: Optional[str] = None
    md = getattr(signal, "metadata", None) or {}
    if md.get("pattern"):
        pattern_label = str(md.get("pattern"))
    elif getattr(signal, "strategy", None):
        pattern_label = str(signal.strategy)
    pattern_hits = [{"pattern": pattern_label}] if pattern_label else []

    analogs = retrieve_analogs(
        ticker=ticker, regime_vector=regime_vector,
        pattern=pattern_label or "unknown",
        horizon="5d", k=50, sector_fallback=True,
    )

    feats = (analytics or {}).get("features") or {}
    iv_rank = feats.get("iv_rank")
    iv_regime_label: Optional[str] = None
    current_iv: Optional[float] = None
    try:
        from backend.bot.iv_regime import classify_ticker
        report = classify_ticker(ticker)
        iv_regime_label = report.regime
        current_iv = report.current_iv
    except Exception:
        pass
    iv_state = {
        "iv_rank": iv_rank,
        "iv_regime": iv_regime_label,
        "current_iv": current_iv,
    }

    sm = build_strategy_matrix(
        ticker=ticker, regime_vector=regime_vector,
        pattern_hits=pattern_hits, analogs=analogs,
        iv_state=iv_state, greeks=None,
    )
    sm_dict = sm.to_dict() if sm is not None else None
    top = sm_dict.get("top_strategy") if isinstance(sm_dict, dict) else None
    return sm_dict, top


def stats() -> Dict[str, Any]:
    """Observability surface. Read by Gate D and the funnel recompute."""
    with _STATE_LOCK:
        return {
            "hits": int(_HITS),
            "misses": int(_MISSES),
            "build_errors": int(_BUILD_ERRORS),
            "size": int(len(_CACHE)),
            "max_size": int(_max_size()),
            "ttl_sec": int(_ttl_sec()),
        }


def clear() -> None:
    """Drop all entries + reset counters. TEST/operator use only."""
    global _HITS, _MISSES, _BUILD_ERRORS
    with _STATE_LOCK:
        _CACHE.clear()
        _HITS = 0
        _MISSES = 0
        _BUILD_ERRORS = 0


__all__ = ["get_or_build", "stats", "clear"]
