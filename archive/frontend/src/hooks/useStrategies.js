/**
 * Single source of truth for the strategy catalog in the UI. Reads from
 * `/strategies/catalog` once per session, caches in module state so every
 * caller (Strategies page, Theory Studio, StrategySelector, StrategyCompare,
 * CuratedRules) gets the same list without re-fetching.
 *
 * Adding a new strategy: register it in `STRATEGY_REGISTRY` on the backend
 * and add metadata to `_STRATEGY_META` in routes/strategies.py. The UI
 * picks it up on next reload — no frontend code change required.
 */
import { useEffect, useState } from 'react';

let _cache = null;
let _inflight = null;
const _subscribers = new Set();

async function _fetch() {
  if (_cache) return _cache;
  if (_inflight) return _inflight;
  _inflight = fetch('/strategies/catalog')
    .then((r) => (r.ok ? r.json() : []))
    .then((rows) => {
      _cache = Array.isArray(rows) ? rows : [];
      _inflight = null;
      _subscribers.forEach((cb) => cb(_cache));
      return _cache;
    })
    .catch(() => {
      _inflight = null;
      _cache = [];
      _subscribers.forEach((cb) => cb(_cache));
      return _cache;
    });
  return _inflight;
}

/**
 * Hook returning the strategy catalog as
 *   [{slug, label, description, category}, ...]
 * Returns [] while loading; safe to map directly.
 */
export function useStrategies() {
  const [list, setList] = useState(_cache || []);
  useEffect(() => {
    let mounted = true;
    const onUpdate = (rows) => { if (mounted) setList(rows); };
    _subscribers.add(onUpdate);
    if (_cache) {
      setList(_cache);
    } else {
      _fetch().then(onUpdate);
    }
    return () => { mounted = false; _subscribers.delete(onUpdate); };
  }, []);
  return list;
}

/**
 * Sync helper for places that just need the slug list (no labels).
 * Returns the cached list immediately, or [] if not yet fetched.
 */
export function strategiesSync() {
  return _cache || [];
}
