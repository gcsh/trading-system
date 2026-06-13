"""MITS Phase 15.B — HistoricalAnalogRetrieval primitive.

The pgvector layer + outcome tables are stubbed via monkeypatch so the
tests are hermetic. Each test asserts a single property of the
primitive: distribution math, empty-cluster contract, two-pass fallback
ordering + dedup.

Note on monkeypatch targets: ``embed`` and ``similarity_search`` are
imported lazily inside ``retrieve_analogs`` so we patch them on the
``backend.bot.ai.vector_store`` module (where lookup happens at call
time). ``_outcomes_for_hits`` is a module-level helper in
``analog_retrieval`` so we patch it there directly.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.bot.corpus.analog_retrieval import (
    AnalogCluster,
    AnalogHit,
    retrieve_analogs,
)


def _make_regime_vector(*, trend: str = "bullish",
                        vol_state: str = "normal"):
    """Build a minimal RegimeVector-shaped object. ``retrieve_analogs``
    only reads ``.trend.value`` and ``.volatility_state.value`` so a
    pair of ``SimpleNamespace`` shells is enough."""
    return SimpleNamespace(
        trend=SimpleNamespace(value=trend),
        volatility_state=SimpleNamespace(value=vol_state),
    )


def _make_hit(*, key: str, cosine: float, date: str,
              regime: str = "bullish") -> SimpleNamespace:
    return SimpleNamespace(
        namespace="regime_snapshot_v2",
        key=key, cosine=cosine,
        metadata={"date": date, "regime": regime},
    )


# ── tests ──────────────────────────────────────────────────────────────


def test_retrieve_analogs_distribution_math(monkeypatch):
    """Three known returns feed into ``outcome_distribution`` — mean,
    min, max, p50 land on the expected values; ``to_dict`` round-trips
    the schema."""
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar

    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    hits = [
        _make_hit(key=f"k{i}", cosine=0.9 - 0.01 * i,
                  date=f"2025-01-{i+1:02d}")
        for i in range(3)
    ]
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: hits)

    def _stub_outcomes(hits, *, ticker, horizon):
        if ticker != "AAPL":
            return []
        return [
            AnalogHit(
                observation_id=100 + i,
                ticker="AAPL",
                timestamp=datetime(2025, 1, i + 1),
                distance=1.0 - (0.9 - 0.01 * i),
                cosine=0.9 - 0.01 * i,
                regime_label="bullish",
                pattern_set=["bull_flag"],
                realized_return_pct=pct,
                horizon=horizon,
            )
            for i, pct in enumerate([1.0, 2.0, 3.0])
        ]

    monkeypatch.setattr(ar, "_outcomes_for_hits", _stub_outcomes)

    rv = _make_regime_vector()
    cluster = retrieve_analogs(
        ticker="AAPL", regime_vector=rv, pattern="bull_flag",
        horizon="5d", k=50, sector_fallback=True,
    )

    assert isinstance(cluster, AnalogCluster)
    assert cluster.cohort_size == 3
    assert cluster.outcome_distribution["mean"] == pytest.approx(2.0)
    assert cluster.outcome_distribution["min"] == 1.0
    assert cluster.outcome_distribution["max"] == 3.0
    assert cluster.outcome_distribution["p50"] == pytest.approx(2.0)
    # 3 outcomes < 10 → sector fallback is allowed to run, but the
    # any-ticker stub returns [] so the flag stays False.
    assert cluster.sector_fallback_used is False
    d = cluster.to_dict()
    assert d["cohort_size"] == 3
    assert d["query_state"]["horizon"] == "5d"
    assert d["analogs"][0]["realized_return_pct"] == 1.0


def test_retrieve_analogs_empty_when_no_hits(monkeypatch):
    """Zero hits from pgvector → empty cluster with documented shape."""
    import backend.bot.ai.vector_store as vs

    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    rv = _make_regime_vector()
    cluster = retrieve_analogs(
        ticker="AAPL", regime_vector=rv, pattern="bull_flag",
        horizon="1d", k=50,
    )
    assert cluster.cohort_size == 0
    assert cluster.outcome_distribution == {}
    assert cluster.sector_fallback_used is False
    assert cluster.analogs == []
    assert cluster.query_state["ticker"] == "AAPL"
    assert cluster.query_state["pattern"] == "bull_flag"


def test_retrieve_analogs_empty_when_embed_fails(monkeypatch):
    """Embed returns [] → empty cluster, no SQL hit attempted."""
    import backend.bot.ai.vector_store as vs

    monkeypatch.setattr(vs, "embed", lambda text: [])
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(vs, "similarity_search", _spy)
    rv = _make_regime_vector()
    cluster = retrieve_analogs(
        ticker="AAPL", regime_vector=rv, pattern="x", horizon="1d", k=50,
    )
    assert cluster.cohort_size == 0
    assert called["n"] == 0


def test_two_pass_fallback_orders_same_ticker_first_and_dedupes(monkeypatch):
    """Same-ticker pass returns 5; any-ticker pass returns 30 more.
    Result: same-ticker rows ordered first, total ≤ k, fallback flag
    fires."""
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar

    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    hits = [_make_hit(key=f"k{i}", cosine=0.9, date=f"2025-02-{i+1:02d}")
            for i in range(5)]
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: hits)

    same_ticker_rows = [
        AnalogHit(
            observation_id=i,
            ticker="AAPL",
            timestamp=datetime(2025, 2, 1),
            distance=0.1, cosine=0.9,
            regime_label="bullish",
            pattern_set=["bull_flag"],
            realized_return_pct=float(i),
            horizon="1d",
        ) for i in range(1, 6)
    ]
    any_ticker_rows = same_ticker_rows + [
        AnalogHit(
            observation_id=100 + i,
            ticker="MSFT",
            timestamp=datetime(2025, 2, 1),
            distance=0.1, cosine=0.9,
            regime_label="bullish",
            pattern_set=["bull_flag"],
            realized_return_pct=float(i + 10),
            horizon="1d",
        ) for i in range(30)
    ]

    def _stub(hits, *, ticker, horizon):
        return same_ticker_rows if ticker == "AAPL" else any_ticker_rows

    monkeypatch.setattr(ar, "_outcomes_for_hits", _stub)

    rv = _make_regime_vector()
    cluster = retrieve_analogs(
        ticker="AAPL", regime_vector=rv, pattern="bull_flag",
        horizon="1d", k=20, sector_fallback=True,
    )

    assert cluster.sector_fallback_used is True
    assert cluster.cohort_size == 20  # truncated to k
    # same-ticker rows ordered first.
    for i in range(5):
        assert cluster.analogs[i].ticker == "AAPL"
        assert cluster.analogs[i].observation_id == i + 1
    # any-ticker rows follow, deduped against same-ticker.
    for i in range(5, 20):
        assert cluster.analogs[i].ticker == "MSFT"


def test_sector_fallback_disabled(monkeypatch):
    """sector_fallback=False stops at same-ticker pass even when < 10."""
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar

    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [
                            _make_hit(key="k1", cosine=0.9,
                                      date="2025-03-01"),
                        ])

    same_ticker_rows = [
        AnalogHit(
            observation_id=1, ticker="AAPL",
            timestamp=datetime(2025, 3, 1),
            distance=0.1, cosine=0.9,
            regime_label="bullish",
            pattern_set=["bull_flag"],
            realized_return_pct=2.0, horizon="1d",
        ),
    ]

    calls = {"any_ticker": 0}

    def _stub(hits, *, ticker, horizon):
        if ticker is None:
            calls["any_ticker"] += 1
        return same_ticker_rows if ticker == "AAPL" else []

    monkeypatch.setattr(ar, "_outcomes_for_hits", _stub)

    rv = _make_regime_vector()
    cluster = retrieve_analogs(
        ticker="AAPL", regime_vector=rv, pattern="bull_flag",
        horizon="1d", k=20, sector_fallback=False,
    )

    assert calls["any_ticker"] == 0
    assert cluster.sector_fallback_used is False
    assert cluster.cohort_size == 1


def test_invalid_horizon_raises():
    rv = _make_regime_vector()
    with pytest.raises(ValueError):
        retrieve_analogs(
            ticker="AAPL", regime_vector=rv, pattern="x",
            horizon="2h", k=10,
        )


def test_query_state_payload_shape(monkeypatch):
    """Query state mirrors the inputs verbatim; ticker is uppercased."""
    import backend.bot.ai.vector_store as vs

    monkeypatch.setattr(vs, "embed", lambda text: [])
    rv = _make_regime_vector(trend="bullish", vol_state="elevated")
    cluster = retrieve_analogs(
        ticker="aapl", regime_vector=rv, pattern="bull_flag",
        horizon="5d", k=42,
    )
    assert cluster.query_state == {
        "ticker": "AAPL",
        "regime": "bullish",
        "vol_state": "elevated",
        "pattern": "bull_flag",
        "horizon": "5d",
        "k": 42,
    }
