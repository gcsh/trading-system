import useSWR from 'swr';

/**
 * Perf-Fix Pass — cached /quote/{ticker}.
 *
 * /quote/{ticker} is the fast live-price endpoint (P10.1). It's
 * referenced from at least 3 pages and the chart hooks. Without
 * sharing, navigating between two pages that both quote AAPL would
 * fire two requests within milliseconds; SWR dedups them.
 *
 * Pass `refreshInterval` to opt into polling (e.g. 1000ms during
 * market hours, 10_000ms off-hours — caller decides; see useTheory's
 * existing market-hours gate).
 */
export function useQuote(ticker, { refreshInterval = 0, enabled = true } = {}) {
  const key = enabled && ticker ? `/quote/${encodeURIComponent(ticker)}` : null;
  const { data, error, isLoading, mutate } = useSWR(key, {
    refreshInterval,
  });
  return { quote: data, error, isLoading, refresh: mutate };
}
