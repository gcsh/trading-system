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
import { useMemo } from 'react';

// Module-level fallbacks so an idle hook (no theories selected) never
// hands a fresh reference to its consumer. Without this, every render
// got a brand-new `{}` → useMemo deps in StockAnalysis kept missing
// the cache → TheoryChart's expensive annotation effect kept refiring
// → the main thread was busy tearing down and re-adding every overlay
// line series → clicks/drawings felt laggy or dead.
const EMPTY_ANN = Object.freeze({});
const EMPTY_BARS = Object.freeze([]);

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

  // Stabilise references: if data is undefined, we return the same
  // frozen empty objects every render so downstream useMemo deps hit
  // the cache. If data exists, `data.annotations` / `data.bars` are
  // already stable across renders (SWR caches the same object until
  // the next fetch).
  const annotations = useMemo(
    () => (data && data.annotations) ? data.annotations : EMPTY_ANN,
    [data],
  );
  const bars = useMemo(
    () => (data && data.bars) ? data.bars : EMPTY_BARS,
    [data],
  );

  return { annotations, bars, error, isLoading };
}
