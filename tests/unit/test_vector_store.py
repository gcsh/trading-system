"""MITS Phase 8.5 — vector_store shape + namespace + ordering tests.

We do NOT spin up a real Postgres. Instead we monkey-patch the embed +
pgvector handles to deterministic stubs so the data-flow logic is
covered without external deps.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from backend.bot.ai import vector_store


class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._last: List[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        sql = sql.strip().lower()
        if sql.startswith("create extension") or sql.startswith("create table"):
            return
        if sql.startswith("create index"):
            return
        if sql.startswith("insert into vector_entries"):
            ns, key, vec_lit, meta_json = params
            self._store.setdefault(ns, {})[key] = (vec_lit, meta_json)
            return
        if sql.startswith("select key, metadata"):
            qv, ns, qv2, k = params
            rows = []
            for key, (vec, meta) in self._store.get(ns, {}).items():
                # Treat cosine as 1.0 for matching key, 0.5 otherwise — just
                # need deterministic ordering for the test.
                rows.append((key, meta, 1.0 if vec == qv else 0.5))
            rows.sort(key=lambda r: -r[2])
            self._last = rows[: int(k)]
            return
        if sql.startswith("select namespace, count"):
            agg = {}
            for ns, entries in self._store.items():
                agg[ns] = (ns, len(entries), None)
            self._last = list(agg.values())
            return

    def fetchall(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self):
        self.store: Dict[str, Dict[str, Any]] = {}
        self.autocommit = True

    def cursor(self):
        return _FakeCursor(self.store)

    def close(self):  # pragma: no cover
        pass


@pytest.fixture()
def fake_pg(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(vector_store, "_conn", conn)
    # Forces lazy loaders to no-op when re-imported.
    monkeypatch.setattr(vector_store, "_conn_handle", lambda: conn)
    # Deterministic embed: map text → length-384 list with text hash as first dim.
    def _embed(text):
        h = float(hash(text) % 1000) / 1000.0
        return [h] + [0.0] * (vector_store.TUNABLES.vector_dim - 1)
    monkeypatch.setattr(vector_store, "embed", _embed)
    return conn


def test_upsert_and_search_round_trip(fake_pg):
    ok = vector_store.upsert("regime_snapshots", "day_a",
                                  vector_store.embed("panic 2020-03-12 vix=80"),
                                  {"date": "2020-03-12", "regime": "panic"})
    assert ok
    qv = vector_store.embed("panic 2020-03-12 vix=80")
    hits = vector_store.similarity_search("regime_snapshots", qv, k=5, min_cosine=0.6)
    assert len(hits) == 1
    assert hits[0].key == "day_a"
    assert hits[0].cosine >= 0.9


def test_similarity_search_respects_min_cosine(fake_pg):
    vector_store.upsert("market_observations", "obs-1",
                            vector_store.embed("text_a"), {"foo": "bar"})
    vector_store.upsert("market_observations", "obs-2",
                            vector_store.embed("text_b"), {"foo": "baz"})
    qv = vector_store.embed("text_c")
    hits = vector_store.similarity_search(
        "market_observations", qv, k=5, min_cosine=0.99,
    )
    assert hits == []   # nothing at 1.0 cosine
    hits2 = vector_store.similarity_search(
        "market_observations", qv, k=5, min_cosine=0.1,
    )
    assert len(hits2) == 2


def test_namespace_stats(fake_pg):
    vector_store.upsert("eod_theses", "k1",
                            vector_store.embed("t1"), {"x": 1})
    vector_store.upsert("eod_theses", "k2",
                            vector_store.embed("t2"), {"x": 2})
    vector_store.upsert("closed_trades", "k3",
                            vector_store.embed("t3"), {"x": 3})
    stats = vector_store.namespace_stats()
    assert stats["eod_theses"]["count"] == 2
    assert stats["closed_trades"]["count"] == 1


def test_index_regime_snapshot_writes(fake_pg):
    ok = vector_store.index_regime_snapshot(
        key="regime:42",
        regime_state="panic", spy_30m=-1.5, vix_level=30.0,
        breadth=0.2, put_call=1.3, sector_dispersion=0.05,
        top_flow_summary="QQQ 1DTE long puts +sweep", date_iso="2026-03-12",
    )
    assert ok
    hits = vector_store.similarity_search(
        "regime_snapshots",
        vector_store.embed(
            "date=2026-03-12 regime=panic spy_30m=-1.5 vix=30.0 "
            "breadth=0.2 put_call=1.3 sector_dispersion=0.05 "
            "flow=QQQ 1DTE long puts +sweep"
        ),
        k=5, min_cosine=0.5,
    )
    assert any(h.key == "regime:42" for h in hits)
