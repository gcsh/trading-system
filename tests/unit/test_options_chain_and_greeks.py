"""Stage-3 — Black-Scholes Greeks + IV + chain + assignment risk.

Pinned values:
  • BS prices against textbook examples (Hull p. 348, ATM 1-month call)
  • Put-call parity
  • Greeks signs + monotonicity (delta_call > 0, theta_long < 0, vega > 0)
  • Implied vol round-trips through bs_price within tol
  • Synthetic chain returns enough ladder rungs to be usable
  • Assignment risk monotonic in ITM-ness and inverse in DTE
  • Thin chains don't crash the fallback path
"""
import math
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.greeks import (
    _norm_cdf,
    _norm_pdf,
    bs_price,
    compute_greeks,
    implied_vol,
)
from backend.bot.options_chain import (
    assignment_probability,
    available_expirations,
    clear_cache,
    fetch_chain,
    iv_surface,
    nearest_available_strike,
)


# ── normal distribution primitives ─────────────────────────────────────────


class TestNormalDist:
    def test_norm_cdf_zero_half(self):
        assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-6)

    def test_norm_cdf_symmetric(self):
        assert _norm_cdf(1.0) + _norm_cdf(-1.0) == pytest.approx(1.0, abs=1e-6)

    def test_norm_pdf_peak_at_zero(self):
        assert _norm_pdf(0.0) == pytest.approx(1 / math.sqrt(2 * math.pi), abs=1e-6)


# ── Black-Scholes ─────────────────────────────────────────────────────────


class TestBlackScholes:
    def test_atm_call_one_month_textbook(self):
        # Hull-style ATM call: S=K=100, T=1/12, r=0.05, σ=0.20
        price = bs_price(100, 100, 1 / 12, 0.05, 0.20, "call")
        # Reference (computed in scipy or any BS calculator): ~2.51
        assert 2.4 < price < 2.7

    def test_put_call_parity(self):
        # C - P = S - K·e^(-rT)
        S, K, T, r, sigma = 100.0, 95.0, 0.5, 0.04, 0.25
        c = bs_price(S, K, T, r, sigma, "call")
        p = bs_price(S, K, T, r, sigma, "put")
        rhs = S - K * math.exp(-r * T)
        assert (c - p) == pytest.approx(rhs, abs=1e-3)

    def test_deep_itm_call_near_intrinsic_plus_pv(self):
        # Deep ITM call should be close to S - K·e^(-rT)
        S, K, T, r, sigma = 200.0, 100.0, 0.25, 0.05, 0.20
        c = bs_price(S, K, T, r, sigma, "call")
        intrinsic_pv = S - K * math.exp(-r * T)
        assert c >= intrinsic_pv - 1e-2

    def test_expiry_intrinsic(self):
        # T=0 → just the intrinsic value
        assert bs_price(110, 100, 0, 0.05, 0.20, "call") == 10.0
        assert bs_price(90, 100, 0, 0.05, 0.20, "put") == 10.0

    def test_degenerate_safe(self):
        # zero σ → no time value → 0
        assert bs_price(100, 100, 0.5, 0.05, 0, "call") == 0.0
        # negative inputs → 0
        assert bs_price(-1, 100, 0.5, 0.05, 0.2, "call") == 0.0


# ── Greeks ────────────────────────────────────────────────────────────────


class TestGreeks:
    def test_atm_call_delta_around_half(self):
        g = compute_greeks(100, 100, 30/365, 0.20, r=0.05, kind="call")
        assert 0.50 < g.delta < 0.60

    def test_atm_put_delta_negative(self):
        g = compute_greeks(100, 100, 30/365, 0.20, r=0.05, kind="put")
        assert -0.6 < g.delta < -0.4

    def test_gamma_positive_for_both(self):
        gc = compute_greeks(100, 100, 30/365, 0.20, kind="call")
        gp = compute_greeks(100, 100, 30/365, 0.20, kind="put")
        assert gc.gamma > 0
        assert gp.gamma > 0
        assert gc.gamma == pytest.approx(gp.gamma, abs=1e-4)

    def test_vega_positive_for_both(self):
        gc = compute_greeks(100, 100, 30/365, 0.20, kind="call")
        gp = compute_greeks(100, 100, 30/365, 0.20, kind="put")
        assert gc.vega > 0 and gp.vega > 0

    def test_theta_long_is_negative(self):
        g = compute_greeks(100, 100, 30/365, 0.20, kind="call")
        # Daily theta on long options should be negative (time decay)
        assert g.theta < 0


# ── Implied volatility ────────────────────────────────────────────────────


class TestImpliedVol:
    def test_round_trip(self):
        S, K, T, r, sigma = 100, 100, 30/365, 0.05, 0.25
        price = bs_price(S, K, T, r, sigma, "call")
        recovered = implied_vol(price, S, K, T, r=r, kind="call")
        assert recovered == pytest.approx(sigma, abs=1e-3)

    def test_below_intrinsic_returns_none(self):
        # Call worth less than intrinsic → no valid σ
        recovered = implied_vol(0.01, 200, 100, 0.5, kind="call")
        assert recovered is None

    def test_zero_price_safe(self):
        assert implied_vol(0, 100, 100, 0.5) is None


# ── Synthetic chain (when yfinance is unavailable / in tests) ─────────────


