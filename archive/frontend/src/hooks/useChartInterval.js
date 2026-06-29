/**
 * Chart interval (candle granularity) hook.
 *
 * Sibling to useChartTimeframe — the timeframe says HOW MUCH history to
 * load (1D / 1W / 1M / ... / MAX); the interval says HOW FINE each
 * candle is (1m / 5m / 15m / 30m / 1h / 4h / D / W).
 *
 * `null` means "use the backend's default interval for the chosen
 * window" (today→5m, 5d→15m, long-range→1d). Picking an explicit
 * interval threads `&interval=` onto the /analysis fetch.
 *
 * 4h is aggregated client-side from a 1h backend fetch — see
 * `aggregateBars` in useAnalysisBars.js. The backend itself only
 * supports 1m/5m/15m/30m/1h/1d/1w.
 */
import { useCallback, useEffect, useState } from 'react';

export const INTERVALS = [
  { id: 'auto', label: 'Auto', backend: null,  aggregate: null },
  { id: '1m',   label: '1m',   backend: '1m',  aggregate: null },
  { id: '5m',   label: '5m',   backend: '5m',  aggregate: null },
  { id: '15m',  label: '15m',  backend: '15m', aggregate: null },
  { id: '30m',  label: '30m',  backend: '30m', aggregate: null },
  { id: '1h',   label: '1h',   backend: '1h',  aggregate: null },
  { id: '4h',   label: '4h',   backend: '1h',  aggregate: 4 },
  { id: '1d',   label: 'D',    backend: '1d',  aggregate: null },
  { id: '1w',   label: 'W',    backend: '1w',  aggregate: null },
];

export const INTERVAL_IDS = INTERVALS.map((i) => i.id);

function storageKey(ticker) {
  return `tb.chart.iv.${(ticker || '').toUpperCase()}`;
}

export default function useChartInterval(ticker, defaultIv = 'auto') {
  const [interval, setIntervalState] = useState(() => {
    if (typeof window === 'undefined') return defaultIv;
    try {
      const stored = window.localStorage.getItem(storageKey(ticker));
      if (stored && INTERVAL_IDS.includes(stored)) return stored;
    } catch (_) { /* ignore */ }
    return defaultIv;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !ticker) return;
    try {
      const stored = window.localStorage.getItem(storageKey(ticker));
      if (stored && INTERVAL_IDS.includes(stored)) {
        setIntervalState(stored);
      }
    } catch (_) { /* ignore */ }
  }, [ticker]);

  const setInterval = useCallback((iv) => {
    if (!INTERVAL_IDS.includes(iv)) return;
    setIntervalState(iv);
    try {
      window.localStorage.setItem(storageKey(ticker), iv);
    } catch (_) { /* ignore */ }
  }, [ticker]);

  const cfg = INTERVALS.find((i) => i.id === interval) || INTERVALS[0];

  return {
    interval,
    setInterval,
    backendInterval: cfg.backend,   // string to send on the URL (or null)
    aggregate: cfg.aggregate,       // bucket size when client-side aggregating (or null)
  };
}
