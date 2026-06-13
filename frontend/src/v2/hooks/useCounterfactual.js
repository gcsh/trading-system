/* MITS Phase 19 Stream 3 — Counterfactual What-If hook.
 *
 *   const { cf, computing, error, recompute } = useCounterfactual(provId);
 *
 *   cf            → latest cf bundle from /learning/counterfactual/{id}
 *   recompute(kind, body) → POST /learning/counterfactual/{id}/{kind}
 *                            (kind ∈ "sizing" | "policy" | "consensus")
 *
 * Each POST replaces `cf[kind]` with the freshly-computed alternative so
 * the UI can show a delta against the original without refetching the
 * whole cockpit.
 */
import { useCallback, useEffect, useState } from 'react';

export default function useCounterfactual(provId) {
  const [cf,        setCf]        = useState(null);
  const [computing, setComputing] = useState(false);
  const [error,     setError]     = useState(null);

  // Initial fetch — only when provId changes
  useEffect(() => {
    let cancelled = false;
    async function fetchInitial() {
      if (!provId) { setCf(null); return; }
      setComputing(true);
      setError(null);
      try {
        const r = await fetch(`/learning/counterfactual/${encodeURIComponent(provId)}`);
        if (!r.ok) {
          if (!cancelled) {
            setCf(null);
            setError(`cf fetch ${r.status}`);
          }
          return;
        }
        const j = await r.json();
        if (!cancelled) setCf(j);
      } catch (e) {
        if (!cancelled) setError(e.message || 'cf fetch failed');
      } finally {
        if (!cancelled) setComputing(false);
      }
    }
    fetchInitial();
    return () => { cancelled = true; };
  }, [provId]);

  const recompute = useCallback(async (kind, body) => {
    if (!provId) return null;
    if (!['sizing', 'policy', 'consensus'].includes(kind)) {
      throw new Error(`invalid cf kind: ${kind}`);
    }
    setComputing(true);
    setError(null);
    try {
      const r = await fetch(
        `/learning/counterfactual/${encodeURIComponent(provId)}/${kind}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body || {}),
        }
      );
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        setError(`recompute ${r.status}: ${txt.slice(0, 200)}`);
        return null;
      }
      const j = await r.json();
      // Splice the new branch into the bundle
      setCf((prev) => ({ ...(prev || {}), [kind]: j }));
      return j;
    } catch (e) {
      setError(e.message || 'recompute failed');
      return null;
    } finally {
      setComputing(false);
    }
  }, [provId]);

  return { cf, computing, error, recompute, setCf };
}
