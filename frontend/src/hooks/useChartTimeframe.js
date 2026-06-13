/**
 * Feature-Merge F4 — canonical chart timeframe hook.
 *
 * Manages the currently selected timeframe across chart pages and
 * persists it per-ticker in localStorage so a user returning to the
 * same ticker on the same page sees the same window.
 *
 * Surface:
 *   const { timeframe, setTimeframe, backendWindow, trimDays }
 *     = useChartTimeframe(ticker, defaultTf = '1M');
 *
 *   - `timeframe`        : canonical 10-value ID
 *                          ('1D','1W','1M','3M','6M','YTD','1Y','3Y','5Y','MAX')
 *   - `backendWindow`    : value to send as ?window= on /analysis
 *                          ('today','5d','all') — the only three the
 *                          current /analysis backend supports.
 *   - `trimDays`         : if non-null, bars older than N calendar days
 *                          should be filtered client-side. Lets us serve
 *                          1W from a 5d fetch and 3Y/5Y from `all`.
 *
 * The mapping below is the SINGLE source of truth — every chart page
 * imports from here so cross-page consistency is automatic.
 */
import { useCallback, useEffect, useState } from 'react';

export const TIMEFRAMES = ['1D', '1W', '1M', '3M', '6M', 'YTD', '1Y', '3Y', '5Y', 'MAX'];

// timeframe → { backendWindow, trimDays | null }
// Backend /analysis route only supports today / 5d / all.
// We map every UI value to the smallest backend window that still
// covers it, and (optionally) trim newer-than-X bars client-side.
export const TIMEFRAME_MAP = {
  '1D':  { backendWindow: 'today', trimDays: null   },
  '1W':  { backendWindow: '5d',    trimDays: null   },
  '1M':  { backendWindow: 'all',   trimDays: 30     },
  '3M':  { backendWindow: 'all',   trimDays: 90     },
  '6M':  { backendWindow: 'all',   trimDays: 180    },
  'YTD': { backendWindow: 'all',   trimDays: 'ytd'  },
  '1Y':  { backendWindow: 'all',   trimDays: 365    },
  '3Y':  { backendWindow: 'all',   trimDays: 1095   },
  '5Y':  { backendWindow: 'all',   trimDays: 1825   },
  'MAX': { backendWindow: 'all',   trimDays: null   },
};

function storageKey(ticker) {
  return `tb.chart.tf.${(ticker || '').toUpperCase()}`;
}

export default function useChartTimeframe(ticker, defaultTf = '1M') {
  const [timeframe, setTimeframeState] = useState(() => {
    if (typeof window === 'undefined') return defaultTf;
    try {
      const stored = window.localStorage.getItem(storageKey(ticker));
      if (stored && TIMEFRAMES.includes(stored)) return stored;
    } catch (_) { /* ignore */ }
    return defaultTf;
  });

  // Re-read on ticker change so each ticker remembers its own last tf.
  useEffect(() => {
    if (typeof window === 'undefined' || !ticker) return;
    try {
      const stored = window.localStorage.getItem(storageKey(ticker));
      if (stored && TIMEFRAMES.includes(stored)) {
        setTimeframeState(stored);
      }
    } catch (_) { /* ignore */ }
  }, [ticker]);

  const setTimeframe = useCallback((tf) => {
    if (!TIMEFRAMES.includes(tf)) return;
    setTimeframeState(tf);
    try {
      window.localStorage.setItem(storageKey(ticker), tf);
    } catch (_) { /* quota or private mode */ }
  }, [ticker]);

  const mapping = TIMEFRAME_MAP[timeframe] || TIMEFRAME_MAP['1M'];

  return {
    timeframe,
    setTimeframe,
    backendWindow: mapping.backendWindow,
    trimDays: mapping.trimDays,
  };
}
