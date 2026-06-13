/* MITS Phase 19 Cluster C — AuditRibbon.
 *
 * Last N operator approve / rollback actions from /learning/audit-log.
 * Filter chips: All / Approve / Rollback / Last 24h / Last 7d.
 * Paginated locally (10 per page) — backend supports limit= so we
 * fetch a window up-front and paginate client-side for simplicity.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Pill, EmptyState } from '../../design/Components.jsx';

async function api(path) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

function fmtAge(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  const ageSec = (Date.now() - ms) / 1000;
  if (ageSec < 60) return `${Math.round(ageSec)}s ago`;
  if (ageSec < 3600) return `${Math.round(ageSec / 60)}m ago`;
  if (ageSec < 86400) return `${Math.round(ageSec / 3600)}h ago`;
  return `${Math.round(ageSec / 86400)}d ago`;
}

const PAGE_SIZE = 10;
const FILTERS = [
  { key: 'all',      label: 'All' },
  { key: 'approve',  label: 'Approve' },
  { key: 'rollback', label: 'Rollback' },
  { key: '24h',      label: 'Last 24h' },
  { key: '7d',       label: 'Last 7d' },
];

export default function AuditRibbon({ refreshTick = 0 }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [filter, setFilter] = useState('all');
  const [page, setPage] = useState(1);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const j = await api('/learning/audit-log?limit=200');
        if (alive) { setData(j); setErr(null); }
      } catch (e) {
        if (alive) setErr(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, [refreshTick]);

  const filtered = useMemo(() => {
    const rows = data?.rows || [];
    const now = Date.now();
    return rows.filter(r => {
      if (filter === 'approve')  return r.action === 'approve';
      if (filter === 'rollback') return r.action === 'rollback';
      if (filter === '24h')      return (now - Date.parse(r.created_at)) <= 86400 * 1000;
      if (filter === '7d')       return (now - Date.parse(r.created_at)) <= 7 * 86400 * 1000;
      return true;
    });
  }, [data, filter]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
        {FILTERS.map(f => {
          const active = filter === f.key;
          return (
            <button
              key={f.key}
              onClick={() => { setFilter(f.key); setPage(1); }}
              style={{
                background: active ? 'var(--accent-cyan-dim)' : 'var(--bg-elevated)',
                color: active ? 'var(--bg-primary)' : 'var(--text-secondary)',
                border: '1px solid ' + (active ? 'var(--accent-cyan)' : 'var(--border-default)'),
                borderRadius: 999,
                padding: '4px 12px',
                fontSize: 11,
                fontWeight: 600,
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
                cursor: 'pointer',
              }}
            >{f.label}</button>
          );
        })}
        <span style={{
          marginLeft: 'auto', alignSelf: 'center',
          fontSize: 11, color: 'var(--text-tertiary)',
        }}>
          {filtered.length} entries
        </span>
      </div>
      {err && (
        <div style={{ color: 'var(--accent-red)', fontSize: 12, padding: 6 }}>{err}</div>
      )}
      {filtered.length === 0 && !err && (
        <EmptyState icon="∅" message="No operator audit entries match this filter." />
      )}
      {filtered.length > 0 && (
        <>
          <div style={{ overflowX: 'auto' }}>
            <table className="v2-table v2-table--striped" style={{ minWidth: 720 }}>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Table</th>
                  <th style={{ textAlign: 'right' }}>Row</th>
                  <th>Operator</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map(r => (
                  <tr key={r.id}>
                    <td className="mono" style={{ fontSize: 11 }}>
                      <div>{fmtAge(r.created_at)}</div>
                      <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>{r.created_at}</div>
                    </td>
                    <td>
                      <Pill tone={r.action === 'approve' ? 'success' : 'warning'}>
                        {String(r.action || '').toUpperCase()}
                      </Pill>
                    </td>
                    <td className="mono" style={{ fontSize: 11 }}>{r.table_name}</td>
                    <td className="mono" style={{ textAlign: 'right' }}>#{r.row_id}</td>
                    <td className="mono" style={{ fontSize: 11 }}>{r.operator || 'operator'}</td>
                    <td style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                      {r.notes || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {totalPages > 1 && (
            <div style={{
              marginTop: 8,
              display: 'flex',
              gap: 6,
              alignItems: 'center',
              justifyContent: 'flex-end',
              fontSize: 11,
              color: 'var(--text-tertiary)',
            }}>
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                style={{
                  background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
                  border: '1px solid var(--border-default)', borderRadius: 4,
                  padding: '3px 10px', cursor: page === 1 ? 'not-allowed' : 'pointer',
                  opacity: page === 1 ? 0.4 : 1,
                }}
              >‹ Prev</button>
              <span>Page {page} / {totalPages}</span>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                style={{
                  background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
                  border: '1px solid var(--border-default)', borderRadius: 4,
                  padding: '3px 10px', cursor: page === totalPages ? 'not-allowed' : 'pointer',
                  opacity: page === totalPages ? 0.4 : 1,
                }}
              >Next ›</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
