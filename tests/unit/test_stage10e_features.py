"""Stage-10 items 17 / 18 / 19 / 20 — new features that help the model learn.

Pinned behavior:
  • dGEX/dPrice: returns linear slope; None below min_obs; correct sign
  • vol-of-vol: stddev of stddev; None below window; positive on noisy series
  • Strike quality: penalizes low OI, low volume, discontinuous IV; clean
    strike scores high
  • Sweep/absorb momentum: trend_buy when slope ≥ +0.05; trend_sell ≤ -0.05;
    neutral in between; None below min_obs
"""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.features.regime_extra import (
    gex_dprice_slope,
    intraday_vol_of_vol,
)
from backend.bot.microstructure.momentum import (
    SweepAbsorbMomentum,
    sweep_absorb_momentum,
)
from backend.bot.options_chain.strike_quality import (
    StrikeQuality,
    score_strike,
)


# ── dGEX / dPrice ─────────────────────────────────────────────────────────


class TestGexDPriceSlope:
    def test_below_min_obs_returns_none(self):
        snaps = [{"price": 100, "gex_total": 1e9}]
        assert gex_dprice_slope(snaps, min_obs=3) is None

    def test_perfect_positive_slope(self):
        # GEX rises $1 per $1 of spot
        snaps = [{"price": 100 + i, "gex_total": 1e9 + i * 1e6}
                  for i in range(5)]
        slope = gex_dprice_slope(snaps)
        # Δgex / Δprice = 1e6 per $1
        assert slope == pytest.approx(1e6, rel=1e-3)

    def test_negative_slope(self):
        # GEX collapses as spot rises
        snaps = [{"price": 100 + i, "gex_total": 1e9 - i * 5e6}
                  for i in range(5)]
        slope = gex_dprice_slope(snaps)
        assert slope < 0
        assert slope == pytest.approx(-5e6, rel=1e-3)

    def test_missing_fields_skipped(self):
        snaps = [{"price": 100, "gex_total": 1e9},
                  {"price": None, "gex_total": 2e9},
                  {"price": 101, "gex_total": 1.5e9},
                  {"price": 102, "gex_total": 2e9}]
        slope = gex_dprice_slope(snaps, min_obs=3)
        assert slope is not None


# ── vol-of-vol ────────────────────────────────────────────────────────────


class TestVolOfVol:
    def test_too_few_returns_none(self):
        assert intraday_vol_of_vol([0.001] * 10, inner_window=12,
                                       outer_window=24) is None

    def test_steady_vol_low_vol_of_vol(self):
        # Constant amplitude oscillation → inner stddev nearly constant
        # → vol-of-vol near zero
        import math
        rets = [0.005 * math.sin(i * 0.5) for i in range(60)]
        v = intraday_vol_of_vol(rets, inner_window=5, outer_window=10)
        assert v is not None
        assert v < 0.005       # very small

    def test_changing_vol_higher_vol_of_vol(self):
        # First half: quiet random; second half: loud random.
        # vol-of-vol picks up the regime change in inner-stddev levels.
        import random
        rng = random.Random(7)
        quiet = [rng.gauss(0, 0.001) for _ in range(30)]
        loud = [rng.gauss(0, 0.01) for _ in range(30)]
        v = intraday_vol_of_vol(quiet + loud, inner_window=5,
                                   outer_window=10)
        assert v is not None
        assert v > 0          # detects the quiet → loud transition


# ── strike quality ───────────────────────────────────────────────────────


def _chain_with_strike(strike, *, oi=500, volume=200, iv=0.30,
                         neighbor_ivs=(0.28, 0.32),
                         kind="call", expiration="2030-01-01"):
    """Build a minimal chain — the target strike plus two neighbors at
    ±5 with the supplied IVs."""
    return [
        {"strike": strike, "kind": kind, "expiration": expiration,
          "open_interest": oi, "volume": volume, "iv": iv},
        {"strike": strike - 5, "kind": kind, "expiration": expiration,
          "open_interest": 1000, "volume": 500, "iv": neighbor_ivs[0]},
        {"strike": strike + 5, "kind": kind, "expiration": expiration,
          "open_interest": 1000, "volume": 500, "iv": neighbor_ivs[1]},
    ]


