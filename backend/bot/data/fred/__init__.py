"""Stage-18a — FRED (Federal Reserve Economic Data) client + cache.

Free public API at https://api.stlouisfed.org/fred/series/observations. The
SDK pattern matches every other data source in the codebase: a thin
client with an injectable HTTP shim for testing, a daily fetcher that
writes into SQLite, and helper functions that downstream consumers
(macro agent, regime classifier, cross-asset state) read locally.

Series we care about (the canonical macro panel):

  • DFF        — Effective Fed Funds rate
  • DGS10      — 10-Year Treasury yield
  • DGS2       — 2-Year Treasury yield
  • UNRATE     — Civilian unemployment rate (monthly)
  • CPIAUCSL   — CPI all-urban consumers (monthly)
  • M2SL       — M2 money stock (monthly)
  • BAMLH0A0HYM2 — High-yield option-adjusted spread
  • NFCI       — Chicago Fed financial conditions index (weekly)

No FRED API key configured → fetcher is a graceful no-op. All helper
functions return ``None`` or empty lists so downstream callers never
crash.

Rate limit: FRED allows ~120 requests/minute. We fetch once per series
per day max, so we're orders of magnitude under.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.fred_observation import FredObservation

logger = logging.getLogger(__name__)


# Canonical FRED macro panel — when these are missing the bot falls
# through to the original VIX-only macro layer.
CANONICAL_SERIES: Tuple[str, ...] = (
    "DFF", "DGS10", "DGS2", "UNRATE", "CPIAUCSL", "M2SL",
    "BAMLH0A0HYM2", "NFCI",
)


# ── client ──────────────────────────────────────────────────────────────


@dataclass
class FredObs:
    date: date
    value: Optional[float]


class FredRateLimited(Exception):
    """Raised when FRED returns HTTP 429. Handled by callers as a
    transient condition (back off, retry), not a warning-worthy error —
    FRED's public bucket gets hot at the top of every minute, especially
    during the U.S. trading session."""

    def __init__(self, retry_after: float = 30.0) -> None:
        super().__init__(f"FRED rate-limited; retry-after {retry_after}s")
        self.retry_after = retry_after


def _default_fetcher(series_id: str, *, api_key: str,
                        limit: int = 365) -> List[FredObs]:
    """Hit the live FRED endpoint. Uses ``requests`` (which auto-bundles
    certifi's CA store) rather than ``urllib.request`` — the latter
    fails on macOS Python 3.14 with SSL: CERTIFICATE_VERIFY_FAILED."""
    import requests
    resp = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id, "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc", "limit": limit,
        },
        timeout=15,
    )
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After")
        try:
            wait = float(retry) if retry else 30.0
        except (TypeError, ValueError):
            wait = 30.0
        raise FredRateLimited(retry_after=min(60.0, max(5.0, wait)))
    resp.raise_for_status()
    payload = resp.json()
    out: List[FredObs] = []
    for row in payload.get("observations") or []:
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        raw = row.get("value")
        if raw in (None, "", "."):
            v: Optional[float] = None
        else:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                v = None
        out.append(FredObs(date=d, value=v))
    return out


class FredClient:
    """Stateful client — caches the API key + the fetcher for testability."""

    def __init__(self, *, api_key: Optional[str] = None,
                    fetcher: Optional[Callable[..., List[FredObs]]] = None) -> None:
        self._api_key = api_key
        self._fetcher = fetcher or _default_fetcher

    def _key(self) -> str:
        # Explicit None → fall through to TUNABLES (env). Explicit "" →
        # disable (overrides any global key). Matches the memo /
        # narrative / brain pattern across the codebase.
        if self._api_key is not None:
            return self._api_key
        return getattr(TUNABLES, "fred_api_key", "") or ""

    @property
    def available(self) -> bool:
        return bool(self._key())

    def fetch_series(self, series_id: str, *, limit: int = 365
                        ) -> List[FredObs]:
        if not self.available:
            return []
        try:
            obs = self._fetcher(series_id, api_key=self._key(), limit=limit)
        except FredRateLimited as rl:
            # Transient: public-bucket exhausted. Caller already paces.
            # Log at INFO so the UI warnings panel doesn't ring red.
            logger.info("fred 429 for %s (retry-after %.1fs)",
                            series_id, rl.retry_after)
            return []
        except Exception:
            logger.warning("fred fetch failed for %s", series_id, exc_info=True)
            return []
        # MITS Phase 8.2 — capture the raw FRED series to bronze.
        try:
            from backend.bot.data import lake as _lake
            _lake.write_bronze(
                "fred", "series",
                [{"series_id": series_id, "date": o.date.isoformat(),
                    "value": o.value} for o in obs],
                extra_tags={"series_id": series_id},
                request_url=f"fred://series/{series_id}",
                source_version=__name__,
            )
        except Exception:
            pass
        return obs


# ── caching + persistence ───────────────────────────────────────────────


def _upsert_observations(series_id: str, obs: List[FredObs]) -> int:
    """Write observations to SQLite, returning number of new rows."""
    if not obs:
        return 0
    new_count = 0
    try:
        with session_scope() as session:
            # Pull dates we already have to avoid duplicate inserts.
            existing_rows = session.execute(
                select(FredObservation.date)
                .where(FredObservation.series_id == series_id)
            ).scalars().all()
            existing_dates = {d.date() if hasattr(d, "date") else d
                                 for d in existing_rows}
            for o in obs:
                if o.date in existing_dates:
                    continue
                session.add(FredObservation(
                    series_id=series_id,
                    date=datetime(o.date.year, o.date.month, o.date.day),
                    value=o.value,
                ))
                new_count += 1
    except Exception:
        logger.exception("fred upsert failed for %s", series_id)
    return new_count


def refresh(*, series: Optional[List[str]] = None,
               limit: int = 365,
               client: Optional[FredClient] = None) -> Dict[str, Any]:
    """Fetch the canonical macro panel (or a custom subset) and cache.

    Returns ``{series_id: rows_inserted}`` plus an ``"available"`` flag so
    the caller (scheduler / endpoint) can log the result.
    """
    import time
    cl = client or FredClient()
    out: Dict[str, Any] = {"available": cl.available, "results": {}}
    if not cl.available:
        out["reason"] = "no FRED API key configured (set TB_FRED_API_KEY)"
        return out
    # FRED's free tier rate-limits at ~120 requests/minute. Refreshing
    # all canonical series concurrently used to slam the endpoint and
    # collect a wave of 429s. Pace ourselves with a 600ms gap (≈100/min,
    # comfortably under the limit) and skip on 429 to give the bucket
    # time to refill instead of hammering harder.
    consecutive_429s = 0
    for sid in series or CANONICAL_SERIES:
        obs = cl.fetch_series(sid, limit=limit)
        out["results"][sid] = _upsert_observations(sid, obs)
        # If the last fetch returned empty (likely 429), back off harder.
        if not obs:
            consecutive_429s += 1
            time.sleep(min(5.0, 0.6 * (2 ** consecutive_429s)))
        else:
            consecutive_429s = 0
            time.sleep(0.6)
    return out


# ── helpers (consumed by agents / regime / endpoints) ──────────────────


def latest(series_id: str) -> Optional[FredObs]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(FredObservation)
                .where(FredObservation.series_id == series_id)
                .where(FredObservation.value.is_not(None))
                .order_by(desc(FredObservation.date))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return FredObs(
                date=row.date.date() if hasattr(row.date, "date") else row.date,
                value=row.value,
            )
    except Exception:
        return None


def history(series_id: str, *, limit: int = 252) -> List[FredObs]:
    """Return up to ``limit`` most recent observations, newest-first."""
    try:
        with session_scope() as session:
            rows = session.execute(
                select(FredObservation)
                .where(FredObservation.series_id == series_id)
                .order_by(desc(FredObservation.date))
                .limit(limit)
            ).scalars().all()
            return [
                FredObs(
                    date=r.date.date() if hasattr(r.date, "date") else r.date,
                    value=r.value,
                )
                for r in rows
            ]
    except Exception:
        return []


def change_pct(series_id: str, *, days: int = 30) -> Optional[float]:
    """% change over the last ``days`` calendar days. None when insufficient
    history or both endpoints are missing."""
    rows = history(series_id, limit=max(days * 2, 60))
    if not rows:
        return None
    newest = rows[0]
    if newest.value is None:
        return None
    target = newest.date - timedelta(days=days)
    # Find the row closest to target by going backward.
    older = None
    for r in rows[1:]:
        if r.date <= target and r.value is not None:
            older = r
            break
    if older is None or older.value in (None, 0):
        return None
    return round((newest.value - older.value) / older.value, 4)


def yield_curve_inverted() -> Optional[bool]:
    """``True`` when the latest DGS10 - DGS2 spread is negative.
    Returns ``None`` when either series is missing."""
    ten = latest("DGS10")
    two = latest("DGS2")
    if ten is None or two is None or ten.value is None or two.value is None:
        return None
    return (ten.value - two.value) < 0


def macro_snapshot() -> Dict[str, Any]:
    """A single compact snapshot of the canonical panel — designed to
    bolt onto ``MarketState`` and the macro agent. Every field can be
    None (cold start, missing key) so consumers handle gracefully."""
    out: Dict[str, Any] = {}
    for sid in CANONICAL_SERIES:
        l = latest(sid)
        out[sid] = {
            "value": l.value if l else None,
            "date": l.date.isoformat() if l else None,
            "change_30d_pct": change_pct(sid, days=30),
        }
    out["yield_curve_inverted"] = yield_curve_inverted()
    # Synthesize the 10Y-2Y spread as its own field — a single number is
    # easier for consumers than two lookups.
    ten = latest("DGS10")
    two = latest("DGS2")
    if ten and two and ten.value is not None and two.value is not None:
        out["spread_10y_2y"] = round(ten.value - two.value, 3)
    else:
        out["spread_10y_2y"] = None
    return out
