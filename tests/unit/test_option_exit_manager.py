"""EXIT.1 — adaptive option exit manager tests.

Covers the contract the manager has with the engine: catastrophe stop,
monitor-mode trailing, DTE adjustments, IV crush, no upper ceiling.

The model: every position has two zones — below +15% gain we only
watch the catastrophe stop; above +15% the trailing logic engages and
the *trailing distance* widens with gain magnitude. There is NO upper
ceiling — a position at +500% can still ride if it hasn't drawn down
past the trailing floor.
"""
from __future__ import annotations

import pytest

from backend.bot.options.exit_manager import decide_exit, compute_dte


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _decide(entry, current, peak=None, dte=21, entry_iv=0.30, current_iv=0.30):
    return decide_exit(
        entry_premium_per_share=entry,
        current_premium_per_share=current,
        peak_premium_per_share=peak,
        dte=dte,
        entry_iv=entry_iv,
        current_iv=current_iv,
    )


class TestNoUpperCeiling:
    """The headline guarantee: winners never get force-closed.
    A position at +200% / +500% / +1000% holds as long as it hasn't
    dropped past the trailing floor."""

    def test_up_200_percent_still_holds_when_at_peak(self):
        # Entry $5, current $15 (+200%), peak == current → at the top
        d = _decide(entry=5.0, current=15.0, peak=15.0, dte=21)
        assert d.should_exit is False
        assert d.monitor_active is True
        assert d.gain_pct == pytest.approx(200.0, abs=0.1)
        assert d.drawdown_from_peak_pct == pytest.approx(0.0, abs=0.1)

    def test_up_500_percent_still_holds_when_at_peak(self):
        d = _decide(entry=5.0, current=30.0, peak=30.0, dte=21)
        assert d.should_exit is False
        assert d.monitor_active is True
        assert d.gain_pct == pytest.approx(500.0, abs=0.1)

    def test_up_1000_percent_still_holds_when_at_peak(self):
        d = _decide(entry=1.0, current=11.0, peak=11.0, dte=30)
        assert d.should_exit is False, "no upper ceiling on winners"
        assert d.gain_pct == pytest.approx(1000.0, abs=0.1)


class TestMonitorFloor:
    """Below the +15% monitor floor we don't apply trailing — only
    catastrophe stop. This prevents whipsaw exits on small noise moves."""

    def test_below_15pct_no_trailing(self):
        # +10% gain → no trailing logic active
        d = _decide(entry=5.0, current=5.5, peak=5.6, dte=21)
        assert d.monitor_active is False
        assert d.trailing_floor_pct is None
        assert d.should_exit is False

    def test_at_exactly_15pct_monitor_activates(self):
        # peak +15% triggers monitor mode
        d = _decide(entry=5.0, current=5.75, peak=5.75, dte=21)
        assert d.monitor_active is True
        assert d.trailing_floor_pct is not None

    def test_just_above_15pct_no_immediate_exit(self):
        # +20% gain, +20% peak → at the top, no exit
        d = _decide(entry=5.0, current=6.0, peak=6.0, dte=21)
        assert d.should_exit is False
        assert d.monitor_active is True


class TestTrailingDistance:
    """Trailing distance widens with gain magnitude — small wins get
    tight trails to lock them in; big wins get wide trails to ride."""

    def test_small_gain_tight_trailing(self):
        # Peak $6 (+20%), tight 10pt trail → floor at $5.40 (+8% gain).
        # Current $5.50 (+10%) stays above the floor → hold.
        d_hold = _decide(entry=5.0, current=5.50, peak=6.0, dte=21)
        assert d_hold.should_exit is False, "+10% above tight floor"
        # Current $5.35 (+7%) — clearly below the +8% floor → exit.
        d_exit = _decide(entry=5.0, current=5.35, peak=6.0, dte=21)
        assert d_exit.should_exit is True, "+7% below tight floor"
        assert "trail hit" in d_exit.reason

    def test_big_gain_wide_trailing(self):
        # Peak +200%, current +180%. A 20-pt drawdown shouldn't exit a
        # runner — the wide trailing band lets it breathe.
        d = _decide(entry=5.0, current=14.0, peak=15.0, dte=21)
        assert d.should_exit is False, "+200% peak with 6% drawdown holds"

    def test_big_gain_only_exits_on_large_drawdown(self):
        # Peak $15 (+200%), wide 35pt trail → floor at $9.75 (+95% gain).
        # A 40% drawdown ($15 → $9.00, +80% gain) crosses the floor.
        d = _decide(entry=5.0, current=9.0, peak=15.0, dte=21)
        assert d.should_exit is True
        assert "trail hit" in d.reason


