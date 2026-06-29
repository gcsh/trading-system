import React, { useEffect, useState } from 'react';

/**
 * MITS Phase 0 — per-ticker corpus status chip.
 *
 * Renders a compact pill showing the bootstrap state for a ticker:
 *   building / ready / insufficient / error / pending / unknown
 *
 * Polls /knowledge/corpus/status every 8s while in `building` so the
 * UI reflects progress without a manual reload.
 */
const STATUS_META = {
  ready:        { label: 'corpus ready', cls: 'success' },
  building:     { label: 'corpus building', cls: 'info' },
  insufficient: { label: 'thin corpus', cls: 'warn' },
  error:        { label: 'corpus error', cls: 'danger' },
  pending:      { label: 'queued', cls: 'muted' },
};


export default function CorpusStatusChip({ ticker }) {
  const [row, setRow] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    let timer = null;

    const fetchOnce = async () => {
      try {
        const res = await fetch('/knowledge/corpus/status');
        if (!res.ok) return;
        const all = await res.json();
        if (cancelled) return;
        const match = (all || []).find((r) => r.ticker === ticker);
        setRow(match || null);
        setLoading(false);
        const status = match?.status;
        if (status === 'building') {
          timer = setTimeout(fetchOnce, 8000);
        }
      } catch (e) {
        setLoading(false);
      }
    };
    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [ticker]);

  if (loading) return null;
  if (!row) return null;

  const meta = STATUS_META[row.status] || { label: row.status, cls: 'muted' };
  const title = [
    `Status: ${row.status}`,
    `Observations: ${row.observation_count}`,
    `Outcomes: ${row.outcome_count}`,
    `Cells: ${row.cell_count}`,
    row.last_built_at ? `Last built: ${row.last_built_at}` : null,
    row.error ? `Error: ${row.error}` : null,
  ].filter(Boolean).join('\n');

  return (
    <span className={`pill ${meta.cls}`} title={title}
                style={{ fontSize: 10.5 }}>
      {meta.label} · {row.observation_count}
    </span>
  );
}
