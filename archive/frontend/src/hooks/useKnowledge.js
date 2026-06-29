/**
 * MITS Phase 0 — knowledge-graph data hook.
 *
 * Module-cached fetch of `/knowledge/cells` and `/knowledge/corpus/status`.
 * Mirrors the `useStrategies` pattern: first caller triggers the fetch,
 * subsequent callers reuse the in-flight promise / cache.
 *
 * `useKnowledgeCells(filters)` re-fetches when filters change. Filters
 * are passed as a flat object — only truthy values get appended to the
 * query string.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

const _cellCache = new Map();        // key -> rows
const _cellInflight = new Map();     // key -> Promise

function _key(filters) {
  if (!filters) return 'all';
  const parts = [];
  for (const k of Object.keys(filters).sort()) {
    const v = filters[k];
    if (v === undefined || v === null || v === '') continue;
    parts.push(`${k}=${encodeURIComponent(String(v))}`);
  }
  return parts.join('&') || 'all';
}

async function _fetchCells(filters) {
  const key = _key(filters);
  if (_cellCache.has(key)) return _cellCache.get(key);
  if (_cellInflight.has(key)) return _cellInflight.get(key);
  const qs = key === 'all' ? '' : `?${key}`;
  const promise = fetch(`/knowledge/cells${qs}`)
    .then((r) => (r.ok ? r.json() : []))
    .then((rows) => {
      _cellCache.set(key, Array.isArray(rows) ? rows : []);
      _cellInflight.delete(key);
      return _cellCache.get(key);
    })
    .catch(() => {
      _cellInflight.delete(key);
      _cellCache.set(key, []);
      return [];
    });
  _cellInflight.set(key, promise);
  return promise;
}

/**
 * Hook returning the filtered knowledge-graph cells. Returns `{ rows,
 * loading, refresh }`. `filters` shape:
 *   { ticker, pattern, regime, vol_state, time_bucket, horizon, min_samples }
 */
export function useKnowledgeCells(filters) {
  const key = _key(filters);
  const [rows, setRows] = useState(_cellCache.get(key) || []);
  const [loading, setLoading] = useState(!_cellCache.has(key));
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  useEffect(() => {
    let mounted = true;
    if (_cellCache.has(key)) {
      setRows(_cellCache.get(key));
      setLoading(false);
      return () => { mounted = false; };
    }
    setLoading(true);
    _fetchCells(filters).then((data) => {
      if (mounted) {
        setRows(data);
        setLoading(false);
      }
    });
    return () => { mounted = false; };
  }, [key]);  // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = useMemo(() => () => {
    _cellCache.delete(_key(filtersRef.current));
    _fetchCells(filtersRef.current).then(setRows);
  }, []);

  return { rows, loading, refresh };
}


let _statusCache = null;
let _statusInflight = null;

async function _fetchStatus() {
  if (_statusCache) return _statusCache;
  if (_statusInflight) return _statusInflight;
  _statusInflight = fetch('/knowledge/corpus/status')
    .then((r) => (r.ok ? r.json() : []))
    .then((rows) => {
      _statusCache = Array.isArray(rows) ? rows : [];
      _statusInflight = null;
      return _statusCache;
    })
    .catch(() => {
      _statusInflight = null;
      _statusCache = [];
      return _statusCache;
    });
  return _statusInflight;
}

function _visible() {
  if (typeof document === 'undefined') return true;
  return document.visibilityState !== 'hidden';
}

export function useCorpusStatus(pollMs = 0) {
  const [rows, setRows] = useState(_statusCache || []);
  useEffect(() => {
    let mounted = true;
    let id = null;
    const load = () => _fetchStatus().then((data) => { if (mounted) setRows(data); });
    load();
    if (pollMs > 0) {
      // Perf-Fix Pass — visibility-gate the poll. The corpus-status
      // route hits S3 + Postgres on the engine box; backgrounded tabs
      // hammering it for nothing was pure waste.
      const tick = () => {
        if (!_visible()) return;
        _statusCache = null;        // force re-fetch
        load();
      };
      const start = () => { if (id == null) id = setInterval(tick, pollMs); };
      const stop = () => { if (id != null) { clearInterval(id); id = null; } };
      const onVis = () => {
        if (_visible()) { tick(); start(); } else { stop(); }
      };
      if (_visible()) start();
      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', onVis);
      }
      return () => {
        mounted = false;
        stop();
        if (typeof document !== 'undefined') {
          document.removeEventListener('visibilitychange', onVis);
        }
      };
    }
    return () => { mounted = false; };
  }, [pollMs]);
  return rows;
}

export function clearKnowledgeCache() {
  _cellCache.clear();
  _cellInflight.clear();
  _statusCache = null;
}


/**
 * MITS Phase 2 (P2.5) — module-cached evidence lookup.
 *
 * Used by `EvidencePanel` to avoid per-mount network calls. Multiple
 * mounts of EvidencePanel for the same `ticker` share one underlying
 * `/knowledge/cells?ticker=...` fetch.
 *
 *   useEvidence(ticker, pattern, horizon, topN)
 *     - returns { cells, primary, loading } where:
 *         - `cells`    is the filtered subset for (ticker, [pattern,]
 *                      horizon) from the module cache.
 *         - `primary`  is the most-populated cell in `cells`, or null.
 *         - `loading`  is true while the first fetch resolves.
 *
 * When `pattern` is omitted, returns the top-N cells across all
 * patterns for the ticker (sorted by sample_size desc), matching the
 * legacy EvidencePanel "no-pattern" mode shape.
 *
 * Two callers on the same page with overlapping filters share ONE
 * network call — the operator can verify in DevTools that mounting
 * multiple EvidencePanels for the same ticker produces a single
 * `/knowledge/cells?ticker=...` request.
 */
export function useEvidence(ticker, pattern, horizon = '1d', topN = 3) {
  const filters = useMemo(() => {
    if (!ticker) return null;
    // We deliberately only key on `ticker` + min_samples + a high
    // `limit`. The pattern / horizon filtering happens client-side
    // against the same cached row set so two panels with different
    // pattern hints share one fetch.
    return { ticker, min_samples: 5, limit: 50 };
  }, [ticker]);

  const { rows, loading } = useKnowledgeCells(filters);

  return useMemo(() => {
    if (!ticker) {
      return { cells: [], primary: null, loading: false };
    }
    const arr = Array.isArray(rows) ? rows : [];
    let filtered = arr;
    if (pattern) {
      filtered = arr.filter((c) => c.pattern === pattern);
    }
    // Prefer the requested horizon when available.
    const sameHorizon = filtered.filter((c) => c.horizon === horizon);
    const ranked = (sameHorizon.length ? sameHorizon : filtered)
      .slice()
      .sort((a, b) => (b.sample_size || 0) - (a.sample_size || 0));
    // MITS Phase 6 — when a pattern is requested, return up to 4 cells
    // so EvidencePanel can render the live / historical / combined
    // source breakdown without re-fetching. Primary is still the
    // top-sample row.
    const cells = pattern ? ranked.slice(0, 4) : ranked.slice(0, Math.max(1, topN));
    const primary = cells.length ? cells[0] : null;
    return { cells, primary, loading };
  }, [ticker, pattern, horizon, topN, rows, loading]);
}
