/**
 * MITS Phase 9.2 + Phase 10 — Theory Studio fetch hooks.
 *
 *   useTheory(theory, ticker, window, params)         — single theory.
 *   useTheoryMulti(ticker, theories[], window, ...)   — multi-overlay.
 *   useTheoryRegistry()                                — list all theories.
 *
 * Phase-10 additions:
 *
 *   - ``live`` flag → schedules a polling timer (30s market hours, 5min
 *     off-hours) and re-fetches the latest bars + annotation. Each
 *     re-fetch passes ``live=true`` to the backend so the response
 *     carries a server_ts the operator can see in the LIVE pill.
 *   - Multi-theory hook hits ``/theories/multi/{ticker}`` once instead
 *     of N parallel single requests, halving network round trips when
 *     three or more theories are selected.
 *
 * Perf-Fix Pass (2026-06-13):
 *
 *   The previous version of this file scheduled THREE independent
 *   setInterval timers — one per hook (useTheory, useTheoryMulti) plus
 *   useQuoteTick's own 1s timer. Each timer also re-fired while the
 *   browser tab was backgrounded, wasting backend cycles + bandwidth.
 *
 *   This rewrite collapses the *theory* refresh loop into a single
 *   module-level dispatcher. Subscribers register a tick handler; the
 *   dispatcher runs ONE setInterval at the longest needed cadence and
 *   broadcasts to all subscribers. Visibility changes pause/resume the
 *   dispatcher (document.visibilityState === 'hidden' → suspend; back
 *   to 'visible' → fire one immediate tick + resume the timer).
 *
 *   useQuoteTick keeps its own 1s timer (different cadence, different
 *   endpoint), but now also pauses while the tab is hidden.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

const CACHE = new Map(); // key -> { ts, payload }
const TTL_MS = 60_000;

function cacheKey(prefix, ticker, win, params) {
  return `${prefix}|${(ticker || '').toUpperCase()}|${win}|${JSON.stringify(params || {})}`;
}

function isMarketHours(d = new Date()) {
  // US equities: M-F 09:30–16:00 ET. Approximate via UTC offset (no DST
  // logic — close enough for "live tick faster" gating).
  const day = d.getUTCDay();
  if (day === 0 || day === 6) return false;
  const utcHour = d.getUTCHours();
  const utcMin = d.getUTCMinutes();
  // ET = UTC−4 (EDT) or −5 (EST); use −4 as the wider window.
  const etHour = (utcHour - 4 + 24) % 24;
  if (etHour > 9 && etHour < 16) return true;
  if (etHour === 9 && utcMin >= 30) return true;
  return false;
}

function livePollIntervalMs() {
  return isMarketHours() ? 30_000 : 5 * 60_000;
}

// ── Module-level theory dispatcher (Perf-Fix Pass) ────────────────────
//
// One timer to rule them all. Each useTheory/useTheoryMulti instance
// with `live=true` registers a fetcher; the dispatcher fires every
// `livePollIntervalMs()` and invokes every registered fetcher.
//
// Pauses while the tab is hidden. Wakes up + fires an immediate tick
// the moment the tab becomes visible again.

const _theorySubscribers = new Set();
let _theoryTimer = null;
let _visibilityHandlerInstalled = false;

function _isTabVisible() {
  if (typeof document === 'undefined') return true;
  return document.visibilityState !== 'hidden';
}

function _tickAll() {
  if (!_isTabVisible()) return;          // pause while hidden
  _theorySubscribers.forEach((fn) => {
    try { fn(); } catch (_e) { /* swallow per-subscriber errors */ }
  });
}

function _startTheoryTimer() {
  if (_theoryTimer != null) return;
  _theoryTimer = setInterval(_tickAll, livePollIntervalMs());
}

function _stopTheoryTimer() {
  if (_theoryTimer != null) {
    clearInterval(_theoryTimer);
    _theoryTimer = null;
  }
}

function _installVisibilityHandler() {
  if (_visibilityHandlerInstalled) return;
  if (typeof document === 'undefined') return;
  document.addEventListener('visibilitychange', () => {
    if (_isTabVisible()) {
      // Wake: refresh once immediately + restart timer cadence.
      _tickAll();
      _stopTheoryTimer();
      if (_theorySubscribers.size > 0) _startTheoryTimer();
    } else {
      // Hidden: stop the timer entirely. Subscribers stay registered.
      _stopTheoryTimer();
    }
  });
  _visibilityHandlerInstalled = true;
}

function _subscribeTheoryTick(fn) {
  _installVisibilityHandler();
  _theorySubscribers.add(fn);
  if (_theorySubscribers.size === 1 && _isTabVisible()) {
    _startTheoryTimer();
  }
  return () => {
    _theorySubscribers.delete(fn);
    if (_theorySubscribers.size === 0) _stopTheoryTimer();
  };
}

export function useTheoryRegistry() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let cancelled = false;
    fetch('/theories')
      .then((r) => r.json())
      .then((j) => { if (!cancelled) setData(j); })
      .catch((e) => { if (!cancelled) setErr(e.message); });
    return () => { cancelled = true; };
  }, []);
  return { registry: data, error: err };
}

