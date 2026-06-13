"""Black-Scholes reference-table tests — P2.1.

The values below come from a standard BS calculator (e.g. CBOE's online
calculator with r=5%, no dividend). If our implementation drifts from
these reference values, every option mark in paper accounting becomes
suspect.
"""
from __future__ import annotations

import math

import pytest

from backend.bot.options.blackscholes import (
    price, delta, gamma, theta, vega, implied_iv, snapshot,
)


pytestmark = [pytest.mark.unit, pytest.mark.data_integrity]


class TestPriceReferenceValues:
    """Values cross-checked against a CBOE-style calculator (r=5%, q=0)."""

    def test_atm_call_30dte_iv_20(self):
        # S=100, K=100, T=30/365, IV=0.20, r=0.05 → ~$2.51
        p = price(100, 100, 30, 0.20, 0.05, "call")
        assert p == pytest.approx(2.51, abs=0.05)

    def test_atm_put_30dte_iv_20(self):
        # Put-call parity: P = C - S + K*e^(-rT)
        p = price(100, 100, 30, 0.20, 0.05, "put")
        assert p == pytest.approx(2.10, abs=0.05)

    def test_otm_call_5pct_30dte_iv_20(self):
        # S=100, K=105, r=0.05, IV=0.20, T=30/365 → ≈ $0.73
        # (CBOE calculator confirms — earlier ref of $0.49 was r=0.)
        p = price(100, 105, 30, 0.20, 0.05, "call")
        assert p == pytest.approx(0.73, abs=0.10)

    def test_itm_call_5pct_30dte_iv_20(self):
        # S=100, K=95 → ~$5.85 (intrinsic 5 + time value)
        p = price(100, 95, 30, 0.20, 0.05, "call")
        assert p == pytest.approx(5.85, abs=0.10)

    def test_higher_iv_increases_premium(self):
        low = price(100, 100, 30, 0.15, 0.05, "call")
        high = price(100, 100, 30, 0.40, 0.05, "call")
        assert high > low


class TestPutCallParity:
    """C - P = S - K * e^(-rT). If parity holds, the implementation is
    internally consistent — fundamental BS invariant."""

    def test_parity_atm_30dte(self):
        S, K, T, sigma, r = 100, 100, 30, 0.20, 0.05
        c = price(S, K, T, sigma, r, "call")
        p = price(S, K, T, sigma, r, "put")
        forward = S - K * math.exp(-r * T / 365.0)
        assert c - p == pytest.approx(forward, abs=1e-3)

    def test_parity_otm_60dte(self):
        S, K, T, sigma, r = 100, 110, 60, 0.30, 0.05
        c = price(S, K, T, sigma, r, "call")
        p = price(S, K, T, sigma, r, "put")
        forward = S - K * math.exp(-r * T / 365.0)
        assert c - p == pytest.approx(forward, abs=1e-3)


class TestDelta:
    def test_atm_call_delta_near_half(self):
        d = delta(100, 100, 30, 0.20, 0.05, "call")
        assert 0.45 <= d <= 0.60

    def test_deep_itm_call_delta_near_one(self):
        d = delta(150, 100, 30, 0.20, 0.05, "call")
        assert d > 0.95

    def test_deep_otm_call_delta_near_zero(self):
        d = delta(50, 100, 30, 0.20, 0.05, "call")
        assert d < 0.05

    def test_put_delta_negative(self):
        d = delta(100, 100, 30, 0.20, 0.05, "put")
        assert -0.55 <= d <= -0.40


class TestGammaThetaVega:
    def test_gamma_positive(self):
        g = gamma(100, 100, 30, 0.20, 0.05)
        assert g > 0

    def test_theta_negative_for_long(self):
        # Long options decay over time → theta < 0.
        t = theta(100, 100, 30, 0.20, 0.05, "call")
        assert t < 0

    def test_vega_positive(self):
        v = vega(100, 100, 30, 0.20, 0.05)
        assert v > 0


class TestImpliedIV:
    def test_roundtrip_implied_iv_matches_input(self):
        true_iv = 0.27
        p = price(100, 100, 30, true_iv, 0.05, "call")
        recovered = implied_iv(100, 100, 30, p, 0.05, "call")
        assert recovered is not None
        assert recovered == pytest.approx(true_iv, abs=0.005)


class TestSnapshot:
    def test_returns_complete_dict(self):
        snap = snapshot(100, 100, 30, 0.20, 0.05, "call")
        for k in ("price", "delta", "gamma", "theta", "vega", "iv", "rate"):
            assert k in snap

    def test_snapshot_values_consistent_with_individual_calls(self):
        snap = snapshot(100, 100, 30, 0.20, 0.05, "call")
        assert snap["price"] == pytest.approx(
            price(100, 100, 30, 0.20, 0.05, "call"), abs=1e-9,
        )


class TestEdgeCases:
    def test_zero_dte_does_not_crash(self):
        # At expiry, price collapses to intrinsic. Our T_MIN = 1/365
        # ensures no division by zero.
        p = price(100, 95, 0, 0.20, 0.05, "call")
        assert p >= 5.0   # ≈ intrinsic

    def test_invalid_spot_raises(self):
        with pytest.raises(ValueError):
            price(-1, 100, 30, 0.20)

    def test_invalid_iv_raises(self):
        with pytest.raises(ValueError):
            price(100, 100, 30, 0.0)
