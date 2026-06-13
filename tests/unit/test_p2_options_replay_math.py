"""P2.1-FU — options-strategy P&L math (pure-function tests)."""
from __future__ import annotations

from backend.bot.backfill.options_history_replay import (
    _bull_call_spread_pnl,
    _cc_pnl,
    _closest,
    _csp_pnl,
    _iron_condor_pnl,
    _intrinsic_value,
)


# ── intrinsic value ─────────────────────────────────────────────────────


def test_intrinsic_call_in_the_money():
    assert _intrinsic_value(kind="call", strike=100.0, spot=110.0) == 10.0


def test_intrinsic_call_out_of_the_money():
    assert _intrinsic_value(kind="call", strike=100.0, spot=95.0) == 0.0


def test_intrinsic_put_in_the_money():
    assert _intrinsic_value(kind="put", strike=100.0, spot=95.0) == 5.0


def test_intrinsic_put_out_of_the_money():
    assert _intrinsic_value(kind="put", strike=100.0, spot=110.0) == 0.0


# ── CSP P&L ─────────────────────────────────────────────────────────────


def test_csp_full_win_when_put_expires_worthless():
    # Entry premium $2, exit premium $0 (or out-of-money) — keep full credit.
    pnl, src = _csp_pnl(entry_premium=2.0, exit_premium=0.0,
                              exit_spot=110, strike=100, contracts=1)
    assert pnl == 200.0
    assert src == "thetadata_eod"


def test_csp_loss_when_put_in_the_money_intrinsic_fallback():
    # No ThetaData → intrinsic of 5 against entry premium of 2 → loss of 3 × 100.
    pnl, src = _csp_pnl(entry_premium=2.0, exit_premium=None,
                              exit_spot=95, strike=100, contracts=1)
    assert pnl == (2.0 - 5.0) * 100
    assert src == "intrinsic_approx"


def test_csp_contracts_scale_linearly():
    pnl_1, _ = _csp_pnl(entry_premium=2.0, exit_premium=0.5,
                                exit_spot=110, strike=100, contracts=1)
    pnl_3, _ = _csp_pnl(entry_premium=2.0, exit_premium=0.5,
                                exit_spot=110, strike=100, contracts=3)
    assert pnl_3 == pnl_1 * 3


# ── CC P&L ──────────────────────────────────────────────────────────────


def test_cc_full_win_when_call_expires_worthless():
    pnl, src = _cc_pnl(entry_premium=1.5, exit_premium=0.0,
                              exit_spot=95, strike=100, contracts=1)
    assert pnl == 150.0
    assert src == "thetadata_eod"


def test_cc_capped_when_underlying_runs_past_strike_intrinsic_fallback():
    # Premium 1.5 received, underlying runs to 110 vs strike 100 → intrinsic 10.
    pnl, src = _cc_pnl(entry_premium=1.5, exit_premium=None,
                              exit_spot=110, strike=100, contracts=1)
    assert pnl == (1.5 - 10.0) * 100
    assert src == "intrinsic_approx"


# ── Bull call spread ────────────────────────────────────────────────────


def test_bull_call_spread_max_profit_at_expiry_intrinsic():
    # Buy 100 call for 3, sell 105 call for 1 → debit 2.
    # At expiry with spot 110: long worth 10, short worth 5 → spread 5.
    # P&L = (5 - 2) * 100 = +300.
    pnl, src = _bull_call_spread_pnl(
        entry_buy=3.0, entry_sell=1.0,
        exit_buy=None, exit_sell=None,
        exit_spot=110, buy_strike=100, sell_strike=105, contracts=1,
    )
    assert pnl == 300.0
    assert src == "intrinsic_approx"


def test_bull_call_spread_max_loss_when_spot_below_long_strike():
    # spot 90 < long strike 100 → both worthless → lose the debit of 2.
    pnl, _ = _bull_call_spread_pnl(
        entry_buy=3.0, entry_sell=1.0,
        exit_buy=None, exit_sell=None,
        exit_spot=90, buy_strike=100, sell_strike=105, contracts=1,
    )
    assert pnl == -200.0


def test_bull_call_spread_uses_thetadata_when_both_legs_priced():
    pnl, src = _bull_call_spread_pnl(
        entry_buy=3.0, entry_sell=1.0,
        exit_buy=4.5, exit_sell=2.0,
        exit_spot=104, buy_strike=100, sell_strike=105, contracts=1,
    )
    # debit = 2, credit_at_exit = 2.5 → +0.5 × 100 = +50
    assert pnl == 50.0
    assert src == "thetadata_eod"


# ── Iron condor ─────────────────────────────────────────────────────────


def test_iron_condor_max_profit_inside_short_strikes_intrinsic():
    # spot finishes between short strikes → all legs worthless → keep credit.
    entries = {
        "call_short": 1.0, "call_long": 0.4,
        "put_short": 0.9, "put_long": 0.3,
    }
    exits = {k: None for k in entries}
    strikes = {
        "call_short": 105, "call_long": 110,
        "put_short": 95, "put_long": 90,
    }
    pnl, src = _iron_condor_pnl(
        entries=entries, exits=exits, strikes=strikes,
        exit_spot=100, contracts=1,
    )
    # credit_in = (1.0 - 0.4) + (0.9 - 0.3) = 1.2 → +120 on 1 contract.
    assert pnl == 120.0
    assert src == "intrinsic_approx"


def test_iron_condor_loses_when_breached():
    # spot blows past long call strike → maximum loss = wing - credit.
    entries = {
        "call_short": 1.0, "call_long": 0.4,
        "put_short": 0.9, "put_long": 0.3,
    }
    exits = {k: None for k in entries}
    strikes = {
        "call_short": 105, "call_long": 110,
        "put_short": 95, "put_long": 90,
    }
    pnl, _ = _iron_condor_pnl(
        entries=entries, exits=exits, strikes=strikes,
        exit_spot=120, contracts=1,
    )
    # cost_call = (15 - 10) = 5; cost_put = 0; total cost_to_close = 5.
    # credit_in 1.2 - 5 = -3.8 → -380.
    assert pnl == -380.0


# ── strike helper ──────────────────────────────────────────────────────


def test_closest_picks_nearest():
    assert _closest([95, 100, 105], 99) == 100
    assert _closest([95, 100, 105], 101) == 100
    assert _closest([95, 100, 105], 107) == 105


def test_closest_handles_empty():
    assert _closest([], 100) is None


def test_closest_handles_invalid_target():
    assert _closest([100, 105], 0) is None
    assert _closest([100, 105], -1) is None
