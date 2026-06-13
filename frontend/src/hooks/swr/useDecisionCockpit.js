/**
 * Feature-Merge F3 — SWR-cached /decision/cockpit + /decision/provenance.
 *
 * Why this exists:
 *   The cockpit endpoint synchronously runs `compute_all_counterfactuals`
 *   + 4 learning-insight aggregations on every request (decision.py:518,
 *   530). Measured cold latency on EC2 is 8-18s when the engine cycle
 *   competes for DB; warm calls are ~15ms. The legacy DecisionCockpit
 *   page used `useEffect+fetch+useState` with NO cache, so every
 *   navigation hit the cold path again.
 *
 *   This hook wraps the same two endpoints in SWR with:
 *     • keepPreviousData: true        — navigation feels instant; the
 *       previous cockpit stays painted until the new one resolves.
 *     • revalidateIfStale: true       — but a cached entry < 5 minutes
 *       old short-circuits the spinner entirely (SWR returns it before
 *       firing the revalidation request).
 *     • dedupingInterval: 30s         — two components (e.g. picker +
 *       page) on the same identifier collapse to one network call.
 *     • revalidateOnFocus: false      — the operator alt-tabs back
 *       constantly while a cockpit is open; we do NOT want a 10s spinner
 *       every time they switch windows. The page polls on its own if
 *       the operator wants fresh data.
 *
 * Cross-page consistency:
 *   The cockpit's `would_have_been`, `counterfactuals`, and
 *   `learning_insights.funnel_snapshot` keys are also surfaced on the
 *   Today page (via useFunnel) and the Hypothesis Studio. By going
 *   through SWR, all three consumers share the same cache entry for the
 *   same identifier — no double-fetch, no skew between pages.
 */
import useSWR from 'swr';

const COCKPIT_TTL_MS = 30_000;   // dedup window
const STALE_THRESHOLD_MS = 5 * 60_000; // 5min — beyond this, show spinner

export function useDecisionCockpit(identifier, opts = {}) {
  const { refreshInterval = 0 } = opts;
  const key = identifier
    ? `/decision/cockpit/${encodeURIComponent(identifier)}`
    : null;
  const { data, error, isLoading, isValidating, mutate } = useSWR(key, {
    refreshInterval,
    keepPreviousData: true,
    revalidateOnFocus: false,
    revalidateOnReconnect: true,
    dedupingInterval: COCKPIT_TTL_MS,
  });
  return {
    cockpit: data || null,
    error,
    isLoading,
    isValidating,
    refresh: mutate,
  };
}

export function useRecentProvenance({ limit = 20 } = {}) {
  const key = `/decision/provenance?limit=${limit}`;
  const { data, error, isLoading, mutate } = useSWR(key, {
    revalidateOnFocus: false,
    dedupingInterval: COCKPIT_TTL_MS,
  });
  return {
    provenance: data || null,
    error,
    isLoading,
    refresh: mutate,
  };
}

export const _F3_STALE_THRESHOLD_MS = STALE_THRESHOLD_MS;
