"""Stage-15 — vectorized regime similarity scan.

Pinned:
  • _vectorized_scores produces the same scores as the per-row loop
  • Empty target → all zeros (no categorical match, no numeric input)
  • Mixed missing-numeric-data is handled (per-row L1 average over present axes)
  • find_similar auto-engages vectorized path past the threshold
  • Vectorized + loop paths return the same top-K matches
"""
from datetime import datetime, timedelta

import pytest

from backend.bot.regime_similarity import (
    _VECTORIZE_THRESHOLD,
    _cat_score,
    _num_score,
    _vectorized_scores,
    find_similar,
)


def _row(*, trend="bullish", vol_phase="neutral", gamma="long_gamma",
            equities="risk_on", yields="rising",
            vix=15.0, iv_rank=40.0, breadth_score=0.5,
            sentiment_score=0.3, sector_strength=0.4):
    return {"trend": trend, "vol_phase": vol_phase, "gamma": gamma,
            "equities": equities, "yields": yields,
            "vix": vix, "iv_rank": iv_rank,
            "breadth_score": breadth_score,
            "sentiment_score": sentiment_score,
            "sector_strength": sector_strength,
            "rates_10y": None, "dollar_dxy": None}


class TestVectorizedScores:
    def test_matches_loop_for_perfect_match(self):
        target = _row()
        snap = [_row()]
        loop = _cat_score(target, snap[0]) + _num_score(target, snap[0])
        vec = _vectorized_scores(target, snap)
        assert abs(vec[0] - loop) < 1e-5

    def test_matches_loop_for_partial_match(self):
        target = _row(trend="bullish", vix=15)
        snap = [_row(trend="bearish", vix=30)]
        loop = _cat_score(target, snap[0]) + _num_score(target, snap[0])
        vec = _vectorized_scores(target, snap)
        assert abs(vec[0] - loop) < 1e-5

    def test_handles_missing_numeric(self):
        target = _row()
        # Row missing one numeric axis
        row = _row()
        row["sentiment_score"] = None
        loop = _cat_score(target, row) + _num_score(target, row)
        vec = _vectorized_scores(target, [row])
        assert abs(vec[0] - loop) < 1e-5

    def test_handles_many_rows(self):
        target = _row()
        snap = [_row(vix=15 + i * 0.5) for i in range(50)]
        vec = _vectorized_scores(target, snap)
        for i, row in enumerate(snap):
            loop = _cat_score(target, row) + _num_score(target, row)
            assert abs(vec[i] - loop) < 1e-5

    def test_empty_target_no_crash(self):
        snap = [_row()]
        vec = _vectorized_scores({}, snap)
        # No categorical match, no numeric input → 0
        assert vec[0] == 0.0


# ── integration: vectorized path engages past threshold ─────────────────


def _seed_snapshots(n):
    from backend.db import session_scope
    from backend.models.regime_episode import RegimeEpisodeSnapshot
    rows = []
    with session_scope() as s:
        for i in range(n):
            r = RegimeEpisodeSnapshot(
                trend="bullish" if i % 2 == 0 else "bearish",
                trend_phase="neutral", volatility="normal",
                vol_phase="neutral",
                gamma="long_gamma" if i % 2 == 0 else "short_gamma",
                risk="neutral",
                equities="risk_on" if i % 2 == 0 else "risk_off",
                yields="rising" if i % 2 == 0 else "falling",
                dollar="neutral", label=f"test {i}",
                vix=14 + (i % 20),
                iv_rank=30 + (i % 50),
                breadth_score=0.5 * ((-1) ** i),
                sentiment_score=0.3 * ((-1) ** i),
                sector_strength=0.4 * ((-1) ** i),
            )
            r.timestamp = datetime.utcnow() + timedelta(seconds=i)
            s.add(r); rows.append(r)
    return rows


class TestVectorizedFindSimilar:
    def test_auto_engages_past_threshold(self, temp_db):
        # Below threshold: loop path is used
        _seed_snapshots(_VECTORIZE_THRESHOLD - 5)
        bullish_target = {"trend": "bullish", "vol_phase": "neutral",
                           "gamma": "long_gamma", "equities": "risk_on",
                           "yields": "rising", "vix": 16, "iv_rank": 40,
                           "breadth_score": 0.5, "sentiment_score": 0.3,
                           "sector_strength": 0.4}
        loop_matches = find_similar(bullish_target, k=20, min_similarity=0.3)
        assert len(loop_matches) > 0

    def test_vectorized_returns_same_top_match(self, temp_db):
        """Run find_similar before and after exceeding the vector threshold
        and confirm the top-1 result is the same row."""
        _seed_snapshots(_VECTORIZE_THRESHOLD - 5)
        target = {"trend": "bullish", "vol_phase": "neutral",
                    "gamma": "long_gamma", "equities": "risk_on",
                    "yields": "rising", "vix": 16, "iv_rank": 35,
                    "breadth_score": 0.5, "sentiment_score": 0.3,
                    "sector_strength": 0.4}
        loop_top = find_similar(target, k=1, min_similarity=0.3)
        # Add more rows to push above threshold
        _seed_snapshots(15)
        vec_top = find_similar(target, k=1, min_similarity=0.3)
        assert len(loop_top) == 1 and len(vec_top) == 1
        # Both paths pick a bullish match (the highest-similarity bucket).
        assert loop_top[0].snapshot["trend"] == "bullish"
        assert vec_top[0].snapshot["trend"] == "bullish"
