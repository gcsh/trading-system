"""Stage-7 data-source health monitoring + SLO tracking.

The bot depends on several flaky feeds (yfinance, Cboe, NewsAPI, Anthropic).
When one quietly degrades the metrics layer can't tell what changed — until
trades start mis-firing. This module:

  • Records every feed access (success + latency)
  • Computes p50 / p95 / p99 latency per feed over a sliding window
  • Tracks last-success timestamp + minutes-since-last-success
  • Returns a unified ``FeedHealth`` report with a single ``ok`` verdict

Lightweight in-memory rolling buffers — no DB load. Reset on process
restart; the metrics flow naturally re-fill in the first few cycles.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FeedHealth:
    name: str
    success_count: int = 0
    failure_count: int = 0
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    p50_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    minutes_since_last_success: Optional[float] = None
    slo_breached: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── per-feed rolling state ────────────────────────────────────────────────


_WINDOW = 200
_STATE: Dict[str, Dict[str, Any]] = {}


def _feed_state(name: str) -> Dict[str, Any]:
    return _STATE.setdefault(name, {
        "latencies_ms": deque(maxlen=_WINDOW),
        "successes": 0, "failures": 0,
        "last_success_ts": None, "last_failure_ts": None,
    })


def record_success(feed: str, latency_ms: float) -> None:
    state = _feed_state(feed)
    state["successes"] += 1
    state["last_success_ts"] = time.time()
    state["latencies_ms"].append(max(0.0, float(latency_ms)))


def record_failure(feed: str, *, error: Optional[str] = None) -> None:
    state = _feed_state(feed)
    state["failures"] += 1
    state["last_failure_ts"] = time.time()
    if error:
        # we don't log the body verbose — just a counter is enough until
        # Stage 8 adds full incident records
        logger.debug("[monitoring] %s failure: %s", feed, error)


# ── feed health snapshots ────────────────────────────────────────────────


def _percentile(samples: List[float], p: float) -> Optional[float]:
    if not samples:
        return None
    s = sorted(samples)
    idx = int(round((len(s) - 1) * p))
    return round(s[idx], 2)


_SLO_MAX_MINUTES_STALE: Dict[str, float] = {
    "yfinance": 30.0,           # 30 min — soft target
    "cboe": 20.0,
    "anthropic": 60.0,
    "newsapi": 60.0,
    "finnhub": 30.0,
    "flashalpha": 15.0,
}


def feed_health(name: str) -> FeedHealth:
    """Build a per-feed snapshot."""
    state = _STATE.get(name)
    if state is None:
        return FeedHealth(name=name, notes=["no observations yet"])

    latencies = list(state["latencies_ms"])
    last_succ = state.get("last_success_ts")
    minutes_stale = ((time.time() - last_succ) / 60.0) if last_succ else None

    slo_max = _SLO_MAX_MINUTES_STALE.get(name)
    breached = bool(slo_max and (minutes_stale is None or minutes_stale > slo_max))

    return FeedHealth(
        name=name,
        success_count=state["successes"],
        failure_count=state["failures"],
        last_success_at=(datetime.utcfromtimestamp(last_succ).isoformat()
                          if last_succ else None),
        last_failure_at=(datetime.utcfromtimestamp(state["last_failure_ts"]).isoformat()
                          if state.get("last_failure_ts") else None),
        p50_ms=_percentile(latencies, 0.50),
        p95_ms=_percentile(latencies, 0.95),
        p99_ms=_percentile(latencies, 0.99),
        minutes_since_last_success=(round(minutes_stale, 2)
                                       if minutes_stale is not None else None),
        slo_breached=breached,
    )


def feed_summary() -> Dict[str, Any]:
    """Aggregate snapshot for every feed we've ever seen."""
    healths = [feed_health(name) for name in _STATE]
    breached = [h.name for h in healths if h.slo_breached]
    return {
        "feeds": [h.to_dict() for h in healths],
        "any_breach": bool(breached),
        "breached_feeds": breached,
        "tracked_feeds": list(_SLO_MAX_MINUTES_STALE.keys()),
    }


# ── helper: scope-aware timing ───────────────────────────────────────────


class timing:                       # noqa: N801 — kept lowercase for `with timing("x"):`
    """Context manager that records latency + success/failure to the feed.

    Example:
        with timing("yfinance"):
            t.history(...)
    """
    def __init__(self, feed: str):
        self.feed = feed
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        dt_ms = (time.monotonic() - self._t0) * 1000
        if exc_type is None:
            record_success(self.feed, dt_ms)
        else:
            record_failure(self.feed, error=str(exc_val))
        return False