class TestCatastropheStop:
    """The hard floor for losses — DTE-adjusted because a 50% drawdown
    is recoverable at 30 DTE but not at 3 DTE."""

    def test_minus_50_fires_at_long_dte(self):
        d = _decide(entry=5.0, current=2.5, peak=5.0, dte=21)
        assert d.should_exit is True
        assert "catastrophe" in d.reason
        assert d.hard_stop_pct == pytest.approx(50.0, abs=0.1)

    def test_minus_50_tighter_at_short_dte(self):
        # At 5 DTE, hard stop should be -35% not -50%
        d_hold = _decide(entry=5.0, current=3.5, peak=5.0, dte=5)
        # -30%, hard stop is -35% → still holds
        assert d_hold.should_exit is False
        d_exit = _decide(entry=5.0, current=3.0, peak=5.0, dte=5)
        # -40%, hard stop is -35% → exits
        assert d_exit.should_exit is True
        assert d_exit.hard_stop_pct == pytest.approx(35.0, abs=0.1)

    def test_minus_50_with_1_dte_very_tight(self):
        # At DTE=1, hard stop is -15%
        d = _decide(entry=5.0, current=4.0, peak=5.0, dte=1)
        # -20%, hard stop is -15% → catastrophe fires
        # BUT dte cliff (3) also fires for ANY profit — current is loss
        # so cliff doesn't apply, catastrophe does.
        assert d.should_exit is True
        assert d.hard_stop_pct == pytest.approx(15.0, abs=0.1)


class TestDteCliff:
    """Near expiry, any profit must be banked — theta cliff."""

    def test_dte_cliff_banks_any_profit(self):
        # +5% gain at 2 DTE — below the +15% monitor floor BUT cliff
        # rule says bank any profit when DTE <= 3.
        d = _decide(entry=5.0, current=5.25, peak=5.25, dte=2)
        assert d.should_exit is True
        assert "theta cliff" in d.reason

    def test_dte_cliff_does_not_force_loss(self):
        # At a loss with DTE <= cliff, we don't trigger the cliff rule
        # (that would just lock in a loss); the catastrophe stop or
        # natural expiry handles it.
        d = _decide(entry=5.0, current=4.5, peak=5.0, dte=2)
        # -10% loss at 2 DTE; hard stop tightens to -25%, so it holds.
        assert d.should_exit is False


class TestIvCrush:
    """When IV collapses, the trailing distance tightens so we exit
    faster before vol drain eats the position."""

    def test_iv_crush_tightens_trailing(self):
        # Peak +50%, current +35% — at normal IV this 15-pt drawdown
        # might hold; under IV crush the tighter trail should exit.
        d_normal = _decide(
            entry=5.0, current=6.75, peak=7.5, dte=21,
            entry_iv=0.30, current_iv=0.30,  # no crush
        )
        d_crush = _decide(
            entry=5.0, current=6.75, peak=7.5, dte=21,
            entry_iv=0.40, current_iv=0.20,  # 50% crush
        )
        assert d_crush.iv_crush_detected is True
        assert d_normal.iv_crush_detected is False
        # Crush trail is tighter; the exit decision may flip depending
        # on the exact numbers — verify the trailing floor moved up.
        assert d_crush.trailing_floor_pct > d_normal.trailing_floor_pct, (
            "IV crush should pull the trailing floor higher (less giveback)"
        )

    def test_no_iv_data_skips_crush_detection(self):
        # entry_iv None → no crush detection, normal trailing applies.
        d = _decide(entry=5.0, current=6.0, peak=6.0, dte=21,
                    entry_iv=None, current_iv=None)
        assert d.iv_crush_detected is False


class TestPeakBootstrap:
    """First-cycle case: peak might be None until persisted. Manager
    treats max(entry, current) as the initial peak."""

    def test_peak_none_bootstraps_to_current(self):
        d = _decide(entry=5.0, current=7.0, peak=None, dte=21)
        # Treated as peak=7.0 → at the top, no drawdown
        assert d.drawdown_from_peak_pct == pytest.approx(0.0, abs=0.1)
        assert d.should_exit is False

    def test_peak_promoted_when_current_exceeds_it(self):
        # peak=5 but current=7 — manager promotes peak on the fly.
        d = _decide(entry=5.0, current=7.0, peak=5.0, dte=21)
        # Should treat peak as 7.0
        assert d.drawdown_from_peak_pct == pytest.approx(0.0, abs=0.1)


class TestComputeDte:
    """Sanity check for the date parsing helper."""

    def test_past_dates_clamp_to_zero(self):
        assert compute_dte("1999-01-01") == 0

    def test_invalid_string_returns_zero(self):
        assert compute_dte("not-a-date") == 0
        assert compute_dte("") == 0
