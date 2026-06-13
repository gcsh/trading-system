"""MITS Phase 17.B — FillSnapshot fill-provenance regression net.

Each test isolates one observable in the snapshot path:
  1-3. The FillSnapshot dataclass itself — option / stock / spread_pct.
  4.   Single-leg option order writes fill_snapshot_json on order.raw.
  5.   Stock order writes fill_snapshot_json on order.raw.
  6.   Iron condor (4 legs) writes ``{"legs": [...]}`` envelope.

Hits real production surfaces (PaperExecutor + OptionMark + Quote)
so the suite catches regressions on rename / refactor without mocks
drifting.
"""
from __future__ import annotations

import json

import pytest

from backend.bot.data.quote_source import Quote
from backend.bot.execution.fill_snapshot import FillSnapshot
from backend.bot.options.pricing import OptionMark
from backend.bot.paper_executor import PaperExecutor
from backend.bot.strategies.base import Action, Signal


# ── shared helpers ─────────────────────────────────────────────────────


def _price(_ticker):
    return 100.0


# ── Test 1 ──────────────────────────────────────────────────────────────
# FillSnapshot.from_option_mark populates the full 14 fields + captured_at
# + source. spread_pct derived from bid/ask/mid.


def test_fill_snapshot_from_option_mark_has_14_fields():
    mark = OptionMark(
        bid=2.40, ask=2.55, mid=2.475, iv=0.32,
        delta=0.45, gamma=0.08, theta=-0.04, vega=0.12,
        source="thetadata", age_seconds=2.3, underlying=101.2,
    )
    snap = FillSnapshot.from_option_mark(
        mark,
        commission=0.65, fill_price=2.50,
        slippage_bps=10.1, spread_paid=0.025,
    )
    d = snap.to_dict()
    # 14 numeric/string observables + captured_at + source (already in the
    # 14 via "source") — verify all keys exist and the typed fields land.
    expected_keys = {
        "bid", "ask", "mid", "spread_pct", "iv", "delta", "gamma",
        "theta", "vega", "underlying", "source", "age_seconds",
        "commission", "spread_paid", "slippage_bps", "captured_at",
    }
    assert set(d.keys()) == expected_keys
    assert d["bid"] == 2.40
    assert d["ask"] == 2.55
    assert d["mid"] == 2.475
    assert d["iv"] == 0.32
    assert d["delta"] == 0.45
    assert d["gamma"] == 0.08
    assert d["theta"] == -0.04
    assert d["vega"] == 0.12
    assert d["underlying"] == 101.2
    assert d["source"] == "thetadata"
    assert d["age_seconds"] == 2.3
    assert d["commission"] == 0.65
    assert d["spread_paid"] == 0.025
    assert d["slippage_bps"] == 10.1
    assert d["captured_at"]  # ISO timestamp string


# ── Test 2 ──────────────────────────────────────────────────────────────
# FillSnapshot.from_stock_quote — quote has only price+source+age.
# bid/ask/iv/greeks/underlying must be None; mid/source/age/commission/
# slippage_bps populated.


def test_fill_snapshot_from_stock_quote_has_required_fields():
    quote = Quote(price=100.05, source="thetadata", age_seconds=2.3)
    snap = FillSnapshot.from_stock_quote(
        quote, commission=1.00, fill_price=100.05, slippage_bps=5.0,
    )
    d = snap.to_dict()
    assert d["mid"] == 100.05
    assert d["source"] == "thetadata"
    assert d["age_seconds"] == 2.3
    assert d["commission"] == 1.00
    assert d["slippage_bps"] == 5.0
    # Stock quotes don't carry these.
    assert d["bid"] is None
    assert d["ask"] is None
    assert d["spread_pct"] is None
    assert d["spread_paid"] is None
    assert d["iv"] is None
    assert d["delta"] is None
    assert d["gamma"] is None
    assert d["theta"] is None
    assert d["vega"] is None
    assert d["underlying"] is None
    assert d["captured_at"]


# ── Test 3 ──────────────────────────────────────────────────────────────
# spread_pct = (ask - bid) / mid * 100.


def test_fill_snapshot_spread_pct_calculation():
    mark = OptionMark(
        bid=2.40, ask=2.55, mid=2.475, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=100.0,
    )
    snap = FillSnapshot.from_option_mark(
        mark, commission=0.65, fill_price=2.50,
        slippage_bps=0.0, spread_paid=0.0,
    )
    # (2.55 - 2.40) / 2.475 * 100 = 6.0606...
    assert snap.spread_pct is not None
    assert abs(snap.spread_pct - 6.0606) < 0.001


def test_fill_snapshot_spread_pct_none_when_missing_book():
    """No bid/ask → spread_pct is None, not zero."""
    mark = OptionMark(
        bid=None, ask=None, mid=2.50, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="bs_fallback", age_seconds=None, underlying=100.0,
    )
    snap = FillSnapshot.from_option_mark(
        mark, commission=0.65, fill_price=2.50,
        slippage_bps=0.0, spread_paid=0.0,
    )
    assert snap.spread_pct is None


# ── Test 4 ──────────────────────────────────────────────────────────────
# place_options_order returns fill_snapshot_json with all 14 fields.


