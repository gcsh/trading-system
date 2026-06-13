/**
 * GateStack — "Why didn't I trade?" diagnostic panel (P1.12).
 *
 * Reads /gates/stack to show the rejection mix in the last 24h.
 * On 2026-06-03 the adaptive calibration gate auto-tightened to A+
 * and rejected every signal as ``low_grade``. Without this surface,
 * diagnosing that required a database query.
 */
import React, { useEffect, useMemo, useState } from 'react';

const STATUS_COLOR = {
  submitted: 'var(--accent)',
  closed: 'var(--accent)',
  low_grade: '#ffd84d',
  already_held: '#5dc6ff',
  options_disabled: 'var(--muted)',
  event_hold: '#ff944d',
  rejected: 'var(--danger)',
  drift_halt: '#a98bff',
  meta_rejected: '#ff5d5d',
  failed: 'var(--danger)',
};

function colorFor(status) {
  return STATUS_COLOR[status] || 'var(--muted)';
}

function StatCard({ label, value, color, hint }) {
  return (
    <div className="panel" style={{ padding: '10px 14px', flex: 1, minWidth: 130 }}>
      <div style={{
        fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase',
        letterSpacing: '0.06em', fontWeight: 600,
      }}>{label}</div>
      <div style={{
        fontSize: 22, fontWeight: 700, color: color || 'var(--text)',
        fontFeatureSettings: '"tnum"', marginTop: 2,
      }}>{value}</div>
      {hint && (
        <div style={{ fontSize: 10, color: 'var(--muted-2)', marginTop: 4 }}>{hint}</div>
      )}
    </div>
  );
}


export default function GateStack() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [hours, setHours] = useState(24);

  useEffect(() => {
    let cancelled = false;
    fetch(`/gates/stack?hours=${hours}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(String(e)); });
    return () => { cancelled = true; };
  }, [hours]);

  const sortedRejections = useMemo(() => {
    if (!data?.rejection_counts) return [];
    return Object.entries(data.rejection_counts)
      .sort(([, a], [, b]) => b - a);
  }, [data]);

  if (err) return <div className="empty">gate-stack endpoint error: {err}</div>;
  if (!data) return <div className="empty">Loading gate diagnostics…</div>;

  const totalRej = sortedRejections.reduce((a, [, c]) => a + c, 0);

  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <StatCard label="Total decisions" value={data.total_decisions || 0}
                     hint={`last ${data.window_hours}h`} />
        <StatCard label="Submitted (live)" value={data.submitted || 0}
                     color="var(--accent)" />
        <StatCard label="Closed" value={data.closed || 0}
                     color="var(--accent)" />
        <StatCard label="Rejected" value={totalRej}
                     color={totalRej > 0 ? 'var(--danger)' : 'var(--muted)'}
                     hint={totalRej > 0 ? 'see breakdown' : 'nothing blocked'} />
        <div style={{ marginLeft: 'auto', alignSelf: 'center' }}>
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}
                  style={{
                    padding: '6px 10px', background: 'var(--panel)',
                    color: 'var(--text)', border: '1px solid var(--border)',
                    borderRadius: 6, fontSize: 12,
                  }}>
            <option value={1}>last 1h</option>
            <option value={6}>last 6h</option>
            <option value={24}>last 24h</option>
            <option value={72}>last 72h</option>
            <option value={168}>last 7d</option>
          </select>
        </div>
      </div>

      <div className="panel" style={{ padding: 14, marginBottom: 14 }}>
        <div style={{
          fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
          letterSpacing: '0.05em', fontWeight: 600, marginBottom: 8,
        }}>
          Rejection breakdown
        </div>
        {sortedRejections.length === 0 ? (
          <div className="empty" style={{ fontSize: 12, padding: 12 }}>
            No rejections in the last {data.window_hours}h — gates clean.
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 6 }}>
            {sortedRejections.map(([status, count]) => {
              const pct = totalRej > 0 ? (count / totalRej) * 100 : 0;
              const color = colorFor(status);
              return (
                <div key={status} style={{
                  display: 'grid', gridTemplateColumns: '160px 1fr 60px',
                  alignItems: 'center', gap: 8,
                }}>
                  <div style={{
                    fontSize: 12, fontWeight: 600, color,
                    textTransform: 'capitalize',
                  }}>{status.replace(/_/g, ' ')}</div>
                  <div style={{
                    height: 12, background: 'var(--panel-2)', borderRadius: 2,
                    overflow: 'hidden',
                  }}>
                    <div style={{
                      width: `${pct}%`, height: '100%', background: color,
                      opacity: 0.7,
                    }} />
                  </div>
                  <div style={{
                    fontSize: 13, fontWeight: 700, textAlign: 'right',
                    fontFeatureSettings: '"tnum"',
                  }}>{count}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="panel" style={{ padding: 14 }}>
        <div style={{
          fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
          letterSpacing: '0.05em', fontWeight: 600, marginBottom: 8,
        }}>
          Recent rejections (drill-down)
        </div>
        {(!data.recent_rejections || data.recent_rejections.length === 0) ? (
          <div className="empty" style={{ fontSize: 12, padding: 12 }}>—</div>
        ) : (
          <div style={{ maxHeight: 320, overflow: 'auto' }}>
            <table style={{
              width: '100%', borderCollapse: 'collapse',
              fontSize: 12, fontFeatureSettings: '"tnum"',
            }}>
              <thead>
                <tr style={{ color: 'var(--muted)' }}>
                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>Time</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>Ticker</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>Strategy</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>Action</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>Status</th>
                  <th style={{ textAlign: 'right', padding: '4px 8px' }}>Conf</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_rejections.map((r, i) => (
                  <tr key={i} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={{ padding: '4px 8px', color: 'var(--muted)' }}>
                      {r.timestamp ? r.timestamp.replace('T', ' ').slice(0, 19) : '—'}
                    </td>
                    <td style={{ padding: '4px 8px', fontWeight: 600 }}>{r.ticker}</td>
                    <td style={{ padding: '4px 8px' }}>{r.strategy || '—'}</td>
                    <td style={{ padding: '4px 8px' }}>{r.action}</td>
                    <td style={{ padding: '4px 8px', color: colorFor(r.status) }}>
                      {r.status}
                    </td>
                    <td style={{ padding: '4px 8px', textAlign: 'right' }}>
                      {(r.confidence != null ? r.confidence : 0).toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div style={{ marginTop: 10, fontSize: 10, color: 'var(--muted-2)' }}>
        Source: <code>/gates/stack</code> · live-only by default. Gate stack
        order in the engine: calendar → already_held → grade → drift_halt →
        IV_sanity → event_risk → options_disabled → risk → journal/curated →
        meta_AI → authority_spine.
      </div>
    </div>
  );
}
