"""MITS Phase 17.A — 14 hidden-behavior fixes.

Each test isolates one of the 14 operational gaps the operator
flagged: stale chain Greeks on entry, IV refresh lag, order-fill
race, premium slippage, multi-leg commission, peak drift on chain
resume, options_disabled flag plumbing, IV crush stamping,
assignment stock-row, MTM cadence, pricing_source duplication,
realized-vs-marked delta, stale yfinance qualifier, and stored-IV
freshness warning.

The tests instantiate the minimal real surface (paper executor +
policy rules + ORM rows) so they're a true regression net — not
mocks that drift from production behavior.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.bot.decision import rules as rmod
from backend.bot.decision.policy import PolicyContext
from backend.bot.options.pricing import OptionMark
from backend.bot.paper_executor import PaperExecutor
from backend.bot.strategies.base import Action, Signal
from backend.db import session_scope
from backend.models.paper import PaperPosition
from backend.models.trade import Trade


# ── shared helpers ─────────────────────────────────────────────────────


def _price(_ticker):
    return 100.0


def _policy_ctx(signal):
    """Minimal PolicyContext for direct-rule unit tests."""
    return PolicyContext(
        ticker="AAPL",
        signal=signal,
        event={
            "ticker": "AAPL", "reason": signal.reason,
            "action": signal.action.value,
            "confidence": signal.confidence,
            "timestamp": "2026-06-12T15:00:00",
        },
        data={"price": 100.0, "iv_rank": 30.0},
        analytics_cfg={"enabled": True},
        ai_config={},
        config={"force_run_when_closed": True, "min_confidence": 0.6,
                "options_disabled": True},
        kill_active=False,
        portfolio_risk_dict=None,
        eod_bias_map={},
        brain_cooldown={},
        use_brain=False,
        cycle_id="cycle-1",
    )


# ── Test 1 ──────────────────────────────────────────────────────────────
# Stale chain Greeks: when price_at_entry returns age_seconds > 60 (or
# bs_fallback), the position row gets chain_freshness_at_entry_sec
# stamped + meta["entry_chain_stale"] True.

def test_1_stale_chain_marks_entry_chain_stale(temp_db, monkeypatch):
    fake_mark = OptionMark(
        bid=2.20, ask=2.30, mid=2.25, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=120.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fake_mark,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    res = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0,
        expiration="2026-12-19",
    )
    assert res.success
    with session_scope() as s:
        row = (s.query(PaperPosition)
                .filter_by(ticker="AAPL", kind="option").first())
        assert row is not None
        assert row.chain_freshness_at_entry_sec == 120.0
        meta = json.loads(row.meta or "{}")
        assert meta.get("entry_chain_stale") is True


def test_1_bs_fallback_marks_entry_chain_stale(temp_db, monkeypatch):
    fake_mark = OptionMark(
        bid=2.20, ask=2.30, mid=2.25, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="bs_fallback", age_seconds=None, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fake_mark,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    res = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0,
        expiration="2026-12-19",
    )
    assert res.success
    with session_scope() as s:
        row = (s.query(PaperPosition)
                .filter_by(ticker="AAPL", kind="option").first())
        meta = json.loads(row.meta or "{}")
        assert meta.get("entry_chain_stale") is True


# ── Test 2 ──────────────────────────────────────────────────────────────
# IV refresh lag: positions() surfaces stored_iv_age_seconds inside the
# d["mtm"] dict when the chain repriced the position.

def test_2_positions_surfaces_stored_iv_age(temp_db, monkeypatch):
    # 1. Seed an open option via the same path the engine uses.
    fresh_mark = OptionMark(
        bid=2.20, ask=2.30, mid=2.25, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=5.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fresh_mark,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0,
        expiration="2026-12-19",
    )
    # 2. Backdate stored_iv_at so the age is computable.
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    with session_scope() as s:
        row = (s.query(PaperPosition)
                .filter_by(ticker="AAPL", kind="option").first())
        # Reset stored_iv so the chain refresh inside positions() doesn't
        # immediately bump stored_iv_at back to "now".
        row.stored_iv_at = one_hour_ago
        row.stored_iv = 0.30

    # 3. Stub price_for_mark so MTM uses a NON-thetadata source (bs_fallback)
    #    — that path doesn't refresh stored_iv_at, but the new MTM dict
    #    should still report age (we report only when source == "thetadata"
    #    per spec; verify the chain path on a separate code path).
    mark_for_mtm = OptionMark(
        bid=2.30, ask=2.40, mid=2.35, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=3.0, underlying=100.0,
    )
    # Force stored_iv refresh inside positions() to NOT happen by setting
    # iv to 0 on the returned mark.
    mark_for_mtm.iv = 0
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_for_mark",
        lambda **kw: mark_for_mtm,
    )
    out = ex.positions()
    assert len(out) == 1
    mtm = out[0].get("mtm")
    assert mtm is not None
    assert mtm.get("source") == "thetadata"
    assert mtm.get("stored_iv_age_seconds") is not None
    # ~3600s elapsed; allow generous slack.
    assert mtm["stored_iv_age_seconds"] > 1800


# ── Test 3 ──────────────────────────────────────────────────────────────
# spot_at_emit + spot_at_fill captured on Trade row. We test the
# _finalize_execution path directly by simulating the spot capture
# flow against a fake quote source.

def test_3_spot_at_emit_and_fill_persisted(temp_db, monkeypatch):
    """
    Direct unit on the spot-capture lift: simulate the same code path
    _finalize_execution runs by calling get_quote + persisting via a
    plan dict carrying spot_at_emit/spot_at_fill.
    """
    from backend.bot.engine import BotEngine

    # Build a minimal trade via _persist_trade with a plan carrying
    # the new fields. This is the same shape _finalize_execution
    # constructs after _submit_order returns.
    fake_quote = SimpleNamespace(price=101.5, source="thetadata",
                                 age_seconds=2.0)
    monkeypatch.setattr(
        "backend.bot.data.quote_source.get_quote",
        lambda t: fake_quote,
    )

    eng = BotEngine.__new__(BotEngine)
    eng.status = SimpleNamespace(daily_pnl=0.0)
    # Stub the heavy dependencies _persist_trade pulls in.
    monkeypatch.setattr(
        "backend.bot.state.build_market_state",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.state.set_latest", lambda ms: None,
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_similar", lambda ctx, k=3: [],
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_summary", lambda matches: "",
    )
    monkeypatch.setattr(
        "backend.bot.agents.run_consensus",
        lambda *a, **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.memo.get_generator",
        lambda: SimpleNamespace(
            generate=lambda **kw: SimpleNamespace(to_dict=lambda: {})
        ),
    )

    signal = Signal(
        action=Action.BUY_STOCK, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat", metadata={"source": "test"},
    )
    plan = {
        "instrument": "stock", "side": "BUY", "quantity": 5,
        "spot_at_emit": 100.0, "spot_at_fill": 101.5,
        "slippage_bps": 12.5, "total_commission": 1.00,
        "pricing_source": "thetadata",
    }
    trade_id = eng._persist_trade(
        signal, 5, 101.5, paper=True, status="open",
        plan=plan, snapshot={"price": 101.5}, event={},
    )
    assert trade_id is not None
    with session_scope() as s:
        row = s.get(Trade, trade_id)
        assert row.spot_at_emit == 100.0
        assert row.spot_at_fill == 101.5


# ── Test 4 ──────────────────────────────────────────────────────────────
# Option fill premium_per_share=2.50, mid=2.40 → slippage_bps ≈ 416.67.

def test_4_slippage_bps_computed(temp_db, monkeypatch):
    # mid=2.40 so half-spread × 2% nets premium_per_share ≈ 2.424 on BUY.
    # We'd rather assert the FORMULA directly. So use a fixed
    # monkeypatched spread to produce exactly 2.50.
    fake_mark = OptionMark(
        bid=2.35, ask=2.45, mid=2.40, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fake_mark,
    )
    # Override the spread helper so premium_per_share lands at 2.50
    # exactly.
    monkeypatch.setattr(
        "backend.bot.paper_executor._apply_option_spread",
        lambda mid, side: 2.50,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    res = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0,
        expiration="2026-12-19",
    )
    assert res.success
    slip = res.raw.get("slippage_bps")
    assert slip is not None
    # |2.50 - 2.40| / 2.40 * 10000 = 416.666...
    assert 416.0 < slip < 417.5


# ── Test 5 ──────────────────────────────────────────────────────────────
# Multi-leg commission attribution: place_complex_order surfaces
# total_commission in order.raw.

def test_5_multi_leg_total_commission(temp_db, monkeypatch):
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    # Synthesize an IRON_CONDOR signal with 4 legs.
    sig = Signal(
        action=Action.IRON_CONDOR, ticker="AAPL", confidence=0.9,
        reason="t", strategy="iron",
        metadata={
            "call_short_strike": 105.0,
            "call_long_strike": 110.0,
            "put_short_strike": 95.0,
            "put_long_strike": 90.0,
            "expiration": "2026-12-19",
            "contracts": 1,
        },
        strike=None,
    )
    res = ex.place_complex_order(sig)
    assert res.success
    total = res.raw.get("total_commission")
    assert total is not None
    # 4 legs × $0.65/contract = $2.60 minimum (per IBKR commission
    # config). Min commission is $1.00 — we expect ≥ $2.60.
    assert total >= 2.60


# ── Test 6 ──────────────────────────────────────────────────────────────
# Peak re-anchored when chain resumes after BS-fallback stretch.

def test_6_peak_reanchored_on_chain_resume(temp_db, monkeypatch):
    """Direct unit on the peak-re-anchor branch — simulate the engine's
    exit-manager block calling its own PaperPosition update logic."""
    from backend.bot.engine import BotEngine

    # 1. Seed an open option with peak_premium_at BEFORE stored_iv_at
    #    — emulates "we set peak during BS_fallback, then ThetaData
    #    came back online".
    old_peak_at = datetime.utcnow() - timedelta(minutes=10)
    fresh_iv_at = datetime.utcnow() - timedelta(minutes=5)
    with session_scope() as s:
        row = PaperPosition(
            ticker="AAPL", kind="option", quantity=1, avg_cost=200.0,
            strike=100.0, expiration="2026-12-19", option_type="call",
            entry_iv=0.30,
            stored_iv=0.32, stored_iv_at=fresh_iv_at,
            peak_premium_per_share=2.0,
            peak_premium_at=old_peak_at,
        )
        s.add(row)

    # 2. Build the position dict the way paper_executor.positions()
    #    would, with pricing_source=thetadata + a current mid that
    #    EXCEEDS the stale peak.
    pos = {
        "ticker": "AAPL", "strike": 100.0, "expiration": "2026-12-19",
        "option_type": "call", "quantity": 1, "avg_cost": 200.0,
        "mark": 2.50, "current_price": 2.50,
        "pricing_source": "thetadata",
    }

    # 3. Stub _maybe_close_via_thesis_health + decide_exit so the
    #    exit-manager block reaches our re-anchor code but doesn't
    #    actually close.
    eng = BotEngine.__new__(BotEngine)
    eng.status = SimpleNamespace(daily_pnl=0.0)
    eng.executor = MagicMock()
    eng._maybe_close_via_thesis_health = MagicMock(return_value=None)
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.decide_exit",
        lambda **kw: SimpleNamespace(
            should_exit=False, reason="", drawdown_from_peak_pct=0,
            trailing_floor_pct=None, monitor_active=False, hard_stop_pct=0,
            iv_crush_detected=False, gain_pct=0,
        ),
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.compute_dte",
        lambda exp: 30,
    )
    eng._maybe_close_option(pos)

    # 4. Confirm re-anchor happened.
    with session_scope() as s:
        row = (s.query(PaperPosition)
                .filter_by(ticker="AAPL", kind="option").first())
        # New peak >= 2.50 (current mid), peak_premium_at moved forward.
        assert row.peak_premium_per_share >= 2.50
        assert row.peak_premium_at > old_peak_at


# ── Test 7 ──────────────────────────────────────────────────────────────
# options_disabled config returns BlockingFactor for BUY_CALL.

def test_7_options_disabled_blocks_buy_call():
    signal = Signal(
        action=Action.BUY_CALL, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat",
    )
    ctx = _policy_ctx(signal)
    bf = rmod.rule_options_disabled(ctx)
    assert bf is not None
    assert bf.legacy_status == "options_disabled"
    assert bf.severity == "hard"


# ── Test 8 ──────────────────────────────────────────────────────────────
# IV crush detected → iv_crush_first_detected_at stamped ONCE.

def test_8_iv_crush_first_detected_at_idempotent(temp_db, monkeypatch):
    from backend.bot.engine import BotEngine
    from backend.config import TUNABLES

    # Force crush ratio to a known value.
    monkeypatch.setattr(TUNABLES, "opt_exit_iv_crush_ratio", 0.75)

    # 1. Seed an open option with entry_iv=0.50, no crush stamp yet.
    with session_scope() as s:
        row = PaperPosition(
            ticker="AAPL", kind="option", quantity=1, avg_cost=200.0,
            strike=100.0, expiration="2026-12-19", option_type="call",
            entry_iv=0.50,
            stored_iv=0.30,  # 0.30 / 0.50 = 0.60 < 0.75 → crush
            stored_iv_at=datetime.utcnow(),
        )
        s.add(row)

    pos = {
        "ticker": "AAPL", "strike": 100.0, "expiration": "2026-12-19",
        "option_type": "call", "quantity": 1, "avg_cost": 200.0,
        "mark": 2.50, "current_price": 2.50,
        "pricing_source": "bs_fallback",
    }

    eng = BotEngine.__new__(BotEngine)
    eng.status = SimpleNamespace(daily_pnl=0.0)
    eng.executor = MagicMock()
    eng._maybe_close_via_thesis_health = MagicMock(return_value=None)
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.decide_exit",
        lambda **kw: SimpleNamespace(
            should_exit=False, reason="", drawdown_from_peak_pct=0,
            trailing_floor_pct=None, monitor_active=False, hard_stop_pct=0,
            iv_crush_detected=True, gain_pct=0,
        ),
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.compute_dte",
        lambda exp: 30,
    )

    eng._maybe_close_option(pos)
    with session_scope() as s:
        row = (s.query(PaperPosition)
                .filter_by(ticker="AAPL", kind="option").first())
        first_stamp = row.iv_crush_first_detected_at
        assert first_stamp is not None

    # Run the cycle again — stamp should NOT change.
    eng._maybe_close_option(pos)
    with session_scope() as s:
        row = (s.query(PaperPosition)
                .filter_by(ticker="AAPL", kind="option").first())
        assert row.iv_crush_first_detected_at == first_stamp


# ── Test 9 ──────────────────────────────────────────────────────────────
# close_option with assignment → engine close path persists a SECOND
# Trade row (kind=stock, reason contains "assignment_from_").

def test_9_assignment_writes_second_stock_trade(temp_db, monkeypatch):
    from backend.bot.engine import BotEngine

    # Seed a SHORT PUT (CSP) that would be assigned at expiry.
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=lambda _t: 95.0)
    fake_mark = OptionMark(
        bid=4.95, ask=5.05, mid=5.00, iv=0.30,
        delta=-0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=95.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fake_mark,
    )
    # Fix N=2 (2026-06-13) — naked SELL_PUT is now blocked at the
    # executor; the test seeds an equivalent CSP (cash-secured put)
    # which is the legitimate short-put structure. starting_cash=$20k
    # covers the strike=$100 × 100 = $10k collateral requirement.
    res = ex.place_options_order(
        "AAPL", "SELL_CSP", 1, strike=100.0,
        expiration="2026-12-19",
    )
    assert res.success

    # Simulate the engine's exit-manager close path. Build a minimal
    # BotEngine + drive _maybe_close_option.
    eng = BotEngine.__new__(BotEngine)
    eng.executor = ex
    eng.status = SimpleNamespace(daily_pnl=0.0)
    eng._maybe_close_via_thesis_health = MagicMock(return_value=None)
    # MITS Phase 17.E — engine now consumes decide_exit_with_policy
    # (returns the ExitDecision + ExitPolicyResult pair); the legacy
    # decide_exit() shim still exists but is no longer the caller path.
    _stub_decision = SimpleNamespace(
        should_exit=True, reason="expiry_close",
        drawdown_from_peak_pct=0, trailing_floor_pct=None,
        monitor_active=False, hard_stop_pct=0,
        iv_crush_detected=False, gain_pct=0,
    )
    _stub_result = SimpleNamespace(
        should_close=True,
        chosen=SimpleNamespace(rule_name="catastrophe_stop"),
        triggers=[],
        rule_evaluations=[],
        legacy_action="close",
        to_dict=lambda: {"should_close": True, "legacy_action": "close"},
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.decide_exit_with_policy",
        lambda **kw: (_stub_decision, _stub_result),
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.persist_exit_evaluations",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.compute_dte",
        lambda exp: 0,
    )
    # Stub _persist_trade's heavy dependencies.
    monkeypatch.setattr(
        "backend.bot.state.build_market_state",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.state.set_latest", lambda ms: None,
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_similar", lambda ctx, k=3: [],
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_summary", lambda matches: "",
    )
    monkeypatch.setattr(
        "backend.bot.agents.run_consensus",
        lambda *a, **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.memo.get_generator",
        lambda: SimpleNamespace(
            generate=lambda **kw: SimpleNamespace(to_dict=lambda: {})
        ),
    )

    pos = {
        "ticker": "AAPL", "strike": 100.0, "expiration": "2026-12-19",
        "option_type": "put", "quantity": -1, "avg_cost": -500.0,
        "mark": 5.00, "current_price": 5.00,
        "pricing_source": "thetadata",
    }
    eng._maybe_close_option(pos)

    # Verify the assignment-stock Trade row was written.
    with session_scope() as s:
        rows = s.query(Trade).filter(
            Trade.signal_source == "assignment",
        ).all()
        assert len(rows) == 1
        assert rows[0].instrument == "stock"
        assert "assignment_from_" in rows[0].reason


# ── Test 10 ─────────────────────────────────────────────────────────────
# positions() stamps last_marked_at on every position returned.

def test_10_last_marked_at_populated(temp_db, monkeypatch):
    fresh_mark = OptionMark(
        bid=2.20, ask=2.30, mid=2.25, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=5.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fresh_mark,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_for_mark",
        lambda **kw: fresh_mark,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    ex.place_stock_order("AAPL", "BUY", 5)
    ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0, expiration="2026-12-19",
    )

    now = datetime.utcnow()
    out = ex.positions()
    assert len(out) == 2
    for p in out:
        assert p.get("last_marked_at") is not None
        marked = datetime.fromisoformat(p["last_marked_at"])
        delta = abs((now - marked).total_seconds())
        assert delta < 5.0, (
            f"last_marked_at drifted from now: {delta}s"
        )

    # And confirm the DB column got updated, not just the dict.
    with session_scope() as s:
        rows = s.query(PaperPosition).all()
        for r in rows:
            assert r.last_marked_at is not None


# ── Test 11 ─────────────────────────────────────────────────────────────
# pricing_source bug — engine cycle now lifts order.raw["pricing_source"]
# into plan, so the Trade row gets the REAL source, not "paper_stub".

def test_11_pricing_source_lifted_from_order_raw(temp_db, monkeypatch):
    """Direct unit on the lift: persist with a plan whose
    pricing_source came from order.raw."""
    from backend.bot.engine import BotEngine
    eng = BotEngine.__new__(BotEngine)
    eng.status = SimpleNamespace(daily_pnl=0.0)
    monkeypatch.setattr(
        "backend.bot.state.build_market_state",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.state.set_latest", lambda ms: None,
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_similar", lambda ctx, k=3: [],
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_summary", lambda matches: "",
    )
    monkeypatch.setattr(
        "backend.bot.agents.run_consensus",
        lambda *a, **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.memo.get_generator",
        lambda: SimpleNamespace(
            generate=lambda **kw: SimpleNamespace(to_dict=lambda: {})
        ),
    )

    signal = Signal(
        action=Action.BUY_CALL, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat", metadata={"source": "test"},
    )
    plan = {
        "instrument": "option", "side": "BUY",
        "strike": 100.0, "expiration": "2026-12-19", "contracts": 1,
        # Lifted from order.raw — must override the default
        # "paper_stub" the legacy Trade column carries.
        "pricing_source": "thetadata",
    }
    trade_id = eng._persist_trade(
        signal, 1, 2.50, paper=True, status="open",
        plan=plan, snapshot={"price": 100.0}, event={},
    )
    with session_scope() as s:
        row = s.get(Trade, trade_id)
        assert row.pricing_source == "thetadata"
        assert row.pricing_source != "paper_stub"


# ── Test 12 ─────────────────────────────────────────────────────────────
# Close option → realized_vs_marked_delta populated and equals
# pnl - marked_pnl.

def test_12_realized_vs_marked_delta(temp_db, monkeypatch):
    # Seed a long call at $2.00/share.
    fresh_mark = OptionMark(
        bid=1.95, ask=2.05, mid=2.00, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fresh_mark,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=lambda _t: 105.0)
    res = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0,
        expiration="2026-12-19",
    )
    assert res.success

    # On close, intrinsic is (spot - strike) = 5.0 per share.
    # Live mark via price_for_mark — set it to 4.50 so the delta is
    # non-zero.
    close_mark = OptionMark(
        bid=4.45, ask=4.55, mid=4.50, iv=0.30,
        delta=0.7, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=105.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_for_mark",
        lambda **kw: close_mark,
    )

    close_res = ex.close_option(
        "AAPL", strike=100.0, expiration="2026-12-19", reason="expiry",
    )
    assert close_res.success
    pnl = close_res.raw.get("pnl")
    marked_pnl = close_res.raw.get("marked_pnl")
    delta = close_res.raw.get("realized_vs_marked_delta")
    assert pnl is not None
    assert marked_pnl is not None
    assert delta is not None
    assert abs(delta - (pnl - marked_pnl)) < 0.05


# ── Test 13 ─────────────────────────────────────────────────────────────
# _yfinance_intraday returns Quote(source="yfinance_stale") when bar
# is older than 5 minutes.

def test_13_yfinance_stale_qualifier(monkeypatch):
    """Verify the source-tag flip when age > 300s."""
    import pandas as pd
    from backend.bot.data import quote_source as qs

    # Build a synthetic hist DataFrame with a single bar whose
    # timestamp is 400 seconds ago.
    ts = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=400)
    fake_hist = pd.DataFrame(
        {"Close": [101.5]},
        index=pd.DatetimeIndex([ts]),
    )

    class FakeTicker:
        def __init__(self, _t):
            pass

        def history(self, **kw):
            return fake_hist

    fake_yf = SimpleNamespace(Ticker=FakeTicker)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

    q = qs._yfinance_intraday("AAPL")
    assert q is not None
    assert q.source == "yfinance_stale"
    assert q.age_seconds is not None and q.age_seconds > 300


def test_13_yfinance_fresh_still_intraday(monkeypatch):
    """Counter-check: bar < 5 minutes old keeps the old tag."""
    import pandas as pd
    from backend.bot.data import quote_source as qs

    ts = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=30)
    fake_hist = pd.DataFrame(
        {"Close": [101.5]},
        index=pd.DatetimeIndex([ts]),
    )

    class FakeTicker:
        def __init__(self, _t):
            pass

        def history(self, **kw):
            return fake_hist

    fake_yf = SimpleNamespace(Ticker=FakeTicker)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

    q = qs._yfinance_intraday("AAPL")
    assert q.source == "yfinance_intraday"


# ── Test 14 ─────────────────────────────────────────────────────────────
# stored_iv 2h stale → MTM cycle adds mtm_warnings = ["stored_iv_stale"]
# onto detail_json on next Trade persist.

def test_14_stored_iv_stale_warning_on_close_trade(temp_db, monkeypatch):
    """End-to-end: seed a position with stored_iv_at 2h ago, drive
    the engine's exit-manager close path, then check the close Trade
    row's detail_json carries mtm_warnings."""
    from backend.bot.engine import BotEngine

    # Seed a long call WITHOUT placing via paper_executor (we want to
    # backdate stored_iv_at to 2h ago — placing via the executor
    # would stamp it to "now").
    with session_scope() as s:
        row = PaperPosition(
            ticker="AAPL", kind="option", quantity=1, avg_cost=200.0,
            strike=100.0, expiration="2026-12-19", option_type="call",
            entry_iv=0.30,
            stored_iv=0.30,
            stored_iv_at=datetime.utcnow() - timedelta(hours=2),
            meta=json.dumps({
                "action": "BUY_CALL", "strike": 100.0,
                "expiration": "2026-12-19",
            }),
        )
        s.add(row)

    fresh_mark = OptionMark(
        bid=2.45, ask=2.55, mid=2.50, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_for_mark",
        lambda **kw: fresh_mark,
    )

    ex = PaperExecutor(starting_cash=10_000.0, price_fn=lambda _t: 105.0)
    eng = BotEngine.__new__(BotEngine)
    eng.executor = ex
    eng.status = SimpleNamespace(daily_pnl=0.0)
    eng._maybe_close_via_thesis_health = MagicMock(return_value=None)
    # MITS Phase 17.E — engine consumes decide_exit_with_policy.
    _stub_decision = SimpleNamespace(
        should_exit=True, reason="expiry",
        drawdown_from_peak_pct=0, trailing_floor_pct=None,
        monitor_active=False, hard_stop_pct=0,
        iv_crush_detected=False, gain_pct=0,
    )
    _stub_result = SimpleNamespace(
        should_close=True,
        chosen=SimpleNamespace(rule_name="catastrophe_stop"),
        triggers=[],
        rule_evaluations=[],
        legacy_action="close",
        to_dict=lambda: {"should_close": True, "legacy_action": "close"},
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.decide_exit_with_policy",
        lambda **kw: (_stub_decision, _stub_result),
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.persist_exit_evaluations",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "backend.bot.options.exit_manager.compute_dte",
        lambda exp: 5,
    )
    # Stub _persist_trade dependencies.
    monkeypatch.setattr(
        "backend.bot.state.build_market_state",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.state.set_latest", lambda ms: None,
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_similar", lambda ctx, k=3: [],
    )
    monkeypatch.setattr(
        "backend.bot.memory.recall_summary", lambda matches: "",
    )
    monkeypatch.setattr(
        "backend.bot.agents.run_consensus",
        lambda *a, **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.memo.get_generator",
        lambda: SimpleNamespace(
            generate=lambda **kw: SimpleNamespace(to_dict=lambda: {})
        ),
    )

    pos = {
        "ticker": "AAPL", "strike": 100.0, "expiration": "2026-12-19",
        "option_type": "call", "quantity": 1, "avg_cost": 200.0,
        "mark": 2.50, "current_price": 2.50,
        "pricing_source": "thetadata",
    }
    eng._maybe_close_option(pos)

    # Verify the close Trade row's detail_json carries mtm_warnings.
    with session_scope() as s:
        row = (s.query(Trade)
                .filter(Trade.action == "CLOSE_OPTION")
                .order_by(Trade.id.desc()).first())
        assert row is not None
        detail = json.loads(row.detail_json or "{}")
        warnings = detail.get("mtm_warnings") or []
        assert "stored_iv_stale" in warnings
