"""Stage-18b — Wire Wave 1 data (FRED, breadth, EDGAR) into agents."""
import json
from datetime import datetime, timedelta

import pytest

from backend.bot.agents import (
    STANCE_BUY,
    STANCE_SELL,
    agent_macro,
    agent_microstructure,
)
from backend.bot.state import build_market_state, reset_latest


@pytest.fixture(autouse=True)
def _isolate():
    reset_latest()
    yield
    reset_latest()


def _macro_panel(*, curve_inverted=False, hy=2.5, nfci=-0.30,
                    spread=0.5, hy_change=None):
    return {
        "DFF": {"value": 4.0, "date": "2026-05-28", "change_30d_pct": 0.0},
        "DGS10": {"value": 4.4, "date": "2026-05-28", "change_30d_pct": 0.01},
        "DGS2": {"value": 3.9, "date": "2026-05-28", "change_30d_pct": 0.01},
        "BAMLH0A0HYM2": {"value": hy, "date": "2026-05-28",
                            "change_30d_pct": hy_change},
        "NFCI": {"value": nfci, "date": "2026-05-22",
                   "change_30d_pct": 0.0},
        "yield_curve_inverted": curve_inverted,
        "spread_10y_2y": spread,
    }


# ── FRED → agent_macro ──────────────────────────────────────────────────


class TestMacroAgentWithFred:
    def test_curve_inversion_plus_hy_widening_vetoes_long(self):
        ctx = {
            "ticker": "NVDA", "action": "BUY_CALL",
            "analytics": {"features": {"vix": 16}},
            "snapshot": {"spy_trend": "bullish", "vix": 16},
            "cross_asset": {"equities": "risk_on"},
            "macro": _macro_panel(curve_inverted=True, hy=4.2,
                                       hy_change=0.20, spread=-0.30),
        }
        v = agent_macro(ctx)
        assert v.stance == STANCE_SELL
        assert "yield curve" in v.reasoning.lower() \
            or "HY" in v.reasoning

    def test_loose_conditions_boost_long(self):
        ctx = {
            "ticker": "NVDA", "action": "BUY_CALL",
            "analytics": {"features": {"vix": 14, "news_sentiment": 0.3}},
            "snapshot": {"spy_trend": "bullish", "vix": 14},
            "cross_asset": {"equities": "risk_on"},
            "macro": _macro_panel(hy=2.5, nfci=-0.50),
        }
        v = agent_macro(ctx)
        # risk_on + macro boosts → BUY with high confidence
        assert v.stance == STANCE_BUY
        assert v.confidence > 0.65

    def test_no_macro_data_falls_through(self):
        ctx = {
            "ticker": "X", "action": "BUY_STOCK",
            "analytics": {"features": {"vix": 14}},
            "snapshot": {"spy_trend": "bullish", "vix": 14},
        }
        # Without macro panel the agent still functions on VIX+SPY
        v = agent_macro(ctx)
        assert v.agent == "macro"
        assert v.stance in ("buy", "sell", "hold", "abstain")


# ── EDGAR → microstructure agent ───────────────────────────────────────


def _seed_form4(ticker, n, *, days_ago=10):
    from backend.db import session_scope
    from backend.models.edgar_filing import EdgarFiling
    with session_scope() as s:
        for i in range(n):
            f = EdgarFiling(
                cik="0000000001", ticker=ticker.upper(),
                accession_number=f"acc-{ticker}-{i}-{datetime.utcnow().timestamp()}",
                form="4",
                filed_at=datetime.utcnow() - timedelta(days=days_ago),
            )
            s.add(f)


class TestMicrostructureWithEdgar:
    def test_high_insider_activity_damps_confidence(self, temp_db):
        ctx_base = {
            "ticker": "NVDA", "action": "BUY_STOCK", "strategy": "x",
            "analytics": {"features": {"volume_ratio": 1.4,
                                            "flow_bullishness": 0.4}},
            "snapshot": {"volume": 1_400_000, "avg_volume": 1_000_000},
        }
        # Baseline confidence (no insider data)
        baseline = agent_microstructure(ctx_base)
        # With 6 recent Form-4s the dampener should kick in.
        _seed_form4("NVDA", 6, days_ago=5)
        with_insider = agent_microstructure(ctx_base)
        assert with_insider.confidence <= baseline.confidence
        assert "insider" in with_insider.reasoning.lower()


# ── FRED + breadth → MarketState ────────────────────────────────────────


class TestMarketStateMacroBreadth:
    def test_state_carries_macro_when_supplied(self):
        state = build_market_state(
            regime={"trend": "bullish"},
            macro=_macro_panel(spread=0.5, hy=2.7),
        )
        assert state.macro.get("spread_10y_2y") == 0.5
        assert state.macro.get("BAMLH0A0HYM2", {}).get("value") == 2.7

    def test_breadth_verdict_lands_on_state(self):
        breadth = {
            "verdict": "healthy_advance",
            "pct_above_50dma": 0.70, "pct_above_200dma": 0.65,
        }
        state = build_market_state(regime={"trend": "bullish"},
                                          breadth=breadth)
        assert state.breadth_verdict == "healthy_advance"
        assert state.breadth_pct_above_50dma == 0.70
        # Cross-asset breadth field collapses to "broad" when healthy_advance.
        assert state.breadth == "broad"

    def test_broken_breadth_marks_narrow(self):
        state = build_market_state(
            regime={"trend": "bullish"},
            breadth={"verdict": "narrow_rally_fragile"},
        )
        assert state.breadth_verdict == "narrow_rally_fragile"
        assert state.breadth == "narrow"

    def test_auto_pull_when_omitted(self, temp_db):
        # No macro/breadth args → builder auto-pulls (empty cache → empty dicts)
        state = build_market_state(regime={"trend": "bullish"})
        assert isinstance(state.macro, dict)
        assert state.breadth_verdict in ("unknown", "")
