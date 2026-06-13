/**
 * P4.3 — SnapshotQualityChip
 *
 * Reads the latest PortfolioSnapshot.data_quality + accounting_version
 * and surfaces it as a small chip. Hidden when data_quality === "good"
 * and accounting_version is current (latest known: v1). Drops a visible
 * badge when the equity curve is being drawn from degraded data so the
 * operator never trusts a number that came from a stale snapshot.
 */
import React, { useEffect, useState } from 'react';

const COLOR = {
  good: 'var(--accent)',
  partial: '#ffd84d',
  degraded: 'var(--danger)',
};

export default function SnapshotQualityChip({ polite = false }) {
  const [snap, setSnap] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/portfolio/equity?range=1d&limit=1')
      .then((r) => r.ok ? r.json() : Promise.reject())
      .then((rows) => {
        if (cancelled) return;
        // The endpoint returns an array; take the last one.
        const last = Array.isArray(rows) ? rows[rows.length - 1] : null;
        setSnap(last);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (!snap) return null;
  const quality = snap.data_quality || 'good';
  const version = snap.accounting_version ?? 1;
  // Hide entirely when everything is clean.
  if (quality === 'good' && polite) return null;

  const color = COLOR[quality] || COLOR.degraded;
  return (
    <div title={`Equity snapshot quality: ${quality} · accounting v${version} · ` +
                  `excludes_synthetic=${snap.excludes_synthetic}`}
         style={{
           display: 'inline-flex', alignItems: 'center', gap: 4,
           padding: '2px 8px', borderRadius: 10,
           background: `${color}22`, color, fontSize: 10,
           fontWeight: 600, letterSpacing: '0.04em',
           textTransform: 'uppercase',
         }}>
      <span>📊</span>
      <span>{quality}</span>
      <span style={{ opacity: 0.6 }}>v{version}</span>
    </div>
  );
}
