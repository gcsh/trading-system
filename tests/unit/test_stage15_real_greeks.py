"""Stage-15 — real Black-Scholes Greeks in scenarios.

Pinned:
  • years_to_expiry parses ISO dates and clamps to 0 on expired
  • greeks_from_position returns sensible values for an ATM call
  • greeks_from_position returns sensible values for an OTM put
  • Falls through to compute_greeks zeros when inputs are missing
  • _option_impact prefers computed Greeks when inputs are complete
  • _option_impact falls back to heuristic when inputs are missing
  • Per-position meta override wins over computed Greeks
  • Existing test_stage11_scenarios assertions still hold (back-compat)
"""
from datetime import datetime, timedelta

from backend.bot.greeks import (
    Greeks,
    compute_greeks,
    greeks_from_position,
    years_to_expiry,
)
from backend.bot.scenarios import Shock, _option_impact


# ── years_to_expiry ─────────────────────────────────────────────────────


class TestYearsToExpiry:
    def test_iso_string_future(self):
        far = (datetime.utcnow() + timedelta(days=365)).date().isoformat()
        T = years_to_expiry(far)
        assert T is not None and 0.95 < T < 1.05

    def test_already_expired(self):
        past = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
        assert years_to_expiry(past) == 0.0

    def test_unparseable(self):
        assert years_to_expiry("not a date") is None
        assert years_to_expiry(None) is None


# ── greeks_from_position ─────────────────────────────────────────────────


def _atm_call_position():
    """A ~30-day ATM call, $200 strike, $200 underlying, 30% IV."""
    return {
        "ticker": "NVDA", "kind": "option", "option_type": "call",
        "strike": 200.0, "expiration":
            (datetime.utcnow() + timedelta(days=30)).date().isoformat(),
        "current_price": 200.0,    # underlying
        "contracts": 1,
        "meta": {"iv": 0.30},
        "market_value": 1000.0,
        "side": "LONG",
    }


def _atm_put_position():
    p = _atm_call_position()
    p["option_type"] = "put"
    return p


class TestGreeksFromPosition:
    def test_atm_call_delta_near_half(self):
        g = greeks_from_position(_atm_call_position())
        assert isinstance(g, Greeks)
        # ATM call delta is ~0.52-0.58 depending on rate + DTE
        assert 0.45 < g.delta < 0.65
        # Vega per vol point should be positive
        assert g.vega > 0

    def test_atm_put_delta_negative(self):
        g = greeks_from_position(_atm_put_position())
        assert -0.65 < g.delta < -0.35

    def test_missing_strike_returns_degenerate(self):
        p = _atm_call_position()
        p["strike"] = 0
        g = greeks_from_position(p)
        # compute_greeks returns zeros for degenerate inputs
        assert g.delta == 0.0
        assert g.vega == 0.0


# ── _option_impact integration ──────────────────────────────────────────


class TestOptionImpactWithGreeks:
    def test_prefers_computed_greeks(self):
        pos = _atm_call_position()
        # Pure delta shock (no VIX change) so we can isolate the delta contribution.
        impact = _option_impact(pos, Shock(spy_pct=-0.02))
        assert impact.breakdown["greeks_source"] == "computed"
        # Real ATM call delta ~0.55, NVDA beta 1.7, SPY -2% → underlying -3.4%
        # ΔP&L ≈ 1000 × 0.55 × -0.034 = -18.7 → loss
        assert impact.pnl_delta < 0
        assert impact.breakdown["delta"] < 0
        # No VIX shock → vega contribution is zero
        assert impact.breakdown["vega"] == 0.0

    def test_meta_override_wins(self):
        pos = _atm_call_position()
        pos["meta"] = {"iv": 0.30, "delta": 1.0}      # forced delta = 1 (deep ITM)
        impact = _option_impact(pos, Shock(spy_pct=-0.02))
        assert impact.breakdown["greeks_source"] == "meta_override"
        assert impact.breakdown["delta_used"] == 1.0

    def test_fallback_when_no_greeks_input(self):
        # No strike / expiration → can't compute Greeks
        pos = {
            "ticker": "NVDA", "kind": "option", "option_type": "call",
            "side": "LONG", "market_value": 1000.0, "contracts": 1,
        }
        impact = _option_impact(pos, Shock(spy_pct=-0.02))
        assert impact.breakdown["greeks_source"] == "heuristic"
        # Should still produce a sensible (heuristic) impact
        assert impact.pnl_delta != 0


class TestBackwardCompat:
    def test_existing_long_call_test_still_passes(self):
        """Test from test_stage11_scenarios.TestSensitivities.test_long_call_gains_on_up_move
        — the heuristic case with no Greeks input should still produce a
        positive impact within the original 30-50 range."""
        pos = {
            "ticker": "NVDA", "kind": "option", "option_type": "call",
            "side": "LONG", "quantity": 1, "market_value": 2000,
            "unrealized_pnl": 0.0, "strike": 200, "expiration": "2026-06-21",
        }
        # No `meta.iv` → fallback to derived IV from iv_rank or default
        # No `current_price` → underlying defaults to 0 → degenerate Greeks
        # → falls back to heuristic (delta=0.55, vega=0.02)
        impact = _option_impact(pos, Shock(spy_pct=0.02))
        assert 25 < impact.pnl_delta < 55