export function useTheory(theory, ticker, window, params, refreshKey = 0, live = false) {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const lastReq = useRef(0);

  const fetcher = useCallback(async () => {
    if (!theory || !ticker || !window) return;
    const key = cacheKey(theory, ticker, window, params);
    const cached = CACHE.get(key);
    if (cached && Date.now() - cached.ts < TTL_MS && refreshKey === 0 && !live) {
      setPayload(cached.payload);
      return;
    }
    setLoading(true);
    setError(null);
    const reqId = ++lastReq.current;
    try {
      const q = new URLSearchParams({ window });
      if (live) q.set('live', 'true');
      if (params && Object.keys(params).length) {
        q.set('params', JSON.stringify(params));
      }
      const url = `/theories/${encodeURIComponent(theory)}/${encodeURIComponent(ticker)}?${q.toString()}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      if (reqId !== lastReq.current) return;  // ignore stale
      if (!live) CACHE.set(key, { ts: Date.now(), payload: j });
      setPayload(j);
    } catch (e) {
      if (reqId !== lastReq.current) return;
      setError(e.message);
    } finally {
      if (reqId === lastReq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theory, ticker, window, JSON.stringify(params), refreshKey, live]);

  useEffect(() => { fetcher(); }, [fetcher]);

  // Live polling — register with the shared dispatcher (one timer for
  // the whole app) instead of opening our own setInterval.
  useEffect(() => {
    if (!live) return undefined;
    return _subscribeTheoryTick(fetcher);
  }, [live, fetcher]);

  return { payload, loading, error, refresh: fetcher };
}


// ── Multi-theory hook ────────────────────────────────────────────────


// ── MITS Phase 10.1 — live tick hook ──────────────────────────────────
//
// Polls the dedicated /quote/{ticker} endpoint at 1s during market hours
// and 10s off-hours so the chart's forming candle updates visibly. The
// heavier multi-theory re-fetch stays at 30s — splitting the concerns
// keeps the page responsive.
//
// Perf-Fix Pass: visibility-gated. While the tab is hidden the 1s timer
// is paused entirely (it was burning ~60 requests/min in the background).


function liveTickIntervalMs() {
  return isMarketHours() ? 1_000 : 10_000;
}


export function useQuoteTick(ticker, enabled = false) {
  const [tick, setTick] = useState(null);   // {price, ts, source, age_seconds}
  const [error, setError] = useState(null);
  const lastReq = useRef(0);

  useEffect(() => {
    if (!enabled || !ticker) return undefined;
    let cancelled = false;
    let timer = null;

    const fetchOnce = async () => {
      if (!_isTabVisible()) return;       // pause while hidden
      const reqId = ++lastReq.current;
      try {
        const r = await fetch(`/quote/${encodeURIComponent(ticker)}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        if (cancelled || reqId !== lastReq.current) return;
        if (j && typeof j.price === 'number' && j.price > 0) {
          setTick(j);
          setError(null);
        }
      } catch (e) {
        if (cancelled) return;
        setError(e.message);
      }
    };

    const start = () => {
      if (timer != null) return;
      timer = setInterval(fetchOnce, liveTickIntervalMs());
    };
    const stop = () => {
      if (timer != null) { clearInterval(timer); timer = null; }
    };
    const onVis = () => {
      if (_isTabVisible()) { fetchOnce(); start(); } else { stop(); }
    };

    fetchOnce();
    if (_isTabVisible()) start();
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis);
    }

    return () => {
      cancelled = true;
      stop();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVis);
      }
    };
  }, [ticker, enabled]);

  return { tick, error };
}


export function useTheoryMulti(ticker, theoryNames, window, params,
                                refreshKey = 0, live = false) {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const lastReq = useRef(0);

  const namesKey = (theoryNames || []).slice().sort().join(',');
  const fetcher = useCallback(async () => {
    if (!ticker || !window || !namesKey) {
      setPayload(null);
      return;
    }
    const key = cacheKey(`multi:${namesKey}`, ticker, window, params);
    const cached = CACHE.get(key);
    if (cached && Date.now() - cached.ts < TTL_MS && refreshKey === 0 && !live) {
      setPayload(cached.payload);
      return;
    }
    setLoading(true);
    setError(null);
    const reqId = ++lastReq.current;
    try {
      const q = new URLSearchParams({ window, theories: namesKey });
      if (live) q.set('live', 'true');
      if (params && Object.keys(params).length) {
        q.set('params', JSON.stringify(params));
      }
      const url = `/theories/multi/${encodeURIComponent(ticker)}?${q.toString()}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      if (reqId !== lastReq.current) return;
      if (!live) CACHE.set(key, { ts: Date.now(), payload: j });
      setPayload(j);
    } catch (e) {
      if (reqId !== lastReq.current) return;
      setError(e.message);
    } finally {
      if (reqId === lastReq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, namesKey, window, JSON.stringify(params), refreshKey, live]);

  useEffect(() => { fetcher(); }, [fetcher]);

  // Live polling — share the same module-level dispatcher as useTheory.
  // This is the THIRD interval the old file used to spin up; collapsing
  // it into the shared dispatcher is the main win of this rewrite.
  useEffect(() => {
    if (!live) return undefined;
    return _subscribeTheoryTick(fetcher);
  }, [live, fetcher]);

  return { payload, loading, error, refresh: fetcher };
}
