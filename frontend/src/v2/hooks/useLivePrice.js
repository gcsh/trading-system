/* MITS Phase 19 Stream 1 — live-price hook.
 *
 * Polls /quote/{ticker} every 1s during US equity market hours and
 * every 10s off-hours. The /quote endpoint already handles the
 * Alpaca→yfinance fallback + a small in-process cache; this hook
 * just keeps the UI re-rendering with the latest tick.
 *
 *   const { tick, error } = useLivePrice('AAPL');
 *   tick → { ticker, price, source, age_seconds, ts, cached }
 *
 * Single ticker. For watchlist-wide polling (Mission Control rows),
 * see useLivePrices below.
 */
import { useEffect, useRef, useState } from 'react';

function isMarketHours(d = new Date()) {
  const day = d.getUTCDay();
  if (day === 0 || day === 6) return false;
  const utcHour = d.getUTCHours();
  const utcMin = d.getUTCMinutes();
  // ET = UTC-4 (EDT) — wider window. Real DST handled at server.
  const etHour = (utcHour - 4 + 24) % 24;
  if (etHour > 9 && etHour < 16) return true;
  if (etHour === 9 && utcMin >= 30) return true;
  return false;
}

function liveIntervalMs() {
  return isMarketHours() ? 1_000 : 10_000;
}

export default function useLivePrice(ticker, enabled = true) {
  const [tick, setTick] = useState(null);
  const [error, setError] = useState(null);
  const lastReq = useRef(0);

  useEffect(() => {
    if (!enabled || !ticker) return undefined;
    let cancelled = false;

    const fetchOnce = async () => {
      const reqId = ++lastReq.current;
      try {
        const r = await fetch(`/quote/${encodeURIComponent(ticker)}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        if (cancelled || reqId !== lastReq.current) return;
        if (j && typeof j.price === 'number' && j.price > 0) {
          setTick(j);
          setError(null);
        }
      } catch (e) {
        if (cancelled) return;
        setError(e.message || 'fetch failed');
      }
    };

    fetchOnce();
    const t = setInterval(fetchOnce, liveIntervalMs());
    return () => { cancelled = true; clearInterval(t); };
  }, [ticker, enabled]);

  return { tick, error };
}

/**
 * Batched variant for the Mission Control watchlist row. Polls each
 * ticker at a slower cadence (5s market, 30s off-hours) so we don't
 * fan out 44 × 1Hz requests.
 *
 *   const { ticks, error } = useLivePrices(['AAPL','SPY','QQQ']);
 *   ticks.AAPL → { price, source, ts }
 */
export function useLivePrices(tickers = [], enabled = true) {
  const [ticks, setTicks] = useState({});
  const [error, setError] = useState(null);
  const lastReqs = useRef({});

  useEffect(() => {
    if (!enabled || !tickers || !tickers.length) return undefined;
    let cancelled = false;

    const fetchAll = async () => {
      const results = await Promise.allSettled(
        tickers.map(async (t) => {
          const reqId = (lastReqs.current[t] || 0) + 1;
          lastReqs.current[t] = reqId;
          try {
            const r = await fetch(`/quote/${encodeURIComponent(t)}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const j = await r.json();
            if (cancelled || lastReqs.current[t] !== reqId) return null;
            if (j && typeof j.price === 'number' && j.price > 0) {
              return [t, j];
            }
            return null;
          } catch (e) {
            return null;
          }
        }),
      );
      if (cancelled) return;
      const next = {};
      for (const r of results) {
        if (r.status === 'fulfilled' && r.value) {
          next[r.value[0]] = r.value[1];
        }
      }
      // Merge so a single failed ticker doesn't wipe the others.
      setTicks((prev) => ({ ...prev, ...next }));
    };

    fetchAll();
    const interval = isMarketHours() ? 5_000 : 30_000;
    const t = setInterval(fetchAll, interval);
    return () => { cancelled = true; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tickers.join(','), enabled]);

  return { ticks, error };
}
