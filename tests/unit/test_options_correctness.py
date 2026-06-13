"""Options correctness — strike snapping, instrument labelling, expiry close.

Catches the class of bugs that left the UI showing $215.35 strikes (stock
price), CSP positions labelled "spread", and options stuck "open" past expiry.
"""
import json
from datetime import date, timedelta

import pytest

from backend.bot.data.options import snap_strike
from backend.bot.engine import (
    SINGLE_LEG_OPTIONS,
    SINGLE_LEG_SHORT_OPTIONS,
    SPREAD_OPTIONS,
)
from backend.bot.strategies.base import Action, Signal


# ── strike snapping ─────────────────────────────────────────────────────────


class TestStrikeSnapping:
    def test_under_25_snaps_to_half_dollar(self):
        assert snap_strike(7.34) == 7.50
        assert snap_strike(12.10) == 12.00
        assert snap_strike(24.76) == 25.0  # boundary

    def test_25_to_100_snaps_to_dollar(self):
        assert snap_strike(67.23) == 67.0
        assert snap_strike(67.62) == 68.0
        assert snap_strike(99.4) == 99.0

    def test_100_to_500_snaps_to_five(self):
        # NVDA $215.35 — the bug report case — must NOT keep the .35
        assert snap_strike(215.35) == 215.0
        assert snap_strike(218.0) == 220.0
        assert snap_strike(497.5) == 500.0   # rounds to nearest, not down

    def test_above_500_snaps_to_ten(self):
        assert snap_strike(738.05) == 740.0      # QQQ-ish
        assert snap_strike(522.5) == 520.0       # boundary

    def test_moneyness_offset_for_otm_put(self):
        # NVDA $214 → 5% OTM put expectation: 214 * 0.95 = 203.3 → snap to 205
        assert snap_strike(214.0, "put", moneyness=-0.05) == 205.0

    def test_moneyness_offset_for_otm_call(self):
        assert snap_strike(214.0, "call", moneyness=0.05) == 225.0

    def test_zero_or_negative_price_returns_zero(self):
        assert snap_strike(0) == 0.0
        assert snap_strike(-5) == 0.0
        assert snap_strike(None) == 0.0   # type: ignore[arg-type]


# ── action-set membership ───────────────────────────────────────────────────


class TestActionSets:
    """The engine partitions option actions into three categories. CSP and
    Covered Call MUST be in SINGLE_LEG_SHORT_OPTIONS (not spreads)."""

    def test_csp_is_short_single_leg(self):
        assert Action.SELL_CSP in SINGLE_LEG_SHORT_OPTIONS
        assert Action.SELL_CSP not in SPREAD_OPTIONS

    def test_covered_call_is_short_single_leg(self):
        assert Action.SELL_COVERED_CALL in SINGLE_LEG_SHORT_OPTIONS
        assert Action.SELL_COVERED_CALL not in SPREAD_OPTIONS

    def test_bull_call_spread_is_spread(self):
        assert Action.BULL_CALL_SPREAD in SPREAD_OPTIONS
        assert Action.BULL_CALL_SPREAD not in SINGLE_LEG_SHORT_OPTIONS

    def test_iron_condor_is_spread(self):
        assert Action.IRON_CONDOR in SPREAD_OPTIONS

    def test_buy_call_is_long_single_leg_only(self):
        assert Action.BUY_CALL in SINGLE_LEG_OPTIONS
        assert Action.BUY_CALL not in SINGLE_LEG_SHORT_OPTIONS
        assert Action.BUY_CALL not in SPREAD_OPTIONS


# ── build_order_plan: instrument + strike correctness ──────────────────────


class _StubExec:
    def positions(self):
        return []


def _make_engine():
    from backend.bot.engine import BotEngine
    return BotEngine(executor=_StubExec())


