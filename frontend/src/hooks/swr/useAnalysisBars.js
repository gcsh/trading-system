/**
 * Feature-Merge F4 — canonical /analysis OHLC bars hook.
 *
 * THIS IS THE SINGLE SOURCE OF TRUTH for OHLC bars on chart pages.
 * Every page that renders a price chart for a ticker (StockAnalysis,
 * TheoryStudio, anywhere else) must read bars through this hook so
 * "same ticker → same candles" cross-page consistency holds.
 *
 * Backend route: GET /analysis/{ticker}?window=today|5d|all
 * Returned shape (subset we care about):
 *   {
 *     bars: [{ t, open, high, low, close, volume }, ...],
 *     observations: [...],
 *     knowledge: {...},
 *     theses: {...},
 *     bar_source: 'thetadata' | 'yfinance' | 'none',
 *     ...
 *   }
 *
 * The optional `trimDays` argument lets the timeframe selector serve
 * extra timeframes the backend doesn't directly support (1M / 3M / 6M
 * / YTD / 1Y / 3Y / 5Y come out of a single window='all' fetch +
 * client-side trim). This is a CACHE WIN — SWR dedups one /analysis
 * request and every trimmed window slices off the same in-memory payload.
 */
import { useMemo } from 'react';
import useSWR from 'swr';

function trimBars(bars, trimDays) {
  if (!Array.isArray(bars) || !bars.length || trimDays == null) return bars || [];

  // YTD — calendar-year filter.
  if (trimDays === 'ytd') {
    const yearStart = new Date(new Date().getFullYear(), 0, 1).getTime();
    return bars.filter((b) => {
      const ts = b && (b.t || b.timestamp);
      if (!ts) return true;
      return new Date(ts).getTime() >= yearStart;
    });
  }

  // Numeric day count from now.
  const days = Number(trimDays);
  if (!Number.isFinite(days) || days <= 0) return bars;
  const cutoff = Date.now() - days * 86_400_000;
  return bars.filter((b) => {
    const ts = b && (b.t || b.timestamp);
    if (!ts) return true;
    return new Date(ts).getTime() >= cutoff;
  });
}

/**
 * @param {string} ticker         — symbol (case-insensitive)
 * @param {string} backendWindow  — 'today' | '5d' | 'all'
 * @param {number|string|null} trimDays
 *                                — null = no trim; number = trailing N
 *                                  calendar days; 'ytd' = year-start cutoff.
 * @param {object} opts           — SWR opts (refreshInterval, enabled).
 */
export default function useAnalysisBars(
  ticker, backendWindow = 'today', trimDays = null, opts = {},
) {
  const { refreshInterval = 0, enabled = true } = opts;
  const key = enabled && ticker
    ? `/analysis/${encodeURIComponent(ticker.toUpperCase())}?window=${encodeURIComponent(backendWindow)}`
    : null;
  const { data, error, isLoading, mutate } = useSWR(key, {
    refreshInterval,
    revalidateOnFocus: false,
  });

  const trimmedBars = useMemo(
    () => trimBars(data?.bars || [], trimDays),
    [data, trimDays],
  );

  return {
    payload: data,
    bars: trimmedBars,
    allBars: data?.bars || [],
    observations: data?.observations || [],
    knowledge: data?.knowledge || {},
    theses: data?.theses || {},
    summary: data?.summary,
    barSource: data?.bar_source,
    error,
    isLoading,
    refresh: mutate,
  };
}
