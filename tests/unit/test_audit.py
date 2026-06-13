"""Audit / invariants — the safety net that catches bad trade data BEFORE it
hits the DB. Same class of bug that produced ``strike=215.35``, CSP labeled
``spread``, and a +191% return from a polluted realized_pnl.
"""
import pytest

from backend.bot.audit import (
    AuditViolation,
    audit_account_write,
    audit_open_options,
    audit_order_plan,
    check_expiration_in_future,
    check_instrument_matches_action,
    check_option_has_required_fields,
    check_strike_is_snapped,
    reconcile_account,
)


# ── individual invariants ───────────────────────────────────────────────────


class TestStrikeInvariant:
    def test_215_is_clean(self):
        check_strike_is_snapped(215.0)
    def test_215_35_is_rejected(self):
        with pytest.raises(AuditViolation) as ei:
            check_strike_is_snapped(215.35)
        assert ei.value.name == "strike_not_snapped"
    def test_zero_strike_rejected(self):
        with pytest.raises(AuditViolation) as ei:
            check_strike_is_snapped(0)
        assert ei.value.name == "strike_missing"


class TestInstrumentInvariant:
    def test_buy_stock_with_stock_instrument(self):
        check_instrument_matches_action("BUY_STOCK", "stock")
    def test_csp_must_be_option_not_spread(self):
        with pytest.raises(AuditViolation) as ei:
            check_instrument_matches_action("SELL_CSP", "spread")
        assert ei.value.name == "instrument_mismatch"
    def test_covered_call_must_be_option(self):
        with pytest.raises(AuditViolation):
            check_instrument_matches_action("SELL_COVERED_CALL", "spread")
    def test_bull_call_spread_with_stock_rejected(self):
        with pytest.raises(AuditViolation):
            check_instrument_matches_action("BULL_CALL_SPREAD", "stock")
    def test_iron_condor_is_spread(self):
        check_instrument_matches_action("IRON_CONDOR", "spread")
    def test_buy_call_with_option(self):
        check_instrument_matches_action("BUY_CALL", "option")


class TestOptionFieldsInvariant:
    def test_stock_doesnt_need_strike(self):
        check_option_has_required_fields("BUY_STOCK", "stock", None, None)
    def test_option_without_strike_rejected(self):
        with pytest.raises(AuditViolation) as ei:
            check_option_has_required_fields("BUY_CALL", "option", None, "2026-06-30")
        assert ei.value.name == "option_fields_missing"
    def test_option_without_expiration_rejected(self):
        with pytest.raises(AuditViolation):
            check_option_has_required_fields("BUY_CALL", "option", 215.0, None)


class TestExpirationInvariant:
    def test_future_expiration_ok(self):
        check_expiration_in_future("2030-01-01")
    def test_past_expiration_rejected(self):
        with pytest.raises(AuditViolation) as ei:
            check_expiration_in_future("2020-01-01")
        assert ei.value.name == "expiration_in_past"


# ── combined order-plan audit ───────────────────────────────────────────────


class TestAuditOrderPlan:
    def test_clean_buy_call(self):
        plan = {"instrument": "option", "strike": 215.0, "expiration": "2030-06-30"}
        result = audit_order_plan("BUY_CALL", plan)
        assert result.ok
        assert result.violations == []

    def test_csp_labeled_spread_blocks(self):
        plan = {"instrument": "spread", "strike": 200.0, "expiration": "2030-06-30"}
        result = audit_order_plan("SELL_CSP", plan)
        assert not result.ok
        assert any(v["name"] == "instrument_mismatch" for v in result.violations)

    def test_unsnapped_strike_blocks(self):
        plan = {"instrument": "option", "strike": 215.35, "expiration": "2030-06-30"}
        result = audit_order_plan("BUY_CALL", plan)
        assert not result.ok
        assert any(v["name"] == "strike_not_snapped" for v in result.violations)

    def test_multiple_violations_all_reported(self):
        # mislabeled AND unsnapped — both should be in violations
        plan = {"instrument": "spread", "strike": 215.35, "expiration": "2030-06-30"}
        result = audit_order_plan("SELL_CSP", plan)
        names = {v["name"] for v in result.violations}
        assert "instrument_mismatch" in names
        assert "strike_not_snapped" in names


# ── reconciliation ──────────────────────────────────────────────────────────


class TestAccountReconciliation:
    def test_clean_account_passes(self):
        result = reconcile_account(cash=1000, realized_pnl=0,
                                    positions_market_value=4000, portfolio_value=5000)
        assert result.ok

    def test_drift_caught(self):
        # The bug we just fixed: cash + positions != portfolio_value
        result = reconcile_account(cash=10560.92, realized_pnl=8930.95,
                                    positions_market_value=4017.50, portfolio_value=14578.42)
        # cash + market = 14578.42 — should match portfolio_value within tolerance
        assert result.ok
        # but if portfolio_value didn't match (e.g. drift from synthetic write):
        bad = reconcile_account(cash=353.92, realized_pnl=-646.05,
                                 positions_market_value=4017.50, portfolio_value=14578.42)
        assert not bad.ok
        assert bad.violations[0]["name"] == "account_pv_drift"


# ── expired-option audit ────────────────────────────────────────────────────


class TestOpenOptionAudit:
    def test_future_expiration_passes(self):
        positions = [{"kind": "option", "ticker": "NVDA", "strike": 215.0,
                       "expiration": "2030-01-01", "option_type": "call", "quantity": 1}]
        assert audit_open_options(positions).ok

    def test_expired_open_position_flagged(self):
        positions = [{"kind": "option", "ticker": "NVDA", "strike": 215.0,
                       "expiration": "2020-01-01", "option_type": "call", "quantity": 1}]
        result = audit_open_options(positions)
        assert not result.ok
        assert result.violations[0]["name"] == "option_expired_still_open"


# ── account-write audit ─────────────────────────────────────────────────────


class TestAccountWriteAudit:
    def test_writes_without_reason_rejected(self):
        with pytest.raises(AuditViolation):
            audit_account_write("", cash_delta=100, realized_delta=0)

    def test_zero_deltas_ok_even_no_reason(self):
        audit_account_write("", cash_delta=0, realized_delta=0)

    def test_synthetic_blocked_when_lock_set(self, monkeypatch):
        monkeypatch.setenv("TB_LOCK_ACCOUNT_WRITES", "1")
        with pytest.raises(AuditViolation) as ei:
            audit_account_write("test_plant", cash_delta=10000, realized_delta=9500)
        assert ei.value.name == "account_write_synthetic"

    def test_real_reason_passes_with_lock(self, monkeypatch):
        monkeypatch.setenv("TB_LOCK_ACCOUNT_WRITES", "1")
        audit_account_write("buy fill: NVDA 215 call", cash_delta=-630, realized_delta=0)
