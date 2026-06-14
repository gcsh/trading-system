/**
 * Theory overlay fetcher — Analysis page's Phase C.2 wire-up.
 *
 * Hits ``/theories/multi/{ticker}?theories=<comma list>&window=<bw>`` and
 * returns the annotations dict from the response. Skips the fetch when
 * the operator has no theories selected so we don't burn a request on
 * every render. ``window`` is the same backend slug the chart's bars
 * fetch uses, which keeps a Bollinger band on a 1Y chart computed from
 * the same 1Y of bars (no cross-window drift).
 *
 * Returned annotations are merged into the TheoryChart contract under
 * a key per theory — the chart's existing renderer handles lines,
 * zones, markers, signals natively (see TheoryChart.jsx around 367+).
 */
import useSWR from 'swr';

export default function useTheoryOverlays(ticker, theoryIds, backendWindow) {
  const ids = Array.isArray(theoryIds) ? theoryIds.filter(Boolean) : [];
  const enabled = !!ticker && ids.length > 0 && !!backendWindow;
  const key = enabled
    ? `/theories/multi/${encodeURIComponent(ticker.toUpperCase())}`
      + `?theories=${encodeURIComponent(ids.join(','))}`
      + `&window=${encodeURIComponent(backendWindow)}`
    : null;
  const { data, error, isLoading } = useSWR(key, {
    revalidateOnFocus: false,
  });
  return {
    annotations: data?.annotations || {},
    bars: data?.bars || [],
    error,
    isLoading,
  };
}
