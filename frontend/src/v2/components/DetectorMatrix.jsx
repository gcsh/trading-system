/* MITS Phase 19 Cluster C — DetectorMatrix.
 *
 * Cell color = win_rate_5d (red→neutral→green). Opacity scales with
 * log(sample_size) so the operator gets a sense of statistical weight
 * at a glance.
 *
 * Rows = detector name; cols = direction buckets (long / neutral /
 * short / null). The /detectors/edge endpoint returns one row per
 * detector with a `direction` (and the underlying detector may have
 * multiple directions if it's symmetric — we collapse to the row's
 * declared direction).
 *
 * Click a row → bubbles `onSelect(detector)` to parent for drill-in.
 */
import React, { useMemo, useState } from 'react';
import { EmptyState } from '../../design/Components.jsx';

const DIRS = ['long', 'neutral', 'short', 'null'];

function colorFor(winRate, baseline) {
  // Edge in percentage points
  if (winRate == null) return 'transparent';
  const edge = (winRate - (baseline || 0.5)) * 100;
  if (edge >= 5)  return `rgba(0, 255, 136, ${0.20 + Math.min(0.6, edge / 60)})`;
  if (edge >= 0)  return `rgba(0, 212, 255, ${0.15 + Math.min(0.45, edge / 30)})`;
  if (edge > -2)  return 'rgba(148, 163, 184, 0.18)';
  return `rgba(255, 51, 85, ${0.20 + Math.min(0.6, Math.abs(edge) / 60)})`;
}

function opacityFor(n) {
  if (!n || n <= 0) return 0.35;
  // log10 squashing — 1 → 0.45, 10 → 0.62, 100 → 0.78, 1000+ → 1.0
  return Math.min(1, 0.4 + Math.log10(n) * 0.15);
}

export default function DetectorMatrix({ detectors = [], familyFilter = 'all', onSelect, baselines }) {
  const [hover, setHover] = useState(null);

  const filtered = useMemo(() => {
    if (familyFilter === 'all') return detectors;
    return (detectors || []).filter(d => d.family === familyFilter);
  }, [detectors, familyFilter]);

  // Group by detector name → { long?, neutral?, short?, null? }
  const grouped = useMemo(() => {
    const map = new Map();
    for (const d of filtered) {
      if (!map.has(d.name)) map.set(d.name, { name: d.name, family: d.family, cells: {} });
      map.get(d.name).cells[d.direction || 'null'] = d;
    }
    return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name));
  }, [filtered]);

  if (grouped.length === 0) {
    return <EmptyState icon="∅" message="No detectors match this filter." />;
  }

  return (
    <div className="v2-card" style={{ padding: 0, overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'separate', borderSpacing: 0, minWidth: 720 }}>
        <thead>
          <tr style={{ background: 'var(--bg-secondary)' }}>
            <th style={{
              position: 'sticky', left: 0, background: 'var(--bg-secondary)',
              textAlign: 'left', padding: '8px 10px',
              fontSize: 11, color: 'var(--text-tertiary)',
              textTransform: 'uppercase', letterSpacing: '0.06em',
              borderBottom: '1px solid var(--border-subtle)',
              minWidth: 200,
            }}>Detector</th>
            <th style={{
              padding: '8px 10px', fontSize: 11, color: 'var(--text-tertiary)',
              textTransform: 'uppercase', letterSpacing: '0.06em',
              borderBottom: '1px solid var(--border-subtle)',
            }}>Family</th>
            {DIRS.map(dir => (
              <th key={dir} style={{
                padding: '8px 10px', fontSize: 11, color: 'var(--text-tertiary)',
                textTransform: 'uppercase', letterSpacing: '0.06em',
                borderBottom: '1px solid var(--border-subtle)',
                textAlign: 'center',
              }}>{dir}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grouped.map(g => (
            <tr key={g.name}
                onMouseEnter={() => setHover(g.name)}
                onMouseLeave={() => setHover(null)}
                onClick={() => onSelect && onSelect(g)}
                style={{
                  cursor: 'pointer',
                  background: hover === g.name ? 'var(--bg-elevated)' : 'transparent',
                }}>
              <td className="mono" style={{
                position: 'sticky', left: 0,
                background: hover === g.name ? 'var(--bg-elevated)' : 'var(--bg-tertiary)',
                padding: '6px 10px', fontSize: 12, color: 'var(--text-primary)',
                borderBottom: '1px solid var(--border-subtle)',
              }}>{g.name}</td>
              <td style={{
                padding: '6px 10px', fontSize: 11, color: 'var(--text-tertiary)',
                borderBottom: '1px solid var(--border-subtle)',
              }}>{g.family}</td>
              {DIRS.map(dir => {
                const cell = g.cells[dir];
                if (!cell) {
                  return <td key={dir} style={{
                    padding: 4, borderBottom: '1px solid var(--border-subtle)',
                  }}>
                    <div style={{
                      background: 'var(--bg-secondary)', height: 38,
                      borderRadius: 4, display: 'flex',
                      alignItems: 'center', justifyContent: 'center',
                      color: 'var(--text-muted)', fontSize: 10,
                    }}>—</div>
                  </td>;
                }
                const base = baselines?.[dir] || baselines?.null || 0.5;
                const bg = colorFor(cell.win_rate_5d, base);
                const op = opacityFor(cell.sample_size);
                const edge = cell.edge_pp_vs_baseline;
                return (
                  <td key={dir} style={{
                    padding: 4, borderBottom: '1px solid var(--border-subtle)',
                  }}>
                    <div
                      title={`${cell.name} · ${dir} · win_rate=${(cell.win_rate_5d * 100).toFixed(1)}% (n=${cell.sample_size}) · edge=${edge?.toFixed(2) || '—'}pp`}
                      style={{
                        background: bg,
                        opacity: op,
                        height: 38,
                        borderRadius: 4,
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontSize: 11,
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--text-primary)',
                      }}>
                      <div>{cell.win_rate_5d != null ? `${(cell.win_rate_5d * 100).toFixed(0)}%` : '—'}</div>
                      <div style={{ fontSize: 9, opacity: 0.8 }}>n={cell.sample_size}</div>
                    </div>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
