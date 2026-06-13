/* MITS Phase 19 Stream 1 — funnel hook.
 *
 * Wraps /learning/funnel + /learning/funnel/history into a single
 * convenient hook for MissionControl + LearningFunnel pages.
 *
 *   const { funnel, history, loading, error, refresh } = useFunnel({
 *     historyDays: 7,
 *   });
 *
 *   funnel  → { source, window_days, persisted, row, report }
 *   history → array of decision_funnel_daily rows (newest last)
 *   The histograms come back as JSON strings inside row — we parse
 *   them here so consumers don't have to.
 */
import { useCallback, useEffect, useState } from 'react';

function parseJSON(s, fallback) {
  if (s == null) return fallback;
  if (typeof s !== 'string') return s;
  try { return JSON.parse(s); } catch (_) { return fallback; }
}

function decoratedRow(row) {
  if (!row || typeof row !== 'object') return row;
  return {
    ...row,
    confidence_histogram: parseJSON(row.confidence_histogram_json, null),
    top_3_blockers:       parseJSON(row.top_3_blockers_json, []),
  };
}

export default function useFunnel({ historyDays = 7 } = {}) {
  const [funnel,  setFunnel]  = useState(null);
  const [history, setHistory] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);

  const fetcher = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [funnelR, histR] = await Promise.allSettled([
        fetch('/learning/funnel'),
        fetch(`/learning/funnel/history?days=${historyDays}`),
      ]);

      if (funnelR.status === 'fulfilled' && funnelR.value.ok) {
        const j = await funnelR.value.json();
        if (j?.row) j.row = decoratedRow(j.row);
        // Parse confidence_histogram from report stages too if present.
        setFunnel(j);
      } else if (funnelR.status === 'fulfilled') {
        setFunnel(null);
        setError(`funnel ${funnelR.value.status}`);
      }

      if (histR.status === 'fulfilled' && histR.value.ok) {
        const j = await histR.value.json();
        if (Array.isArray(j?.rows)) {
          j.rows = j.rows.map(decoratedRow);
        }
        setHistory(j);
      } else if (histR.status === 'fulfilled') {
        setHistory(null);
      }
    } catch (e) {
      setError(e.message || 'failed to load funnel');
    } finally {
      setLoading(false);
    }
  }, [historyDays]);

  useEffect(() => { fetcher(); }, [fetcher]);

  return { funnel, history, loading, error, refresh: fetcher };
}
