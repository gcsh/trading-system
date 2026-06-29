"""MITS Phase 6 (P6.2 + P6.3) + Phase 12.J + Phase 12.2 — Detector
scorecard, suggestion, and edge endpoints.

Read-only over Trade rows + MarketObservation + MarketOutcome +
KnowledgeGraphCell + DetectorConfig + DetectorSuggestion. No writes
outside of the explicit `accept` / `dismiss` POST endpoints.

Phase 12.J added:

  * GET /detectors/edge      — per-detector 5d win rate vs baseline.
  * GET /detectors/edge/families — family rollup.

Phase 12.2 replaced the static TUNABLES.detector_baseline_5d_win_rate
(0.689 — stale, single-axis) with a dynamic per-direction baseline
computed from the live corpus and cached 5 minutes. Each detector is
now compared to ITS OWN direction's baseline so longs vs shorts no
longer share an artificial 68.9% reference that produced -68pp ghost
edges. Family rollups use a sample-size-weighted average of per-
detector edges (NOT a family-wide WR vs single baseline) so a family
of 10 short detectors isn't ranked against a long baseline.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text

from backend.bot.detectors import (
    DETECTOR_REGISTRY,
    clear_detector_config_cache,
)
from backend.bot.scorecard.detector_scorecard import (
    build_detector_scorecard,
    build_leaderboard,
    cumulative_pnl_series,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.detector_config import DetectorConfig
from backend.models.detector_suggestion import (
    DetectorSuggestion,
    REASON_LOW_POSTERIOR,
    REASON_RECOVERED_POSTERIOR,
    SUGGESTION_STATUS_ACCEPTED,
    SUGGESTION_STATUS_DISMISSED,
    SUGGESTION_STATUS_PENDING,
)
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome

logger = logging.getLogger(__name__)

router = APIRouter(tags=["detector_scorecard"])


_VALID_WINDOWS = {"7", "30", "all"}


def _normalize_window(window: str) -> str:
    w = (window or "30").strip().lower()
    if w not in _VALID_WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=f"window must be one of {_VALID_WINDOWS}",
        )
    return w


@router.get("/detectors/scorecard")
async def leaderboard(window: str = Query("30")) -> Dict[str, Any]:
    """Leaderboard of every detector sorted by attribution_score.

    Returns:
      {"window": "30", "detectors": [scorecard, ...]}
    """
    w = _normalize_window(window)
    rows = build_leaderboard(window=w)
    return {"window": w, "detectors": rows, "count": len(rows)}


@router.get("/detectors/{name}/scorecard")
async def detector_scorecard(name: str,
                                          window: str = Query("30"),
                                          include_series: bool = Query(False),
                                          ) -> Dict[str, Any]:
    """Per-detector scorecard. Pass ``include_series=true`` to also
    return the cumulative-P&L series for the chart panel."""
    w = _normalize_window(window)
    try:
        card = build_detector_scorecard(name, window=w)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if include_series:
        card["pnl_series"] = cumulative_pnl_series(name, window=w)
    return card


# ── P6.3 — suggestion endpoints ─────────────────────────────────────


@router.get("/detector-suggestions")
async def list_suggestions(status: str = Query("pending")
                                    ) -> List[Dict[str, Any]]:
    status = (status or "pending").strip().lower()
    valid = {SUGGESTION_STATUS_PENDING, SUGGESTION_STATUS_ACCEPTED,
              SUGGESTION_STATUS_DISMISSED, "all", "*"}
    if status not in valid:
        raise HTTPException(status_code=400,
                                 detail=f"status must be one of {valid}")
    with session_scope() as s:
        q = select(DetectorSuggestion)
        if status not in {"all", "*"}:
            q = q.where(DetectorSuggestion.status == status)
        q = q.order_by(DetectorSuggestion.created_at.desc())
        rows = s.execute(q).scalars().all()
        return [r.to_dict() for r in rows]


@router.post("/detector-suggestions/{sugg_id}/accept")
async def accept_suggestion(sugg_id: int) -> Dict[str, Any]:
    with session_scope() as s:
        row = s.get(DetectorSuggestion, int(sugg_id))
        if row is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        if row.status != SUGGESTION_STATUS_PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"suggestion already resolved as {row.status}",
            )
        # Find / create the DetectorConfig row + flip enabled.
        cfg = s.execute(
            select(DetectorConfig).where(
                DetectorConfig.name == row.detector_name)
        ).scalar_one_or_none()
        if cfg is None:
            cfg = DetectorConfig(
                name=row.detector_name,
                enabled=True,
                params_json="{}",
                source="builtin",
            )
            s.add(cfg)
            s.flush()
        if row.reason == REASON_LOW_POSTERIOR:
            cfg.enabled = False
        elif row.reason == REASON_RECOVERED_POSTERIOR:
            cfg.enabled = True
        cfg.last_updated_at = datetime.utcnow()
        row.status = SUGGESTION_STATUS_ACCEPTED
        row.resolved_at = datetime.utcnow()
        result = {
            "id": row.id,
            "detector_name": row.detector_name,
            "reason": row.reason,
            "new_enabled": cfg.enabled,
            "status": row.status,
        }
    try:
        clear_detector_config_cache()
    except Exception:
        logger.debug("clear_detector_config_cache failed", exc_info=True)
    return result


# ── MITS Phase 12.J — detector edge endpoints ──────────────────────


def _wilson_ci(wins: int, n: int, z: float = 1.96
                     ) -> Optional[tuple]:
    if n == 0:
        return None
    p_hat = wins / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    margin = (
        z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z * z / (4.0 * n * n)))
    ) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _classify_edge_label(edge_pp: Optional[float]) -> str:
    if edge_pp is None:
        return "no_data"
    if edge_pp > TUNABLES.detector_edge_strong_threshold_pp:
        return "strong"
    if edge_pp > TUNABLES.detector_edge_marginal_threshold_pp:
        return "marginal"
    if edge_pp > TUNABLES.detector_edge_negative_threshold_pp:
        return "noise"
    return "negative"


# Phase 12.2 — dynamic per-direction baseline cache.
_BASELINES_CACHE: Dict[str, Any] = {"ts": 0.0, "value": None}
_BASELINES_TTL_SECONDS = 300  # 5 min
_BASELINES_LOCK = threading.Lock()


def _compute_baselines() -> Dict[str, float]:
    """Return baseline 5d win rate per direction, computed from the
    live corpus.

    Output keys: 'long', 'short', 'neutral', 'null'. Missing
    directions fall back to 0.50.
    """
    out: Dict[str, float] = {}
    try:
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT mo.direction, "
                "AVG(CASE WHEN o.was_winner THEN 1.0 ELSE 0.0 END) AS wr "
                "FROM market_observations mo "
                "JOIN market_outcomes o "
                "  ON mo.id = o.observation_id "
                "WHERE o.horizon = '5d' "
                "GROUP BY mo.direction"
            )).all()
        for r in rows:
            key = (r[0] or "null").lower()
            try:
                out[key] = float(r[1] or 0.0)
            except Exception:
                continue
    except Exception:
        logger.warning("dynamic baseline compute failed; using 0.50 floor",
                       exc_info=True)
    for d in ("long", "short", "neutral", "null"):
        out.setdefault(d, 0.50)
    return out


def get_baselines(force_refresh: bool = False) -> Dict[str, float]:
    """5-minute cached accessor for the per-direction baseline."""
    now = time.time()
    with _BASELINES_LOCK:
        if (not force_refresh
                and _BASELINES_CACHE["value"] is not None
                and (now - float(_BASELINES_CACHE["ts"])) < _BASELINES_TTL_SECONDS):
            return dict(_BASELINES_CACHE["value"])
        bl = _compute_baselines()
        _BASELINES_CACHE["value"] = bl
        _BASELINES_CACHE["ts"] = now
        return dict(bl)


def _baseline_for(direction: Optional[str], baselines: Dict[str, float]) -> float:
    if not direction:
        return baselines.get("null", 0.50)
    d = str(direction).lower()
    if d not in baselines:
        return baselines.get("null", 0.50)
    return baselines[d]


def _pattern_direction_map() -> Dict[str, Optional[str]]:
    """Best-effort static mapping from detector pattern → direction.

    We consult the authoritative `STATIC_DIRECTION` table in
    `backend.bot.detectors.direction` so the edge rollup uses the
    SAME direction the emit-path tags onto observations. Dynamic
    detectors (where direction depends on features) fall back to the
    empirical majority direction from `market_observations` so we
    still pick a sensible baseline rather than defaulting to 'null'.
    """
    try:
        from backend.bot.detectors.direction import STATIC_DIRECTION
    except Exception:
        STATIC_DIRECTION = {}
    out: Dict[str, Optional[str]] = dict(STATIC_DIRECTION)
    # Empirical fallback for dynamic detectors — pick majority direction.
    try:
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT pattern, direction, COUNT(*) n "
                "FROM market_observations "
                "WHERE direction IS NOT NULL "
                "GROUP BY pattern, direction "
                "ORDER BY pattern, n DESC"
            )).all()
        empirical: Dict[str, str] = {}
        for pat, d, _n in rows:
            if pat not in empirical:
                empirical[pat] = (d or "").lower() or None
        for pat, d in empirical.items():
            out.setdefault(pat, d)
    except Exception:
        logger.debug("pattern-direction empirical fallback failed",
                     exc_info=True)
    return out


def _build_edge_rows() -> List[Dict[str, Any]]:
    """Walk every detector in the registry and compute its 5d win
    rate, sample size, Wilson CI, and edge vs ITS DIRECTION'S
    baseline (Phase 12.2 fix — was a single global baseline).
    """
    baselines = get_baselines()
    direction_map = _pattern_direction_map()
    out: List[Dict[str, Any]] = []
    # Pull configs (enabled flag).
    enabled_map: Dict[str, bool] = {}
    with session_scope() as s:
        for cfg in s.execute(select(DetectorConfig)).scalars().all():
            enabled_map[cfg.name] = bool(cfg.enabled)
        # Per-detector aggregate query.
        # Phase 12.2 — wrap was_winner in CASE WHEN so SQLite returns
        # a plain integer sum. SQLAlchemy's Boolean column coercion
        # was clamping SUM() back to True/False for rows where every
        # outcome was a win (giving us total_wins=1 per detector and
        # the -50pp family ghost edges).
        rows = s.execute(text(
            "SELECT mo.pattern, COUNT(o.id), "
            "SUM(CASE WHEN o.was_winner THEN 1 ELSE 0 END) "
            "FROM market_observations mo "
            "JOIN market_outcomes o "
            "  ON mo.id = o.observation_id "
            "WHERE o.horizon = '5d' "
            "GROUP BY mo.pattern"
        )).all()
    counts: Dict[str, tuple] = {}
    for pattern, n_outcomes, wins in rows:
        try:
            n_int = int(n_outcomes or 0)
            wins_int = int(wins or 0)
        except Exception:
            n_int = 0
            wins_int = 0
        counts[pattern] = (n_int, wins_int)
    for name, det in DETECTOR_REGISTRY.items():
        n_outcomes, wins = counts.get(name, (0, 0))
        wr = (wins / n_outcomes) if n_outcomes > 0 else None
        direction = direction_map.get(name)
        baseline_for_det = _baseline_for(direction, baselines)
        edge_pp = (
            (wr - baseline_for_det) * 100.0 if wr is not None else None
        )
        ci = _wilson_ci(wins, n_outcomes)
        out.append({
            "name": name,
            "family": getattr(det, "family", "uncategorized"),
            "description": getattr(det, "description", "") or "",
            "enabled": enabled_map.get(name, True),
            "sample_size": n_outcomes,
            "wins": wins,
            "win_rate_5d": round(wr, 4) if wr is not None else None,
            "direction": direction,
            "baseline_5d": round(baseline_for_det, 4),
            "edge_pp_vs_baseline": (round(edge_pp, 2)
                                            if edge_pp is not None else None),
            "ci_lower": round(ci[0], 4) if ci is not None else None,
            "ci_upper": round(ci[1], 4) if ci is not None else None,
            "label": _classify_edge_label(edge_pp),
        })
    return out


@router.get("/detectors/edge")
async def detectors_edge(family: Optional[str] = Query(None),
                                       label: Optional[str] = Query(None),
                                       min_n: int = Query(0)
                                       ) -> Dict[str, Any]:
    """Per-detector 5d edge vs ITS direction's baseline. Optional
    filters by family + label + minimum sample size. Sorted descending
    by edge.
    """
    rows = _build_edge_rows()
    if family:
        rows = [r for r in rows if r["family"] == family]
    if label:
        rows = [r for r in rows if r["label"] == label]
    if min_n > 0:
        rows = [r for r in rows if (r["sample_size"] or 0) >= min_n]
    rows.sort(key=lambda r: -(r["edge_pp_vs_baseline"]
                                       if r["edge_pp_vs_baseline"] is not None
                                       else -999))
    baselines = get_baselines()
    return {
        "baselines_5d": baselines,
        "thresholds": {
            "strong_pp": TUNABLES.detector_edge_strong_threshold_pp,
            "marginal_pp": TUNABLES.detector_edge_marginal_threshold_pp,
            "negative_pp": TUNABLES.detector_edge_negative_threshold_pp,
        },
        "count": len(rows),
        "detectors": rows,
    }


@router.get("/detectors/edge/families")
async def detectors_edge_families() -> Dict[str, Any]:
    """Family rollup of the edge endpoint.

    Phase 12.2 — instead of comparing a family-aggregate WR to a
    single static baseline (which produced the -68pp artifacts when
    short-heavy families were compared to a 68.9% long baseline),
    we now report:

      * detector_count
      * total_n (sum of all per-detector samples)
      * weighted_edge_pp: sample-size weighted average of EACH
        detector's edge vs ITS direction's baseline.

    This is the institutionally-correct rollup because each detector
    is scored against the right reference and the family number is
    just an N-weighted aggregate of those (already correct) edges.
    """
    rows = _build_edge_rows()
    by_family: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        fam = r["family"]
        entry = by_family.setdefault(fam, {
            "family": fam, "detector_count": 0,
            "total_n": 0, "total_wins": 0,
            "_weighted_edge_num": 0.0,
            "_weighted_edge_den": 0,
        })
        entry["detector_count"] += 1
        n = int(r["sample_size"] or 0)
        entry["total_n"] += n
        entry["total_wins"] += int(r["wins"] or 0)
        edge = r.get("edge_pp_vs_baseline")
        if edge is not None and n > 0:
            entry["_weighted_edge_num"] += float(edge) * n
            entry["_weighted_edge_den"] += n
    summary: List[Dict[str, Any]] = []
    for fam, entry in by_family.items():
        n = entry["total_n"]
        wr = (entry["total_wins"] / n) if n > 0 else None
        wedge = (
            entry["_weighted_edge_num"] / entry["_weighted_edge_den"]
            if entry["_weighted_edge_den"] > 0 else None
        )
        # Strip the private accumulators.
        out_entry = {k: v for k, v in entry.items() if not k.startswith("_")}
        out_entry.update({
            "win_rate_5d": round(wr, 4) if wr is not None else None,
            "weighted_edge_pp": (round(wedge, 2)
                                  if wedge is not None else None),
            # Legacy alias for any UI that reads the old key.
            "edge_pp_vs_baseline": (round(wedge, 2)
                                      if wedge is not None else None),
            "label": _classify_edge_label(wedge),
        })
        summary.append(out_entry)
    summary.sort(key=lambda r: -(r["weighted_edge_pp"]
                                              if r["weighted_edge_pp"] is not None
                                              else -999))
    return {
        "baselines_5d": get_baselines(),
        "count": len(summary),
        "families": summary,
    }


@router.post("/detector-suggestions/{sugg_id}/dismiss")
async def dismiss_suggestion(sugg_id: int) -> Dict[str, Any]:
    with session_scope() as s:
        row = s.get(DetectorSuggestion, int(sugg_id))
        if row is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        if row.status != SUGGESTION_STATUS_PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"suggestion already resolved as {row.status}",
            )
        row.status = SUGGESTION_STATUS_DISMISSED
        row.resolved_at = datetime.utcnow()
        return {
            "id": row.id,
            "detector_name": row.detector_name,
            "reason": row.reason,
            "status": row.status,
        }