class TestStrikeQuality:
    def test_clean_strike_scores_high(self):
        chain = _chain_with_strike(100, oi=1000, volume=500, iv=0.30)
        q = score_strike(chain, strike=100, kind="call")
        assert q.score > 0.8
        assert "clean" in q.notes[0]

    def test_low_oi_penalized(self):
        chain = _chain_with_strike(100, oi=5, volume=500, iv=0.30)
        q = score_strike(chain, strike=100, kind="call")
        assert q.score < 0.7
        assert any("low open interest" in n for n in q.notes)

    def test_zero_volume_penalized(self):
        chain = _chain_with_strike(100, oi=1000, volume=0)
        q = score_strike(chain, strike=100, kind="call")
        assert q.score < 0.9
        assert any("low volume" in n for n in q.notes)

    def test_iv_discontinuity_penalized(self):
        # Big gap vs neighbors
        chain = _chain_with_strike(100, iv=0.60, neighbor_ivs=(0.30, 0.32))
        q = score_strike(chain, strike=100, kind="call")
        assert q.factors["iv_smoothness"] < 0.3
        assert any("discontinuous" in n for n in q.notes)

    def test_missing_strike_returns_zero(self):
        chain = _chain_with_strike(100)
        q = score_strike(chain, strike=200, kind="call")
        assert q.score == 0.0
        assert "not in chain" in q.notes[0]


# ── sweep/absorb momentum ────────────────────────────────────────────────


class TestSweepAbsorbMomentum:
    def test_below_min_obs_returns_none(self):
        assert sweep_absorb_momentum([], min_obs=5) is None

    def test_neutral_when_flat(self):
        snaps = [{"sweep_probability": 0.5, "absorption_probability": 0.5}
                  for _ in range(10)]
        result = sweep_absorb_momentum(snaps)
        assert result is not None
        assert result.direction == "neutral"
        assert result.slope == 0.0

    def test_trend_buy_when_sweep_grows(self):
        snaps = [{"sweep_probability": 0.1 + i * 0.05,
                   "absorption_probability": 0.1}
                  for i in range(10)]
        result = sweep_absorb_momentum(snaps)
        assert result is not None
        assert result.direction == "trend_buy"
        assert result.slope > 0

    def test_trend_sell_when_absorb_grows(self):
        snaps = [{"sweep_probability": 0.1,
                   "absorption_probability": 0.1 + i * 0.05}
                  for i in range(10)]
        result = sweep_absorb_momentum(snaps)
        assert result is not None
        assert result.direction == "trend_sell"
        assert result.slope < 0

    def test_missing_fields_skipped(self):
        snaps = ([{"sweep_probability": 0.3, "absorption_probability": 0.1}
                   for _ in range(6)]
                  + [{"sweep_probability": None,
                      "absorption_probability": 0.1}])
        result = sweep_absorb_momentum(snaps)
        assert result is not None
        assert result.n == 6


# ── live API integration ────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_dgex_dprice_endpoint(self, client):
        body = client.post("/features/regime-extra/dgex-dprice", json={
            "snapshots": [{"price": 100 + i, "gex_total": 1e9 - i * 1e6}
                            for i in range(5)],
        }).json()
        assert body["slope"] is not None
        assert body["slope"] < 0

    def test_vol_of_vol_endpoint(self, client):
        body = client.post("/features/regime-extra/vol-of-vol", json={
            "returns": [0.001, -0.001] * 30,
            "inner_window": 5, "outer_window": 10,
        }).json()
        assert "vol_of_vol" in body

    def test_strike_quality_endpoint(self, client):
        chain = _chain_with_strike(100, oi=1000, volume=500, iv=0.30)
        body = client.post("/options/strike-quality", json={
            "quotes": chain, "strike": 100, "kind": "call",
        }).json()
        assert body["score"] > 0.8

    def test_momentum_endpoint(self, client):
        snaps = [{"sweep_probability": 0.1 + i * 0.05,
                   "absorption_probability": 0.1}
                  for i in range(10)]
        body = client.post("/microstructure/momentum", json={
            "snapshots": snaps,
        }).json()
        assert body["direction"] == "trend_buy"

    def test_momentum_endpoint_422_below_min(self, client):
        r = client.post("/microstructure/momentum", json={
            "snapshots": [{"sweep_probability": 0.5,
                            "absorption_probability": 0.5}],
        })
        assert r.status_code == 422