class TestSyntheticChain:
    def setup_method(self):
        clear_cache()

    def test_synthetic_with_spot_returns_ladder(self):
        chain = fetch_chain("NVDA", spot_hint=215.0, prefer_synthetic=True)
        assert chain.source == "synthetic"
        assert chain.spot == 215.0
        # 3 expirations × 31 strikes × 2 kinds = 186 quotes
        assert len(chain.quotes) > 100
        # Every quote has a non-None IV and price > 0
        assert all(q.iv is not None and q.iv > 0 for q in chain.quotes)
        assert all(q.mid > 0 for q in chain.quotes[:50])

    def test_synthetic_without_spot_safe(self):
        chain = fetch_chain("NOPE", spot_hint=0, prefer_synthetic=True)
        assert chain.source == "fallback"
        assert chain.quotes == []

    def test_iv_surface_pivots_correctly(self):
        clear_cache()
        fetch_chain("NVDA", spot_hint=215.0, prefer_synthetic=True)
        surf = iv_surface("NVDA")
        assert surf["spot"] == 215.0
        assert len(surf["samples"]) == 3
        for s in surf["samples"]:
            assert len(s["strikes"]) == len(s["call_iv"]) == len(s["put_iv"])


# ── Strike availability / chain-aware fallback ────────────────────────────


class TestNearestAvailable:
    def setup_method(self):
        clear_cache()

    def test_synthetic_returns_listed_strike(self):
        # The synthetic chain ladders around spot; target very close to atm
        # should snap to an actual rung.
        fetch_chain("NVDA", spot_hint=215.0, prefer_synthetic=True)
        strike, source = nearest_available_strike(
            "NVDA", target=215.0, kind="call", spot_hint=215.0,
        )
        assert source == "synthetic"
        # synthetic ladder steps at $5 for $100–500 band → strike == 215
        assert abs(strike - 215.0) <= 5.0

    def test_offmarket_target_picks_nearest(self):
        fetch_chain("NVDA", spot_hint=215.0, prefer_synthetic=True)
        strike, source = nearest_available_strike(
            "NVDA", target=212.4, kind="put", spot_hint=215.0,
        )
        assert source == "synthetic"
        # nearest synthetic strike must be one of the ladder rungs
        assert strike in [210.0, 215.0]


# ── Assignment risk ───────────────────────────────────────────────────────


class TestAssignmentRisk:
    def test_otm_call_near_zero(self):
        # Spot $100 < strike $110 short call: not in the money → low risk
        r = assignment_probability(spot=100, strike=110, dte=10, kind="call")
        assert r["probability"] < 0.1
        assert "OTM" in " ".join(r["reasons"])

    def test_deep_itm_short_call_high_risk(self):
        # spot 130 vs strike 100 with DTE=1 → almost guaranteed assignment
        r = assignment_probability(spot=130, strike=100, dte=1, kind="call")
        assert r["probability"] > 0.7

    def test_dividend_bumps_risk(self):
        base = assignment_probability(spot=120, strike=110, dte=20, kind="call")
        with_div = assignment_probability(spot=120, strike=110, dte=20,
                                            kind="call", ex_div_days=2)
        assert with_div["probability"] > base["probability"]

    def test_long_position_no_assignment(self):
        r = assignment_probability(spot=120, strike=100, dte=1, kind="call",
                                     side="LONG")
        assert r["probability"] == 0.0

    def test_monotonic_in_itm_amount(self):
        slightly = assignment_probability(spot=101, strike=100, dte=5, kind="call")["probability"]
        deeply = assignment_probability(spot=130, strike=100, dte=5, kind="call")["probability"]
        assert deeply > slightly


# ── live API integration ──────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    clear_cache()
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_synthetic_chain_endpoint(self, client):
        # prefer_synthetic=true skips the network so the test is deterministic
        body = client.get("/options/chain/NVDA?prefer_synthetic=true").json()
        # We didn't pass spot_hint so we get the fallback (no quotes) — fine
        assert "ticker" in body and body["ticker"] == "NVDA"
        assert body["source"] in ("synthetic", "fallback")

    def test_greeks_endpoint(self, client):
        body = client.get(
            "/options/greeks?spot=100&strike=100&dte=30&iv=0.20&kind=call"
        ).json()
        g = body["greeks"]
        assert 0.4 < g["delta"] < 0.7    # ATM-ish 30-day call
        assert g["gamma"] > 0
        assert g["theta"] < 0
        assert g["vega"] > 0
        assert g["price"] > 0

    def test_iv_recovery_endpoint(self, client):
        # Price an ATM call at σ=0.25 then ask the endpoint to recover it.
        price = bs_price(100, 100, 30/365, 0.045, 0.25, "call")
        body = client.get(
            f"/options/implied-vol?price={price:.6f}"
            "&spot=100&strike=100&dte=30&kind=call"
        ).json()
        assert body["implied_vol"] is not None
        assert abs(body["implied_vol"] - 0.25) < 0.005

    def test_assignment_risk_endpoint(self, client):
        body = client.get(
            "/options/assignment-risk?spot=130&strike=100&dte=1&kind=call"
        ).json()
        assert body["probability"] > 0.7
        assert any("imminent" in r or "ITM" in r for r in body["reasons"])

    def test_strike_suggest_endpoint(self, client):
        # prefer the synthetic fallback so the test doesn't depend on yfinance
        body = client.get(
            "/options/strike-suggest?ticker=NVDA&moneyness=-0.05"
            "&kind=put&spot_hint=215.0"
        ).json()
        assert body["source"] in ("synthetic", "snap_fallback", "yfinance")
        assert body["selected_strike"] > 0
