import useSWR from 'swr';

/**
 * Perf-Fix Pass — cached /decision/scorecard.
 *
 * Referenced from DecisionScorecard.jsx, DecisionCockpit.jsx, and the
 * v2 Cockpit panels. Read-side only; SWR dedup + revalidate-on-focus
 * means switching between those pages doesn't re-hit the endpoint
 * inside the 5s window.
 *
 * The scorecard is an expensive computation backend-side (Brier/ECE
 * aggregates), so even a 5s dedup saves real CPU on the engine box.
 */
export function useDecisionScorecard({ refreshInterval = 0 } = {}) {
  const { data, error, isLoading, mutate } = useSWR('/decision/scorecard', {
    refreshInterval,
  });
  return { scorecard: data, error, isLoading, refresh: mutate };
}
