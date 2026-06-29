/* MITS Phase 19 Cluster C — AttributionTable.
 *
 * Sortable + insufficient-sample-dimmed table for /learning/attribution/*
 * rows. One component, three tabs (agents / axes / strategies) drive the
 * same shape because the backend keys all returns to scope_name / n_closed
 * / hit_rate / mean_pnl_pct / brier_score / ece / spearman_corr / notes.
 *
 * Wilson-CI bar shows hit_rate_wilson_lower → hit_rate_wilson_upper as a
 * teal mini-bar inside the cell for fast visual scanning.
 *
 * Approve / Rollback buttons POST to /learning/approve|rollback with
 *   { table: 'learned_attribution', row_id: row.id }
 * exactly matching the legacy contract.
 */
import React, { useMemo, useState } from 'react';
import { Pill, EmptyState } from '../../design/Components.jsx';

const TT = {
  hit_rate: 'Hit rate: percent of closed positions that ended in profit. Higher = the agent picks winners.',
  wilson:   'Wilson 95% confidence interval — narrow = trustworthy, wide = small sample.',
  mean_pnl: 'Average realized P&L percent across closed positions for this scope.',
  brier:    'Brier score: mean squared error of confidence vs outcome. Lower is better. 0.25 = random.',
  ece:      'Expected Calibration Error: average gap between stated confidence and actual win rate. Lower = better-calibrated.',
  spearman: 'Spearman correlation: does this axis sort trades by realized P&L? > 0.2 = real signal.',
  n_closed: 'Number of closed trades that contributed to this row. Watch min_n guardrail.',
};

function pct(v, digits = 1) {
  if (v == null || !isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}
function num(v, digits = 3) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toFixed(digits);
}

function WilsonBar({ lo, hi }) {
  if (lo == null || hi == null) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>;
  const left = Math.max(0, Math.min(1, lo)) * 100;
  const width = Math.max(2, (hi - lo) * 100);
  return (
    <div
      title={`${(lo * 100).toFixed(0)}–${(hi * 100).toFixed(0)}%`}
      style={{
        position: 'relative',
        width: 80,
        height: 6,
        background: 'var(--bg-secondary)',
        borderRadius: 3,
        overflow: 'hidden',
      }}
    >
      <div style={{
        position: 'absolute',
        left: `${left}%`,
        width: `${Math.min(100 - left, width)}%`,
        height: '100%',
        background: 'var(--accent-cyan-dim)',
        borderRadius: 3,
      }} />
    </div>
  );
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error(`${path} -> ${r.status} ${t.slice(0, 200)}`);
  }
  return r.json();
}