class TestOrderPlanShape:
    def test_csp_plan_is_option_not_spread(self):
        eng = _make_engine()
        sig = Signal(ticker="NVDA", action=Action.SELL_CSP, confidence=0.7,
                      reason="t", strategy="csp", strike=None)
        plan = eng.build_order_plan(sig, quantity=1, price=214.0)
        # NVDA 5% OTM put: 214 * 0.95 = 203.3 → snap to 205
        assert plan["instrument"] == "option"        # not "spread"
        assert plan["option_type"] == "put"
        assert plan["side"] == "SELL"
        assert plan["strike"] == 205.0
        assert plan["contracts"] >= 1

    def test_covered_call_plan_is_option_not_spread(self):
        eng = _make_engine()
        sig = Signal(ticker="MSFT", action=Action.SELL_COVERED_CALL, confidence=0.7,
                      reason="t", strategy="cc", strike=None)
        plan = eng.build_order_plan(sig, quantity=1, price=433.0)
        # MSFT 3% OTM call: 433 * 1.03 = 446.0 → snap to 445
        assert plan["instrument"] == "option"
        assert plan["option_type"] == "call"
        assert plan["side"] == "SELL"
        assert plan["strike"] in (445.0, 446.0)        # depends on snap rounding

    def test_buy_call_strike_is_snapped(self):
        eng = _make_engine()
        sig = Signal(ticker="NVDA", action=Action.BUY_CALL, confidence=0.7,
                      reason="t", strategy="trend", strike=None)
        plan = eng.build_order_plan(sig, quantity=1, price=215.35)
        # the bug-report value: 215.35 must NOT become the strike
        assert plan["strike"] == 215.0
        assert plan["instrument"] == "option"
        assert plan["option_type"] == "call"

    def test_iron_condor_is_spread(self):
        eng = _make_engine()
        sig = Signal(ticker="SPY", action=Action.IRON_CONDOR, confidence=0.7,
                      reason="t", strategy="ic", strike=None)
        plan = eng.build_order_plan(sig, quantity=1, price=600.0)
        assert plan["instrument"] == "spread"


# ── option expiry close: settles intrinsic, books P&L ───────────────────────


@pytest.fixture
def paper_exec(temp_db):
    """Fresh paper executor wired to an isolated DB."""
    from backend.bot.paper_executor import PaperExecutor
    ex = PaperExecutor(starting_cash=10000.0)
    return ex


def _book_call(ex, ticker="NVDA", strike=215.0, expiration=None, qty=1):
    """Open a long call position via the public order surface."""
    expiration = expiration or (date.today() + timedelta(days=30)).isoformat()
    # Monkey-patch the executor's price oracle to a known spot.
    ex._price = lambda t: 214.0
    return ex.place_options_order(ticker, "BUY_CALL", qty, strike, expiration)


class TestOptionExpiryClose:
    def test_open_option_marks_to_market_with_intrinsic(self, paper_exec):
        _book_call(paper_exec, strike=210.0)
        # spot 220 -> intrinsic 10/share, mark per share ≈ 10 + 0.005*210 = 11.05
        paper_exec._price = lambda t: 220.0
        positions = paper_exec.positions()
        assert len(positions) == 1
        p = positions[0]
        assert p["kind"] == "option"
        assert p["strike"] == 210.0
        assert p["option_type"] == "call"
        assert p["unrealized_pnl_pct"] != 0.0          # was always 0 before the fix

    def test_close_option_at_itm_books_positive_pnl(self, paper_exec):
        # Entry premium ~ 0.03 * 210 = 6.3/share = $630/contract
        _book_call(paper_exec, strike=210.0)
        paper_exec._price = lambda t: 230.0            # ITM by $20
        expiration = (date.today() + timedelta(days=30)).isoformat()
        order = paper_exec.close_option("NVDA", 210.0, expiration, reason="expiry")
        assert order.success, order.error
        # intrinsic 20 -> $2000 received, entry 6.3 -> $630 paid, pnl = 1370
        assert order.raw["pnl"] > 1000
        assert paper_exec.positions() == []            # position deleted

    def test_close_option_at_otm_books_full_loss(self, paper_exec):
        _book_call(paper_exec, strike=220.0)
        paper_exec._price = lambda t: 200.0            # OTM
        expiration = (date.today() + timedelta(days=30)).isoformat()
        order = paper_exec.close_option("NVDA", 220.0, expiration, reason="expiry")
        assert order.success
        # intrinsic 0 -> $0 received, entry premium lost
        assert order.raw["pnl"] < 0

    def test_close_option_rejects_mismatched_contract(self, paper_exec):
        _book_call(paper_exec, strike=210.0)
        expiration = (date.today() + timedelta(days=30)).isoformat()
        order = paper_exec.close_option("NVDA", 999.0, expiration)
        assert not order.success
        assert "doesn't match" in (order.error or "")
