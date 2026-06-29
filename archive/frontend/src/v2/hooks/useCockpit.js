/* MITS Phase 19 Stream 3 — Decision Cockpit hook.
 *
 *   const { cockpit, provenance, loading, error, refresh } = useCockpit(identifier);
 *
 *   cockpit    → /decision/cockpit/{identifier} payload (or null)
 *   provenance → /decision/provenance?limit=20 picker rows (always loaded)
 *
 * identifier can be a trade_id, decision_provenance.id, or ticker. When
 * `identifier` is null/undefined, only the provenance picker is loaded —
 * useful for the picker landing.
 *
 * All errors are surfaced as `error`; missing keys in the response are
 * left as-is so the caller can render EmptyStates without us swallowing
 * them.
 */
import { useCallback, useEffect, useState } from 'react';

const PROV_LIMIT = 20;

export default function useCockpit(identifier) {
  const [cockpit,    setCockpit]    = useState(null);
  const [provenance, setProvenance] = useState(null);
  const [loading,    setLoading]    = useState(false);
  const [error,      setError]      = useState(null);

  const fetcher = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const tasks = [
        fetch(`/decision/provenance?limit=${PROV_LIMIT}`),
      ];
      if (identifier) {
        tasks.push(fetch(`/decision/cockpit/${encodeURIComponent(identifier)}`));
      }
      const results = await Promise.allSettled(tasks);

      // Provenance — always first
      const provR = results[0];
      if (provR.status === 'fulfilled' && provR.value.ok) {
        const j = await provR.value.json();
        setProvenance(j);
      } else if (provR.status === 'fulfilled') {
        setProvenance(null);
        setError(`provenance ${provR.value.status}`);
      } else {
        setProvenance(null);
        setError(provR.reason?.message || 'provenance fetch failed');
      }

      // Cockpit (optional)
      if (identifier) {
        const ckR = results[1];
        if (ckR.status === 'fulfilled' && ckR.value.ok) {
          const j = await ckR.value.json();
          setCockpit(j);
        } else if (ckR.status === 'fulfilled') {
          setCockpit(null);
          setError(`cockpit ${ckR.value.status}`);
        } else {
          setCockpit(null);
          setError(ckR.reason?.message || 'cockpit fetch failed');
        }
      } else {
        setCockpit(null);
      }
    } catch (e) {
      setError(e.message || 'failed to load cockpit');
    } finally {
      setLoading(false);
    }
  }, [identifier]);

  useEffect(() => { fetcher(); }, [fetcher]);

  return { cockpit, provenance, loading, error, refresh: fetcher };
}