def test_paper_executor_options_order_returns_fill_snapshot_json(
    temp_db, monkeypatch,
):
    fake_mark = OptionMark(
        bid=2.20, ask=2.30, mid=2.25, iv=0.30,
        delta=0.5, gamma=0.1, theta=-0.05, vega=0.1,
        source="thetadata", age_seconds=2.0, underlying=100.0,
    )
    monkeypatch.setattr(
        "backend.bot.options.pricing.price_at_entry",
        lambda **kw: fake_mark,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    res = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0, expiration="2026-12-19",
    )
    assert res.success
    blob = res.raw.get("fill_snapshot_json")
    assert blob is not None
    payload = json.loads(blob)
    # Key shape mirrors FillSnapshot.from_option_mark.
    for key in (
        "bid", "ask", "mid", "spread_pct", "iv", "delta", "gamma",
        "theta", "vega", "underlying", "source", "age_seconds",
        "commission", "spread_paid", "slippage_bps", "captured_at",
    ):
        assert key in payload, f"missing key {key} in snapshot"
    assert payload["bid"] == 2.20
    assert payload["ask"] == 2.30
    assert payload["mid"] == 2.25
    assert payload["source"] == "thetadata"
    assert payload["iv"] == 0.30
    assert payload["delta"] == 0.5
    assert payload["underlying"] == 100.0
    # spread_pct = (2.30 - 2.20) / 2.25 * 100 = 4.444
    assert abs(payload["spread_pct"] - 4.4444) < 0.001


# ── Test 5 ──────────────────────────────────────────────────────────────
# place_stock_order returns fill_snapshot_json — 8 populated fields.


def test_paper_executor_stock_order_returns_fill_snapshot_json(
    temp_db, monkeypatch,
):
    # Stub get_quote so the snapshot doesn't depend on yfinance.
    fake_quote = Quote(price=100.0, source="thetadata", age_seconds=1.5)
    monkeypatch.setattr(
        "backend.bot.data.quote_source.get_quote",
        lambda t: fake_quote,
    )
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    res = ex.place_stock_order("AAPL", "BUY", 5)
    assert res.success
    blob = res.raw.get("fill_snapshot_json")
    assert blob is not None
    payload = json.loads(blob)
    # Populated.
    assert payload["mid"] == 100.0
    assert payload["source"] == "thetadata"
    assert payload["age_seconds"] == 1.5
    assert payload["commission"] is not None
    assert payload["slippage_bps"] is not None
    assert payload["captured_at"]
    # Stocks carry no greeks/IV/book.
    for missing_key in (
        "bid", "ask", "spread_pct", "spread_paid",
        "iv", "delta", "gamma", "theta", "vega", "underlying",
    ):
        assert payload[missing_key] is None, (
            f"stock snapshot leaked {missing_key}={payload[missing_key]!r}"
        )


# ── Test 6 ──────────────────────────────────────────────────────────────
# place_complex_order — iron condor (4 legs) → ``{"legs": [...]}`` with
# one snapshot per leg.


def test_paper_executor_complex_order_returns_legs_array(temp_db):
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    sig = Signal(
        action=Action.IRON_CONDOR, ticker="AAPL", confidence=0.9,
        reason="t", strategy="iron",
        metadata={
            "call_short_strike": 105.0,
            "call_long_strike":  110.0,
            "put_short_strike":   95.0,
            "put_long_strike":    90.0,
            "expiration": "2026-12-19",
            "contracts": 1,
        },
        strike=None,
    )
    res = ex.place_complex_order(sig)
    assert res.success
    blob = res.raw.get("fill_snapshot_json")
    assert blob is not None
    payload = json.loads(blob)
    assert "legs" in payload
    legs = payload["legs"]
    assert isinstance(legs, list)
    assert len(legs) == 4, f"expected 4 legs, got {len(legs)}"
    for leg in legs:
        for key in (
            "bid", "ask", "mid", "spread_pct", "iv", "delta", "gamma",
            "theta", "vega", "underlying", "source", "age_seconds",
            "commission", "spread_paid", "slippage_bps", "captured_at",
        ):
            assert key in leg, f"missing key {key} in leg snapshot"
        # Per-leg metadata sidecar.
        assert leg["kind"] in ("call", "put")
        assert leg["side"] in ("BUY", "SELL")
        assert leg["strike"] > 0
        assert leg["contracts"] >= 1


# ── Test 7 ──────────────────────────────────────────────────────────────
# Engine plumbing — _persist_trade writes plan["fill_snapshot_json"]
# straight to Trade.fill_snapshot_json.


def test_persist_trade_writes_fill_snapshot_json(temp_db, monkeypatch):
    from types import SimpleNamespace
    from backend.bot.engine import BotEngine
    from backend.db import session_scope
    from backend.models.trade import Trade

    eng = BotEngine.__new__(BotEngine)
    eng.status = SimpleNamespace(daily_pnl=0.0)
    # Stub heavy dependencies _persist_trade touches.
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
    snapshot_payload = json.dumps({
        "bid": 99.95, "ask": 100.05, "mid": 100.0,
        "spread_pct": 0.10, "iv": None, "delta": None, "gamma": None,
        "theta": None, "vega": None, "underlying": None,
        "source": "thetadata", "age_seconds": 1.0,
        "commission": 1.0, "spread_paid": 0.0, "slippage_bps": 5.0,
        "captured_at": "2026-06-12T15:00:00",
    })
    plan = {
        "instrument": "stock", "side": "BUY", "quantity": 5,
        "pricing_source": "thetadata",
        "fill_snapshot_json": snapshot_payload,
    }
    trade_id = eng._persist_trade(
        signal, 5, 100.0, paper=True, status="open",
        plan=plan, snapshot={"price": 100.0}, event={},
    )
    assert trade_id is not None
    with session_scope() as s:
        row = s.get(Trade, trade_id)
        assert row.fill_snapshot_json == snapshot_payload
        decoded = json.loads(row.fill_snapshot_json)
        assert decoded["mid"] == 100.0
        assert decoded["source"] == "thetadata"
