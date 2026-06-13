#!/usr/bin/env python
"""MITS Phase 12.2 — pre/post-pass audit script.

Prints the full detection-layer health snapshot we use to decide
whether the recursive cleanup is done.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _emit(label, payload):
    print(json.dumps({"label": label, "ts": datetime.utcnow().isoformat(),
                       "payload": payload}, default=str))
    sys.stdout.flush()


def main() -> int:
    from sqlalchemy import text
    from backend.db import session_scope

    target_detectors = [
        "wyckoff_spring", "wyckoff_upthrust",
        "insider_cluster", "sector_dispersion",
    ]
    new_detectors = [
        "order_block", "fair_value_gap", "liquidity_sweep_v2",
        "stop_hunt_v2", "premium_discount_zone",
        "market_structure_shift_v2",
        "wyckoff_accumulation_phase", "wyckoff_distribution_phase",
        "wyckoff_spring", "wyckoff_sos", "wyckoff_upthrust",
        "poc_retest", "value_area_rejection", "composite_value_area",
        "pead_drift", "insider_cluster", "smart_money_inflow",
        "earnings_revision_shift",
        "yield_curve_inversion", "credit_spread_widening",
        "dollar_strength_shift", "composite_macro_regime",
        "cross_sectional_momentum", "mean_reversion_z",
        "sector_dispersion",
    ]

    with session_scope() as s:
        # 1. direction breakdown
        rows = s.execute(text(
            "SELECT direction, COUNT(*) FROM market_observations GROUP BY direction"
        )).all()
        _emit("direction_breakdown",
              {(r[0] or "null"): int(r[1]) for r in rows})

        # 2. per-direction 5d baseline
        rows = s.execute(text("""
            SELECT mo.direction, COUNT(*), AVG(CASE WHEN o.was_winner THEN 1.0 ELSE 0.0 END)
            FROM market_observations mo
            JOIN market_outcomes o ON o.observation_id = mo.id
            WHERE o.horizon = '5d'
            GROUP BY mo.direction
        """)).all()
        _emit("baseline_5d_per_direction",
              [{"direction": (r[0] or "null"),
                "n": int(r[1]),
                "wr": round(float(r[2] or 0.0), 4)} for r in rows])

        # 3. knowledge_graph distribution
        r = s.execute(text("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN sample_size >= 30 THEN 1 ELSE 0 END) AS n30,
                   SUM(CASE WHEN sample_size >= 100 THEN 1 ELSE 0 END) AS n100
            FROM knowledge_graph
        """)).first()
        _emit("kg_distribution", {
            "total": int(r[0] or 0),
            "n_ge_30": int(r[1] or 0),
            "n_ge_100": int(r[2] or 0),
            "frac_n_ge_30": (
                float(r[1] or 0) / float(r[0] or 1) if r[0] else 0.0),
        })

        # 4. 4 target detector obs counts
        rows = s.execute(text("""
            SELECT pattern, COUNT(*)
            FROM market_observations
            WHERE pattern IN ('wyckoff_spring','wyckoff_upthrust',
                              'insider_cluster','sector_dispersion')
            GROUP BY pattern
        """)).all()
        existing = {r[0]: int(r[1]) for r in rows}
        result = {p: existing.get(p, 0) for p in target_detectors}
        _emit("target_4_detector_obs", result)

        # 5. all new Phase 12 detector obs counts
        ph = ",".join(f"'{n}'" for n in new_detectors)
        rows = s.execute(text(
            f"SELECT pattern, COUNT(*) FROM market_observations "
            f"WHERE pattern IN ({ph}) GROUP BY pattern"
        )).all()
        existing = {r[0]: int(r[1]) for r in rows}
        _emit("new_phase12_detector_obs",
              {n: existing.get(n, 0) for n in new_detectors})

        # 6. per-detector 5d win-rate (descending edge from per-direction
        # baseline) — only top + bottom 15.
        rows = s.execute(text("""
            SELECT mo.pattern,
                   mo.direction,
                   COUNT(*) AS n,
                   AVG(CASE WHEN o.was_winner THEN 1.0 ELSE 0.0 END) AS wr
            FROM market_observations mo
            JOIN market_outcomes o ON o.observation_id = mo.id
            WHERE o.horizon = '5d'
            GROUP BY mo.pattern, mo.direction
            HAVING n >= 30
            ORDER BY wr DESC
        """)).all()
        # Baselines per direction (from #2 above)
        baselines = {(r[0] or "null"): float(r[2] or 0.0) for r in
                     s.execute(text("""
                        SELECT mo.direction, COUNT(*),
                               AVG(CASE WHEN o.was_winner THEN 1.0 ELSE 0.0 END)
                        FROM market_observations mo
                        JOIN market_outcomes o ON o.observation_id = mo.id
                        WHERE o.horizon = '5d'
                        GROUP BY mo.direction
                    """)).all()}
        for d in ("long", "short", "neutral", "null"):
            baselines.setdefault(d, 0.50)
        per = []
        for r in rows:
            d = r[1] or "null"
            edge = (float(r[3]) - baselines.get(d, 0.50)) * 100.0
            per.append({
                "pattern": r[0], "direction": d,
                "n": int(r[2]), "wr": round(float(r[3]), 4),
                "edge_pp": round(edge, 2),
            })
        per.sort(key=lambda x: x["edge_pp"], reverse=True)
        _emit("detector_edge_top15", per[:15])
        _emit("detector_edge_bottom10", per[-10:])

        # 7. EOD analysis state — today + yesterday rows
        try:
            rows = s.execute(text("""
                SELECT analysis_date, COUNT(*), SUM(CASE WHEN top_pattern IS NOT NULL THEN 1 ELSE 0 END)
                FROM eod_analysis
                GROUP BY analysis_date
                ORDER BY analysis_date DESC
                LIMIT 5
            """)).all()
            _emit("eod_analysis_last5days",
                  [{"date": str(r[0]), "rows": int(r[1]),
                    "with_pattern": int(r[2] or 0)} for r in rows])
        except Exception as e:
            _emit("eod_analysis_error", str(e))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
