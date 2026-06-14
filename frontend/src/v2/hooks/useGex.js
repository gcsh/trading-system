/* MITS Phase 19 Stream 2 — Gamma Exposure data hook.
 *
 * Fetches /heatseeker/{ticker} (the full GEX snapshot) on mount and
 * every `refreshMs` (default 30s). Returns the raw payload + loading +
 * error state. Stream 2 pages compute derived series in render to
 * keep this hook stable.
 *
 *   const { data, loading, error, refresh } = useGex('SPY');
 *   data → {
 *     ticker, timestamp, spot_price, call_wall, put_wall, gamma_flip,
 *     dealer_regime, gex_by_strike: [{ strike, call_gex, put_gex,
 *       net_gex, call_oi, put_oi, total_oi, expiry, dte, has_zero_dte }],
 *     net_gex_total, call_gex_total, put_gex_total, total_call_oi,
 *     total_put_oi, total_oi, atm_iv, expected_move, expected_move_pct,
 *     max_gamma_strike, max_gamma_value, vol_trigger, distance_to_flip,
 *     dealer_flow, dealer_flow_intensity, pin_risk_strike,
 *     pin_risk_distance, total_vanna, total_charm,
 *     zero_dte_net_gex, zero_dte_share, expiration, max_dte,
 *     source, ok, note, stale, prev_call_wall, prev_gamma_flip,
 *     flip_direction
 *   }
 */
import { useCallback, useEffect, useState } from 'react';

export default function useGex(ticker, { refreshMs = 30_000, expiration = 'all' } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    if (!ticker) return undefined;
    let cancelled = false;
    setLoading(true);

    async function fetchOnce() {
      try {
        const qs = expiration && expiration !== 'all'
          ? `?expiration=${encodeURIComponent(expiration)}`
          : '';
        const r = await fetch(`/heatseeker/${encodeURIComponent(ticker)}${qs}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        if (cancelled) return;
        setData(j);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e.message || 'heatseeker fetch failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchOnce();
    const id = setInterval(fetchOnce, refreshMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [ticker, refreshMs, tick, expiration]);

  return { data, error, loading, refresh };
}
