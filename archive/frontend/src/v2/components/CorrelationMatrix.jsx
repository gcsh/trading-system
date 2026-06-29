/* MITS Phase 19 Cluster D — CorrelationMatrix component.
 *
 * NxN heatmap of pairwise correlations between portfolio holdings.
 *
 * Source: /portfolio/context → pairwise_correlation { TKR: { TKR: ρ, … } }
 *
 * Diverging red(-1) → grey(0) → green(+1) palette.
 *
 * Props:
 *   matrix:   { TKR: { TKR: ρ, … } } from /portfolio/context.pairwise_correlation
 *   tickers:  optional ordered subset; default is all keys
 *   maxN:     cap the matrix at the top N tickers (default 10)
 */
import React, { useMemo } from 'react';
import { EmptyState } from '../../design/Components.jsx';

function corrColor(v) {
  if (v == null || !isFinite(v)) return 'rgba(148,163,184,0.08)';
  if (Math.abs(v) < 0.05) return 'rgba(148,163,184,0.10)';
  if (v > 0) {
    const t = Math.min(1, v);
    return `rgba(0, 255, 136, ${0.10 + 0.65 * t})`;
  }
  const t = Math.min(1, -v);
  return `rgba(255, 51, 85, ${0.10 + 0.65 * t})`;
}

export default function CorrelationMatrix({
  matrix = {},
  tickers,
  maxN = 10,
}) {
  const list = useMemo(() => {
    if (!matrix || typeof matrix !== 'object') return [];
    const keys = tickers && tickers.length ? tickers : Object.keys(matrix);
    return keys.slice(0, maxN);
  }, [matrix, tickers, maxN]);

  if (list.length < 2) {
    return (
      <EmptyState
        icon="⊞"
        message="Need at least 2 holdings with correlation data."
      />
    );
  }

  return (
    <div className="v2-corr">
      <table className="v2-corr__tbl">
        <thead>
          <tr>
            <th />
            {list.map(t => (
              <th key={t}><span className="mono">{t}</span></th>
            ))}
          </tr>
        </thead>
        <tbody>
          {list.map(r => (
            <tr key={r}>
              <th><span className="mono">{r}</span></th>
              {list.map(c => {
                let v = null;
                if (r === c) v = 1.0;
                else if (matrix[r] && typeof matrix[r][c] === 'number') v = matrix[r][c];
                else if (matrix[c] && typeof matrix[c][r] === 'number') v = matrix[c][r];
                return (
                  <td key={c}
                      style={{ background: corrColor(v) }}
                      title={`${r} × ${c}: ${v != null ? v.toFixed(2) : '—'}`}>
                    {v != null ? v.toFixed(2) : ''}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="v2-corr__legend">
        <span style={{ background: 'rgba(255,51,85,0.55)' }}>-1.0</span>
        <span style={{ background: 'rgba(255,51,85,0.25)' }}>-0.5</span>
        <span style={{ background: 'rgba(148,163,184,0.18)' }}>0</span>
        <span style={{ background: 'rgba(0,255,136,0.25)' }}>+0.5</span>
        <span style={{ background: 'rgba(0,255,136,0.55)' }}>+1.0</span>
      </div>
      <style>{`
        .v2-corr { width: 100%; overflow: auto; }
        .v2-corr__tbl {
          border-collapse: separate; border-spacing: 2px;
          font-size: 11px;
          font-family: var(--font-mono);
          margin: 0 auto;
        }
        .v2-corr__tbl th,
        .v2-corr__tbl td {
          padding: 6px 8px;
          text-align: center;
          color: var(--text-primary);
          min-width: 46px;
        }
        .v2-corr__tbl thead th {
          font-size: 10px;
          font-weight: 700;
          color: var(--text-tertiary);
          letter-spacing: 0.04em;
          text-transform: uppercase;
          background: transparent;
          border-bottom: 1px solid var(--border-subtle);
        }
        .v2-corr__tbl tbody th {
          font-size: 10px;
          font-weight: 700;
          color: var(--text-tertiary);
          letter-spacing: 0.04em;
          text-align: right;
          padding-right: 8px;
        }
        .v2-corr__tbl tbody td {
          border-radius: 4px;
        }
        .v2-corr__legend {
          display: flex; gap: 6px; justify-content: center;
          padding: 8px 0 0;
          font-size: 10px;
          color: var(--text-tertiary);
          font-family: var(--font-mono);
        }
        .v2-corr__legend > span {
          padding: 4px 10px;
          border-radius: 4px;
          color: var(--text-primary);
        }
      `}</style>
    </div>
  );
}
