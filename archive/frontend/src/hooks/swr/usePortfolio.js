import useSWR from 'swr';

/**
 * Perf-Fix Pass — cached portfolio endpoints.
 *
 * Two of the most-fetched endpoints in the app:
 *   - /portfolio/context  — opened by Decision Cockpit + Today + Trade Loop
 *   - /portfolio/equity   — opened by Layout topbar + Today + Cockpit charts
 *
 * Layout.jsx still fetches /portfolio/equity?limit=240 on its own 4s
 * timer (left alone for safety); these hooks let other consumers share
 * its cache via SWR's dedup window when they request the same key.
 */
export function usePortfolioContext({ refreshInterval = 0 } = {}) {
  const { data, error, isLoading, mutate } = useSWR('/portfolio/context', {
    refreshInterval,
  });
  return { context: data, error, isLoading, refresh: mutate };
}

export function usePortfolioEquity({ limit = 240, refreshInterval = 0 } = {}) {
  const key = `/portfolio/equity?limit=${limit}`;
  const { data, error, isLoading, mutate } = useSWR(key, {
    refreshInterval,
  });
  return { equity: data, error, isLoading, refresh: mutate };
}
