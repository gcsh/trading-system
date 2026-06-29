/**
 * Feature-Merge F3 — Counterfactual What-If interactive hook.
 *
 *   const { cf, computing, error, recompute } = useCounterfactual(provId);
 *
 *   cf       → latest counterfactual bundle (sizing / policy / consensus)
 *   recompute(kind, body) → POST /learning/counterfactual/{provId}/{kind}
 *
 * `kind` is one of "sizing", "policy", "consensus". Each POST splices
 * the returned alternative into `cf[kind]` without re-fetching the
 * whole cockpit — the operator can iterate factor / rule / agent
 * choices in a tight loop without round-tripping the heavy 8s+ cockpit
 * endpoint.
 *
 * Initial state can be seeded from the cockpit's `counterfactuals`
 * blob (passed in via `initial`) so the panel renders instantly with
 * the snapshot computed at cockpit load, then updates as the operator
 * runs new scenarios.
 *
 * Errors are surfaced via `error`; on failure `cf[kind]` is left
 * unchanged so the UI doesn't blank.
 */
import { useCallback, useEffect, useState } from 'react';

const VALID_KINDS = new Set(['sizing', 'policy', 'consensus']);

export function useCounterfactual(provId, { initial = null } = {}) {
  const [cf, setCf] = useState(initial);
  const [computing, setComputing] = useState(false);
  const [error, setError] = useState(null);

  // When the parent cockpit reloads with a fresh `initial` blob, sync
  // our local cf so the panel reflects the new baseline. We only
  // overwrite when provId changes — otherwise re-running a single kind
  // would get nuked by the cockpit's next stale-while-revalidate tick.
  useEffect(() => {
    setCf(initial);
    setError(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provId]);

  const recompute = useCallback(async (kind, body) => {
    if (!provId) {
      setError('No decision_provenance ID — pick a numeric trade or decision first.');
      return null;
    }
    if (!VALID_KINDS.has(kind)) {
      setError(`Invalid counterfactual kind: ${kind}`);
      return null;
    }
    setComputing(true);
    setError(null);
    try {
      const res = await fetch(
        `/learning/counterfactual/${encodeURIComponent(provId)}/${kind}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body || {}),
        },
      );
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        setError(`Recompute ${kind} failed (${res.status}): ${text.slice(0, 200)}`);
        return null;
      }
      const payload = await res.json();
      // Backend wraps result as {counterfactual: ...}; unwrap if so.
      const next = payload?.counterfactual ?? payload;
      setCf((prev) => ({ ...(prev || {}), [kind]: next }));
      return next;
    } catch (e) {
      setError(e?.message || `Recompute ${kind} failed`);
      return null;
    } finally {
      setComputing(false);
    }
  }, [provId]);

  return { cf, computing, error, recompute, setCf };
}

export default useCounterfactual;
