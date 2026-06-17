"""MITS Phase 11.F — expanded FRED macro panel (~50 series).

Phase 11 widens the FRED corpus from the original 8-series canonical
panel to a ~50-series macro super-set covering:

    Yield curve, inflation, employment, real-economy activity,
    Fed balance sheet, equity/vol gauges, credit spreads, FX,
    commodities, and housing.

The orchestrator-shaped callback :func:`fred_backfill_callback` is a
thin adapter over the existing :class:`backend.bot.data.fred.FredClient`
— same client, same upsert path, but now wrapped in
:class:`CallbackResult` so the SyncOrchestrator drives retries +
watermarks + progress.

Each series is fetched with ``limit=12000`` on the first backfill (FRED
caps at 100000; 12000 covers daily series back to ~1980 with margin).
Subsequent delta syncs use a smaller limit.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Tuple

from sqlalchemy import select

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.db import session_scope
from backend.models.fred_observation import FredObservation

logger = logging.getLogger(__name__)


# ── series catalog ────────────────────────────────────────────────────


# (series_id, category) — 50 series. Categorized so future surfaces can
# group them; order preserved for deterministic backfill sequencing.
EXPANDED_SERIES_WITH_CATEGORY: Tuple[Tuple[str, str], ...] = (
    # ── yield curve ──
    ("DGS3MO",  "yield_curve"),
    ("DGS6MO",  "yield_curve"),
    ("DGS2",    "yield_curve"),
    ("DGS5",    "yield_curve"),
    ("DGS10",   "yield_curve"),
    ("DGS30",   "yield_curve"),
    ("T10Y2Y",  "yield_curve"),
    ("T10Y3M",  "yield_curve"),
    # ── inflation ──
    ("CPIAUCSL", "inflation"),
    ("CPILFESL", "inflation"),
    ("PCEPI",    "inflation"),
    ("PCEPILFE", "inflation"),
    ("T10YIE",   "inflation"),  # 10y breakeven
    # ── employment ──
    ("UNRATE",  "employment"),
    ("ICSA",    "employment"),
    ("CCSA",    "employment"),
    ("PAYEMS",  "employment"),
    ("U6RATE",  "employment"),
    # ── activity ──
    ("INDPRO",  "activity"),
    ("HOUST",   "activity"),
    ("RSAFS",   "activity"),
    ("RRSFS",   "activity"),
    ("TCU",     "activity"),
    # ── money / fed ──
    ("FEDFUNDS", "money_fed"),
    ("DFF",      "money_fed"),
    ("M2SL",     "money_fed"),
    ("WALCL",    "money_fed"),
    ("M2REAL",   "money_fed"),
    # ── markets (vol gauges) ──
    ("VIXCLS",   "markets"),
    ("VXNCLS",   "markets"),
    ("VXVCLS",   "markets"),
    # ── credit ──
    ("BAMLH0A0HYM2", "credit"),
    ("BAMLC0A0CM",   "credit"),
    ("NFCI",         "credit"),
    ("DTB3",         "credit"),
    ("DTB6",         "credit"),
    # ── currency ──
    ("DTWEXBGS", "currency"),
    ("DEXUSEU",  "currency"),
    ("DEXCHUS",  "currency"),
    ("DEXJPUS",  "currency"),
    # ── commodities ──
    ("DCOILWTICO",          "commodities"),
    ("DCOILBRENTEU",        "commodities"),
    ("GOLDAMGBD228NLBM",    "commodities"),
    ("NATGASEU",            "commodities"),
    # ── housing / misc ──
    ("MORTGAGE30US", "housing"),
    ("CSUSHPISA",    "housing"),
)


EXPANDED_SERIES: Tuple[str, ...] = tuple(
    sid for sid, _ in EXPANDED_SERIES_WITH_CATEGORY
)


def series_category(series_id: str) -> str:
    for sid, cat in EXPANDED_SERIES_WITH_CATEGORY:
        if sid == series_id:
            return cat
    return "uncategorized"


# ── backfill ──────────────────────────────────────────────────────────


def backfill_series(series_id: str, *, limit: int = 12000) -> dict:
    """One-shot pull of ``series_id`` from FRED, upsert into
    ``fred_observations``. Returns ``{"inserted": N, "fetched": M}``.

    No-op + empty result when no API key is configured.
    """
    from backend.bot.data.fred import FredClient, _upsert_observations
    cl = FredClient()
    if not cl.available:
        return {"inserted": 0, "fetched": 0, "reason": "no_api_key"}
    obs = cl.fetch_series(series_id, limit=limit)
    inserted = _upsert_observations(series_id, obs)
    return {"inserted": inserted, "fetched": len(obs)}


def _max_existing_date(series_id: str) -> date:
    try:
        with session_scope() as s:
            row = s.execute(
                select(FredObservation.date)
                .where(FredObservation.series_id == series_id)
                .where(FredObservation.value.is_not(None))
                .order_by(FredObservation.date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return date(1990, 1, 1)
            return row.date() if hasattr(row, "date") else row
    except Exception:
        return date(1990, 1, 1)


def fred_backfill_callback(series_id: str, chunk_start: date,
                                  chunk_end: date) -> CallbackResult:
    """Orchestrator-shaped FRED callback. ``ticker`` here is the FRED
    ``series_id`` (we route it through the same orchestrator API so the
    watermark + progress ledger get free coverage)."""
    if chunk_start > chunk_end:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "empty_window"},
        )
    # Pick limit based on chunk width — a 5y chunk for a daily series
    # needs ~1300 rows, a 25y backfill needs ~6500. Use the full FRED
    # limit on first hit; subsequent (smaller) chunks ask for less.
    span_days = max(1, (chunk_end - chunk_start).days)
    if span_days > 365 * 10:
        limit = 12000
    elif span_days > 365 * 3:
        limit = 4000
    elif span_days > 90:
        limit = 1200
    else:
        limit = 300
    stats = backfill_series(series_id, limit=limit)
    # Cold-start guard: when no API key is configured ``backfill_series``
    # returns ``{"inserted": 0, "fetched": 0, "reason": "no_api_key"}``.
    # We must NOT mark the chunk done in that case — otherwise the next
    # run sees ``status=done`` and skips it forever, even after the
    # operator sets ``TB_FRED_API_KEY``. Raise so the orchestrator
    # marks the chunk error + leaves it retryable.
    if stats.get("reason") == "no_api_key":
        raise RuntimeError(
            "fred: no API key configured (set TB_FRED_API_KEY); "
            f"series={series_id} chunk=[{chunk_start},{chunk_end}]"
        )
    inserted = int(stats.get("inserted") or 0)
    last_seen = _max_existing_date(series_id)
    # ``last_seen`` from a never-fetched series is 1990-01-01 (cold start
    # sentinel). When the chunk wrote zero rows, advance the watermark
    # only up to ``last_seen`` so we don't claim coverage of dates we
    # never actually pulled.
    if inserted == 0 and last_seen.year <= 1990:
        # Chunk genuinely empty — FRED returned no observations. This
        # usually means the chunk window predates the series' first
        # publication (e.g. T10YIE only goes back to 2003). Mark done
        # at ``chunk_end`` so we don't loop on this gap forever.
        last_complete = chunk_end
    else:
        last_complete = min(chunk_end, last_seen)
    return CallbackResult(
        last_completed_date=last_complete,
        rows_written=inserted,
        metadata={"fred_stats": stats},
    )


def expanded_macro_snapshot() -> dict:
    """Like :func:`backend.bot.data.fred.macro_snapshot` but covering the
    full 50-series Phase 11 panel. Returns ``{series_id: {value, date,
    change_30d_pct, category}, ...}`` so downstream surfaces (agent
    context, narrative agent, regime detector) can introspect any series
    without a separate query.

    Every field can be ``None`` (cold start, missing key) so consumers
    handle gracefully.
    """
    from backend.bot.data.fred import (
        change_pct, latest, yield_curve_inverted,
    )
    out: dict = {}
    for sid, cat in EXPANDED_SERIES_WITH_CATEGORY:
        obs = latest(sid)
        out[sid] = {
            "value": obs.value if obs else None,
            "date": obs.date.isoformat() if obs else None,
            "change_30d_pct": change_pct(sid, days=30),
            "category": cat,
        }
    out["yield_curve_inverted"] = yield_curve_inverted()
    ten = latest("DGS10")
    two = latest("DGS2")
    if ten and two and ten.value is not None and two.value is not None:
        out["spread_10y_2y"] = round(ten.value - two.value, 3)
    else:
        out["spread_10y_2y"] = None
    return out


def series_by_category() -> dict:
    """``{category: [series_id, ...]}`` for UI grouping."""
    grouped: dict = {}
    for sid, cat in EXPANDED_SERIES_WITH_CATEGORY:
        grouped.setdefault(cat, []).append(sid)
    return grouped


__all__ = [
    "EXPANDED_SERIES",
    "EXPANDED_SERIES_WITH_CATEGORY",
    "series_category",
    "series_by_category",
    "backfill_series",
    "fred_backfill_callback",
    "expanded_macro_snapshot",
]
