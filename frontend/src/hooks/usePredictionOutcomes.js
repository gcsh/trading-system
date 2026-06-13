/**
 * MITS Phase 5 (P5.6) — module-cached prediction-outcomes data hook.
 *
 * Mirrors the useKnowledge cache pattern: 60s TTL by date, with an
 * accuracy aggregate fetched alongside.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

const TTL_MS = 60_000;

const _rowsCache = new Map();      // dateStr -> { fetchedAt, rows, count }
const _rowsInflight = new Map();   // dateStr -> Promise

const _accuracyCache = new Map();  // windowStr -> { fetchedAt, body }
const _accuracyInflight = new Map();

function _key(d) {
  return d || 'all';
}

async function _fetchRows(date) {
  const k = _key(date);
  const hit = _rowsCache.get(k);
  if (hit && Date.now() - hit.fetchedAt < TTL_MS) return hit;
  if (_rowsInflight.has(k)) return _rowsInflight.get(k);
  const url = date
    ? `/prediction-outcomes?date=${encodeURIComponent(date)}&limit=100`
    : '/prediction-outcomes?limit=100';
  const p = fetch(url)
    .then((r) => (r.ok ? r.json() : { rows: [], count: 0 }))
    .then((body) => {
      const entry = {
        fetchedAt: Date.now(),
        rows: Array.isArray(body.rows) ? body.rows : [],
        count: body.count || 0,
      };
      _rowsCache.set(k, entry);
      _rowsInflight.delete(k);
      return entry;
    })
    .catch(() => {
      _rowsInflight.delete(k);
      const entry = { fetchedAt: Date.now(), rows: [], count: 0 };
      _rowsCache.set(k, entry);
      return entry;
    });
  _rowsInflight.set(k, p);
  return p;
}

async function _fetchAccuracy(windowStr) {
  const k = windowStr || '30';
  const hit = _accuracyCache.get(k);
  if (hit && Date.now() - hit.fetchedAt < TTL_MS) return hit;
  if (_accuracyInflight.has(k)) return _accuracyInflight.get(k);
  const p = fetch(`/prediction-outcomes/accuracy?window=${encodeURIComponent(k)}`)
    .then((r) => (r.ok ? r.json() : {}))
    .then((body) => {
      const entry = { fetchedAt: Date.now(), body };
      _accuracyCache.set(k, entry);
      _accuracyInflight.delete(k);
      return entry;
    })
    .catch(() => {
      _accuracyInflight.delete(k);
      const entry = { fetchedAt: Date.now(), body: {} };
      _accuracyCache.set(k, entry);
      return entry;
    });
  _accuracyInflight.set(k, p);
  return p;
}

/**
 * useTomorrowSetups — kept distinct from useEvidence so a single page can
 * reuse the tomorrow-setup hook for the EOD analysis rows.
 */
export function usePredictionOutcomes(date) {
  const k = _key(date);
  const initial = _rowsCache.get(k);
  const [rows, setRows] = useState(initial ? initial.rows : []);
  const [count, setCount] = useState(initial ? initial.count : 0);
  const [loading, setLoading] = useState(!initial);
  const refKey = useRef(k);
  refKey.current = k;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    _fetchRows(date).then((entry) => {
      if (!alive) return;
      setRows(entry.rows);
      setCount(entry.count);
      setLoading(false);
    });
    return () => { alive = false; };
  }, [k]);  // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = useMemo(() => () => {
    _rowsCache.delete(refKey.current);
    _fetchRows(date).then((entry) => {
      setRows(entry.rows);
      setCount(entry.count);
    });
  }, [date]);

  return { rows, count, loading, refresh };
}

export function usePredictionAccuracy(windowStr = '30') {
  const initial = _accuracyCache.get(windowStr);
  const [body, setBody] = useState(initial ? initial.body : null);
  const [loading, setLoading] = useState(!initial);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    _fetchAccuracy(windowStr).then((entry) => {
      if (!alive) return;
      setBody(entry.body);
      setLoading(false);
    });
    return () => { alive = false; };
  }, [windowStr]);

  return { body, loading };
}

export function clearPredictionOutcomesCache() {
  _rowsCache.clear();
  _rowsInflight.clear();
  _accuracyCache.clear();
  _accuracyInflight.clear();
}
