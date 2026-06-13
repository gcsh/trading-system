import useSWR from 'swr';

/**
 * Feature-Merge F2 — canonical /heatseeker/multi/{ticker} hook for the
 * ORIGINAL site.
 *
 * Single source of truth for the multi-expiration GEX matrix surfaced on
 * /heatseeker (Intel tab) and any per-ticker GEX widget on Stock Analysis.
 * Wiring everywhere through this hook guarantees that the same numbers
 * tell the same story on every page — no per-component fetch divergence.
 *
 * Shape returned by `/heatseeker/multi/{ticker}`:
 *   {
 *     ticker, spot_price, computed_at,
 *     expirations: [
 *       {
 *         expiry, dte,
 *         label: "0DTE" | "1W" | "2W" | "3W" | "1M" | ">1M",
 *         call_gex_total, put_gex_total, net_gex_total,
 *         gex_by_strike: [{ strike, call_gex, put_gex, net_gex }, ...]
 *       },
 *       ...
 *     ]
 *   }
 *
 * On yfinance rate-limit the backend degrades gracefully to
 * `expirations: []` plus an optional `note` — the UI shows an empty-state
 * banner instead of a 500.
 *
 * Defaults: 60s refresh (matches the legacy Heatseeker auto-poll), 5s SWR
 * dedup (provider default), revalidate-on-focus.
 */
export function useHeatseekerMulti(ticker, { refreshInterval = 60_000, enabled = true } = {}) {
  const key = enabled && ticker
    ? `/heatseeker/multi/${encodeURIComponent(String(ticker).toUpperCase())}`
    : null;
  const { data, error, isLoading, mutate } = useSWR(key, { refreshInterval });
  return {
    data,
    ticker: data?.ticker || null,
    spotPrice: data?.spot_price ?? null,
    expirations: data?.expirations || [],
    computedAt: data?.computed_at || null,
    note: data?.note || null,
    isLoading,
    error,
    refresh: mutate,
  };
}

export default useHeatseekerMulti;
