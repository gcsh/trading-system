import useSWR from 'swr';

/**
 * Feature-Merge F1 — canonical /learning/funnel hook for the ORIGINAL site.
 *
 * Single source of truth for the decision-pipeline funnel surfaced on
 * Today (ThroughputAlertBanner + FunnelSummaryPanel) AND, going forward,
 * on /decision-scorecard, /decision-cockpit, and /hypothesis-studio.
 *
 * Wire everywhere through this hook so the same numbers tell the same
 * story on every page — no per-component fetch divergence.
 *
 * Shape returned by `/learning/funnel`:
 *   {
 *     row: {
 *       date, window_days,
 *       n_evaluations, n_submitted,
 *       confidence_histogram_json,   // JSON string
 *       top_3_blockers_json,         // JSON string
 *       top_surgical_change_candidate,
 *       composite_quality_mean,
 *       computed_at,
 *       ...
 *     },
 *     report: { stages: [10], confidence_histograms, counterfactual, ... },
 *   }
 *
 * `confidence_histogram_json` parses to `{ bin_edges:[0..1], all_evals:[10],
 * non_hold:[10], submitted:[10] }`.
 *
 * The hook also derives `submission_rate` and `smoking_gun` so every
 * consumer agrees on what "throughput collapse" means.
 */

function parseJSON(s, fallback) {
  if (s == null) return fallback;
  if (typeof s !== 'string') return s;
  try { return JSON.parse(s); } catch (_) { return fallback; }
}

export function useFunnel({ refreshInterval = 60_000 } = {}) {
  const { data, error, isLoading, mutate } = useSWR('/learning/funnel', {
    refreshInterval,
  });

  const row = data?.row || null;
  const histogram = row ? parseJSON(row.confidence_histogram_json, null) : null;
  const blockers = row ? parseJSON(row.top_3_blockers_json, []) : [];

  const evals = Number(row?.n_evaluations || 0);
  const subs  = Number(row?.n_submitted   || 0);
  const submissionRate = evals > 0 ? subs / evals : null;

  let smokingGun = null;
  if (histogram && Array.isArray(histogram.non_hold)) {
    const total = histogram.non_hold.reduce((a, b) => a + Number(b || 0), 0);
    const zeroBin = Number(histogram.non_hold[0] || 0);
    if (total > 0) {
      smokingGun = {
        total,
        zeroBin,
        pct: zeroBin / total,
        isAlarming: (zeroBin / total) > 0.5,
      };
    }
  }

  return {
    data,
    row,
    report: data?.report || null,
    histogram,
    blockers,
    submissionRate,
    smokingGun,
    isLoading,
    error,
    refresh: mutate,
  };
}

export default useFunnel;