export default function AttributionTable({ rows = [], onMutate }) {
  const [sortKey, setSortKey] = useState('n_closed');
  const [sortDir, setSortDir] = useState('desc');
  const [busyId, setBusyId] = useState(null);
  const [err, setErr] = useState(null);

  function setSort(key) {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  const sorted = useMemo(() => {
    const copy = (rows || []).slice();
    copy.sort((a, b) => {
      const va = a[sortKey];
      const vb = b[sortKey];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'string') return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
      return sortDir === 'asc' ? va - vb : vb - va;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  async function onApprove(row) {
    setBusyId(row.id); setErr(null);
    try {
      await apiPost('/learning/approve', { table: 'learned_attribution', row_id: row.id });
      if (onMutate) await onMutate();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusyId(null); }
  }
  async function onRollback(row) {
    setBusyId(row.id); setErr(null);
    try {
      await apiPost('/learning/rollback', { table: 'learned_attribution', row_id: row.id });
      if (onMutate) await onMutate();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusyId(null); }
  }

  if (!rows || rows.length === 0) {
    return <EmptyState icon="∅" message="No attribution rows yet — the nightly writer hasn't populated this scope." />;
  }

  function Th({ k, label, align = 'left', tip }) {
    const active = sortKey === k;
    const arrow = active ? (sortDir === 'asc' ? ' ▲' : ' ▼') : '';
    return (
      <th
        onClick={() => setSort(k)}
        title={tip || ''}
        style={{ cursor: 'pointer', textAlign: align, userSelect: 'none' }}
      >
        {label}{arrow}
      </th>
    );
  }

  return (
    <div>
      {err && (
        <div style={{ color: 'var(--accent-red)', fontSize: 12, padding: 6 }}>
          {err}
        </div>
      )}
      <div style={{ overflowX: 'auto' }}>
        <table className="v2-table v2-table--striped" style={{ minWidth: 880 }}>
          <thead>
            <tr>
              <Th k="scope_name" label="Scope" />
              <Th k="n_closed" label="N" align="right" tip={TT.n_closed} />
              <Th k="hit_rate" label="Hit Rate" align="right" tip={TT.hit_rate} />
              <th style={{ textAlign: 'left' }} title={TT.wilson}>Wilson CI</th>
              <Th k="mean_pnl_pct" label="Mean P&L %" align="right" tip={TT.mean_pnl} />
              <Th k="brier_score" label="Brier" align="right" tip={TT.brier} />
              <Th k="ece" label="ECE" align="right" tip={TT.ece} />
              <Th k="spearman_corr" label="Spearman ρ" align="right" tip={TT.spearman} />
              <th style={{ textAlign: 'left' }}>Notes</th>
              <th style={{ textAlign: 'left' }}>Review</th>
              <th style={{ textAlign: 'left' }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map(r => {
              const insufficient = (r.notes || '').includes('insufficient');
              const reviewed = r.operator_reviewed === 1;
              const approved = r.operator_approved === 1;
              return (
                <tr key={r.id} style={{ opacity: insufficient ? 0.5 : 1 }}>
                  <td className="mono">{r.scope_name}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{r.n_closed ?? 0}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{pct(r.hit_rate, 1)}</td>
                  <td><WilsonBar lo={r.hit_rate_wilson_lower} hi={r.hit_rate_wilson_upper} /></td>
                  <td className="mono" style={{ textAlign: 'right' }}>
                    {r.mean_pnl_pct == null ? '—' : `${Number(r.mean_pnl_pct).toFixed(2)}%`}
                  </td>
                  <td className="mono" style={{ textAlign: 'right' }}>{num(r.brier_score, 3)}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{num(r.ece, 3)}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{num(r.spearman_corr, 3)}</td>
                  <td style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                    {r.notes || '—'}
                  </td>
                  <td>
                    {reviewed && (
                      <Pill tone={approved ? 'success' : 'warning'}>
                        {approved ? 'APPROVED' : 'ROLLED BACK'}
                      </Pill>
                    )}
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button
                      onClick={() => onApprove(r)}
                      disabled={insufficient || busyId === r.id}
                      title={insufficient ? 'insufficient_sample_size — cannot approve' : 'mark operator_approved=1'}
                      style={{
                        background: 'var(--bg-elevated)',
                        color: 'var(--accent-green)',
                        border: '1px solid var(--accent-green-dim)',
                        borderRadius: 4,
                        padding: '3px 8px',
                        fontSize: 11,
                        marginRight: 4,
                        cursor: insufficient ? 'not-allowed' : 'pointer',
                        opacity: insufficient ? 0.4 : 1,
                      }}
                    >Approve</button>
                    <button
                      onClick={() => onRollback(r)}
                      disabled={!reviewed || busyId === r.id}
                      title="mark operator_approved=0 + write audit row"
                      style={{
                        background: 'var(--bg-elevated)',
                        color: 'var(--accent-red)',
                        border: '1px solid var(--accent-red-dim)',
                        borderRadius: 4,
                        padding: '3px 8px',
                        fontSize: 11,
                        cursor: !reviewed ? 'not-allowed' : 'pointer',
                        opacity: !reviewed ? 0.4 : 1,
                      }}
                    >Rollback</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
