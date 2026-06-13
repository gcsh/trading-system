import { useEffect, useRef, useState } from 'react';

/**
 * Poll the freshest price for a ticker so charts can draw a live, ticking
 * price line. Returns { price, t, source, ageMs } or null.
 *
 * Live-ness is bounded by the data feed: with a Finnhub key it's real-time;
 * otherwise it's the latest 1-minute yfinance bar (a few minutes delayed) and
 * only moves while the US market is open.
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
        const r = await fetch(`/market/last/${encodeURIComponent(ticker)}`);
        if (!r.ok) return;
        const d = await r.json();
        // Ignore stale responses if the ticker changed mid-flight.
        if (active && d && d.price > 0 && tickerRef.current === ticker) {
          setQuote({ ...d, receivedAt: Date.now() });
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
