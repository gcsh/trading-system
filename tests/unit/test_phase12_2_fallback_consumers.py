"""MITS Phase 12.2 — verify every consumer reads through the
hierarchical fallback when the local cell is thin.

Strategy: monkey-patch ``get_posterior_with_fallback`` so we can assert
that each consumer module calls it for the right (ticker, pattern,
regime, vol_state). The DB calls inside the wider routes are bypassed
where needed via separate fixtures that set up a thin local cell.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, Optional

import pytest


def _stub_fallback_factory(calls):
    """Return a stub get_posterior_with_fallback that records calls and
    returns a synthetic parent posterior (N=120, posterior=0.62)."""
    def _stub(ticker, pattern, regime="unknown", vol_state="normal",
             time_bucket="rth", horizon="5d", sample_split="combined"):
        calls.append({
            "ticker": ticker, "pattern": pattern, "regime": regime,
            "vol_state": vol_state, "horizon": horizon,
        })
        return {
            "ticker": ticker, "pattern": pattern,
            "regime": regime, "vol_state": vol_state,
            "horizon": horizon, "sample_split": sample_split,
            "n": 120, "win_rate": 0.61, "posterior": 0.62,
            "avg_return_pct": 0.014, "confidence_level": "high",
            "source": "pattern_regime",
        }
    return _stub


def test_eod_cohort_lookup_uses_fallback(monkeypatch):
    """eod_analysis._cohort_lookup must call the fallback for every
    pattern. With a stubbed fallback the function should return
    populated cohort entries (the direct DB query is bypassed)."""
    calls: list = []
    from backend.bot import eod_analysis
    monkeypatch.setattr(eod_analysis, "get_posterior_with_fallback",
                        _stub_fallback_factory(calls))
    # Avoid DB hit for disabled patterns.
    monkeypatch.setattr(eod_analysis, "disabled_patterns",
                        lambda: set())
    out = eod_analysis._cohort_lookup(
        "AAPL", ["bull_flag", "vwap_rejection"],
        regime="trending_up", vol_state="normal",
    )
    assert len(out) == 2
    assert out["bull_flag"]["sample_size"] == 120
    assert out["bull_flag"]["posterior_win_rate"] == 0.62
    assert out["bull_flag"]["cohort_source"] == "pattern_regime"
    assert len(calls) == 2
    assert calls[0]["regime"] == "trending_up"


def test_eod_cohort_skips_thin_parent(monkeypatch):
    """When fallback returns N below EOD_COHORT_MIN_SAMPLES, the
    cohort entry is dropped — no setup is ranked for it."""
    from backend.bot import eod_analysis

    def _thin(ticker, pattern, **kw):
        return {
            "ticker": ticker, "pattern": pattern,
            "n": 3, "posterior": 0.55, "win_rate": 0.50,
            "avg_return_pct": 0.0, "confidence_level": "thin",
            "source": "local_thin", "regime": "unknown",
            "vol_state": "normal", "horizon": "5d",
        }
    monkeypatch.setattr(eod_analysis, "get_posterior_with_fallback", _thin)
    monkeypatch.setattr(eod_analysis, "disabled_patterns", lambda: set())
    out = eod_analysis._cohort_lookup("AAPL", ["bull_flag"])
    assert out == {}


def test_eod_cohort_respects_disabled(monkeypatch):
    """Disabled patterns are skipped before the fallback is called."""
    from backend.bot import eod_analysis
    calls = []
    monkeypatch.setattr(eod_analysis, "get_posterior_with_fallback",
                        _stub_fallback_factory(calls))
    monkeypatch.setattr(eod_analysis, "disabled_patterns",
                        lambda: {"bull_flag"})
    out = eod_analysis._cohort_lookup(
        "AAPL", ["bull_flag", "vwap_rejection"],
    )
    assert "bull_flag" not in out
    assert "vwap_rejection" in out
    assert all(c["pattern"] != "bull_flag" for c in calls)


def test_fallback_stats_record_source():
    """The in-process counter must increment for every recorded
    source so the diagnostics endpoint reflects real usage."""
    from backend.bot.corpus import knowledge_graph as kg
    kg.reset_fallback_stats()
    kg._record_source("cell")
    kg._record_source("cell")
    kg._record_source("pattern_regime")
    kg._record_source("none")
    snap = kg.get_fallback_stats()
    assert snap["calls"] == 4
    assert snap["cell"] == 2
    assert snap["pattern_regime"] == 1
    assert snap["none"] == 1
    # fallback_rate = 1 - cell/calls = 1 - 2/4 = 0.5
    assert snap["fallback_rate"] == 0.5
    kg.reset_fallback_stats()


def test_fallback_stats_clean_reset():
    """reset_fallback_stats zeros counters; get_fallback_stats is
    pure-read (no side effect on calls counter)."""
    from backend.bot.corpus import knowledge_graph as kg
    kg.reset_fallback_stats()
    snap = kg.get_fallback_stats()
    assert snap["calls"] == 0
    assert snap["cell"] == 0


def test_baseline_dynamic_compute(monkeypatch):
    """Phase 12.2 — compute_baselines reads per-direction WR from
    market_observations + market_outcomes via SQL."""
    from backend.api.routes import detector_scorecard as ds
    # We can't easily mock session_scope; just assert the function
    # returns the expected shape with floor values when DB has no
    # tables (the corpus is real on EC2; locally we just smoke test).
    bl = ds._compute_baselines()
    assert set(bl.keys()) >= {"long", "short", "neutral", "null"}
    for d in ("long", "short", "neutral", "null"):
        assert 0.0 <= bl[d] <= 1.0


def test_baseline_cache_ttl(monkeypatch):
    """get_baselines returns the cached value within TTL and refreshes
    when force_refresh=True is passed."""
    from backend.api.routes import detector_scorecard as ds
    ds._BASELINES_CACHE["value"] = {"long": 0.55, "short": 0.45,
                                     "neutral": 0.50, "null": 0.50}
    import time as _t
    ds._BASELINES_CACHE["ts"] = _t.time()
    bl = ds.get_baselines()
    assert bl["long"] == 0.55
    assert bl["short"] == 0.45
    # Force refresh recomputes.
    refreshed = ds.get_baselines(force_refresh=True)
    assert "long" in refreshed


def test_baseline_for_direction():
    """_baseline_for picks the right key with safe fallback."""
    from backend.api.routes import detector_scorecard as ds
    bl = {"long": 0.55, "short": 0.45, "neutral": 0.50, "null": 0.52}
    assert ds._baseline_for("long", bl) == 0.55
    assert ds._baseline_for("short", bl) == 0.45
    assert ds._baseline_for(None, bl) == 0.52
    assert ds._baseline_for("missing", bl) == 0.52


def test_pattern_direction_map_includes_static():
    """The pattern→direction map includes the STATIC_DIRECTION entries
    (the empirical fallback is best-effort)."""
    from backend.api.routes import detector_scorecard as ds
    m = ds._pattern_direction_map()
    # Wyckoff distribution is short by spec.
    assert m.get("wyckoff_distribution_phase") == "short"
    # Bull flag is long.
    assert m.get("bull_flag") == "long"
