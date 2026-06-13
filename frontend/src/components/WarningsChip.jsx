/**
 * WarningsChip — surfaces recent WARNING + ERROR log records on the
 * Authority Spine. Operator clicks it → drill-down modal with the
 * last N records, their context, and a "clear" button.
 *
 * Backend: /system/warnings, /system/warnings/clear
 */
import React, { useCallback, useEffect, useState } from 'react';

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const LEVEL_PILL = {
  WARNING:  'pill warn',
  ERROR:    'pill danger',
  CRITICAL: 'pill danger',
};

function timeAgo(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!t) return '—';
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export default function WarningsChip({ compact = false }) {
  const [counts, setCounts] = useState(null);
  const [records, setRecords] = useState([]);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api('/system/warnings?limit=100');
      setRecords(r.records || []);
      setCounts(r.counts || null);
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 6000);
    return () => clearInterval(id);
  }, [load]);

  const clear = async () => {
    try { await api('/system/warnings/clear', { method: 'POST' }); await load(); }
    catch { /* silent */ }
  };

  if (!counts) return null;
  const total = counts.total || 0;
  const errors = counts.ERROR + counts.CRITICAL;
  const warnings = counts.WARNING;
  // Choose visual treatment based on severity
  const cls = errors > 0 ? 'pill danger' : warnings > 0 ? 'pill warn' : 'pill on';
  const label = total === 0
    ? 'all clear'
    : (errors > 0 ? `${errors} error${errors === 1 ? '' : 's'}` : `${warnings} warning${warnings === 1 ? '' : 's'}`);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className={cls}
        style={{
          border: 'none', cursor: 'pointer',
          fontSize: 11, padding: '3px 9px', fontWeight: 600,
        }}
        title={`${counts.WARNING || 0} warnings · ${counts.ERROR || 0} errors`}
      >
        {total > 0 ? '⚠ ' : '✓ '}{label}
      </button>

      {open && (
        <div
          onClick={() => setOpen(false)}
          style={{
            position: 'fixed', inset: 0,
            background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(4px)',
            display: 'grid', placeItems: 'center', zIndex: 100, padding: 24,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="panel"
            style={{ maxWidth: 900, width: '100%', maxHeight: '85vh', overflow: 'auto' }}
          >
            <div className="panel-head">
              <div>
                <div style={{
                  fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
                  color: 'var(--muted)', fontWeight: 600,
                }}>System warnings · ring buffer (most recent 200)</div>
                <h2 style={{ margin: '4px 0 0' }}>
                  {records.length === 0
                    ? 'No warnings · clean run'
                    : `${records.length} record${records.length === 1 ? '' : 's'}`}
                </h2>
              </div>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn small" onClick={clear} disabled={!records.length}>
                  Clear buffer
                </button>
                <button className="btn small" onClick={() => setOpen(false)}>Close</button>
              </div>
            </div>

            {!records.length ? (
              <div className="empty">
                <div className="title">All clear ✓</div>
                <div className="hint">No WARNING / ERROR / CRITICAL records since startup or last clear.</div>
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 8 }}>
                {records.map((r, i) => (
                  <div key={i} style={{
                    padding: '10px 12px',
                    background: 'var(--panel-2)',
                    border: `1px solid var(--border)`,
                    borderRadius: 8,
                    borderLeft: `3px solid ${r.level === 'ERROR' || r.level === 'CRITICAL' ? 'var(--danger)' : 'var(--warn)'}`,
                  }}>
                    <div className="row" style={{ gap: 8, marginBottom: 4 }}>
                      <span className={LEVEL_PILL[r.level] || 'pill off'}>{r.level}</span>
                      <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                        {timeAgo(r.timestamp)}
                      </span>
                      <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                        {r.path}:{r.line}
                      </span>
                      <span style={{ fontSize: 11, color: 'var(--muted-2)' }}>
                        {r.logger}
                      </span>
                    </div>
                    <div style={{ fontSize: 13, color: 'var(--text)', wordBreak: 'break-word' }}>
                      {r.message}
                    </div>
                    {r.exc_type && (
                      <div style={{
                        marginTop: 4, fontSize: 11, color: 'var(--danger-2)',
                        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
                        wordBreak: 'break-word',
                      }}>
                        {r.exc_type}: {r.exc_summary}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
