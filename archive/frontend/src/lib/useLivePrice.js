import { useEffect, useRef, useState } from 'react';

/**
 * Poll the freshest price for a ticker so charts can draw a live, ticking
 * price line. Returns the full /quote payload:
 *   { price, ts, source, age_seconds, is_fresh, is_stale,
 *     market_status, approved_source, ticker, receivedAt }
 * or null when no quote is available yet.
 *
 * 2026-06-15 — switched from /market/last/{ticker} (price-only, yfinance
 * tag) to /quote/{ticker} so the UI gets the freshness booleans the
 * backend now exposes. Without this, every legacy chart kept rendering
 * "LIVE" on top of stale yfinance prints because the source field was
 * the only signal.
 */
export function useLivePrice(ticker, { intervalMs = 4000, enabled = true } = {}) {
  const [quote, setQuote] = useState(null);
  const tickerRef = useRef(ticker);
  tickerRef.current = ticker;

  useEffect(() => {
    if (!ticker || !enabled) return undefined;
    let active = true;
    const load = async () => {
      try {
        const r = await fetch(`/quote/${encodeURIComponent(ticker)}`);
        if (!r.ok) return;
        const d = await r.json();
        // Ignore stale responses if the ticker changed mid-flight.
        if (active && d && d.price > 0 && tickerRef.current === ticker) {
          // Preserve legacy field names (`t`) so existing callers
          // that only read `quote.t` keep working without changes.
          setQuote({ ...d, t: d.ts, receivedAt: Date.now() });
        }
      } catch { /* ignore transient errors */ }
    };
    setQuote(null);
    load();
    const id = setInterval(load, intervalMs);
    return () => { active = false; clearInterval(id); };
  }, [ticker, enabled, intervalMs]);

  return quote;
}
