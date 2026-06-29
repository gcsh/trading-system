/**
 * MITS Phase 7.6 — intraday regime polling hook.
 *
 * Polls `GET /regime/intraday` every `pollMs` (default 30 s) and
 * exposes the latest state to the RegimeBanner. Module-cached so
 * multiple banner instances on the same page share a single fetch.
 */
import { useEffect, useState } from 'react';

let _cache = null;
let _cacheAt = 0;
const _subscribers = new Set();
const DEFAULT_TTL_MS = 30_000;

async function _fetchOnce() {
  try {
    const r = await fetch('/regime/intraday');
    if (!r.ok) return null;
    const body = await r.json();
    _cache = body;
    _cacheAt = Date.now();
    _subscribers.forEach((cb) => cb(_cache));
    return body;
  } catch (_e) {
    return null;
  }
}

function _visible() {
  if (typeof document === 'undefined') return true;
  return document.visibilityState !== 'hidden';
}

export function useIntradayRegime(pollMs = DEFAULT_TTL_MS) {
  const [state, setState] = useState(_cache);

  useEffect(() => {
    let mounted = true;
    let id = null;
    const cb = (data) => { if (mounted) setState(data); };
    _subscribers.add(cb);

    // Always fetch on mount, even if cache is warm — the operator
    // expects fresh data on page load.
    _fetchOnce().then((data) => { if (mounted && data) setState(data); });

    // Perf-Fix Pass — visibility gate. The 30s poll burned a request
    // every cycle even when the tab was backgrounded; now it pauses
    // and fires one immediate fetch on tab-focus.
    const start = () => {
      if (id != null) return;
      id = setInterval(() => { if (_visible()) _fetchOnce(); }, pollMs);
    };
    const stop = () => { if (id != null) { clearInterval(id); id = null; } };
    const onVis = () => {
      if (_visible()) { _fetchOnce(); start(); } else { stop(); }
    };
    if (_visible()) start();
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis);
    }

    return () => {
      mounted = false;
      _subscribers.delete(cb);
      stop();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVis);
      }
    };
  }, [pollMs]);

  return state;
}

export function clearIntradayRegimeCache() {
  _cache = null;
  _cacheAt = 0;
}
