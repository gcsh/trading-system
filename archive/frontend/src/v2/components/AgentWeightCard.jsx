/* MITS Phase 19 Cluster C — AgentWeightCard.
 *
 * One row per council agent. Shows base_weight (always 1.0 today),
 * Bayesian-shrunk proposed multiplier, confidence pill, rationale +
 * Approve/Rollback. Mirrors PolicyTuningCard layout but for the
 * adaptive_weights table.
 */
import React, { useState } from 'react';
import { Pill } from '../../design/Components.jsx';

const WEIGHTS_ENV = 'TB_ADAPTIVE_WEIGHTS_APPLY_ENABLED';

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
function toneFor(conf) {
  if (conf === 'high') return 'success';
  if (conf === 'medium') return 'warning';
  if (conf === 'low') return 'neutral';
  return 'neutral';
}

export default function AgentWeightCard({ agentName, baseWeight = 1.0, row, applyEnabled, onMutate }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const insufficient = !row || (row.recommendation_confidence === 'insufficient_data');
  const reviewed = row?.operator_reviewed === 1;
  const approved = row?.operator_approved === 1;
  const conf = row?.recommendation_confidence || 'insufficient_data';
  const proposed = row?.weight_proposed;

  async function onApprove() {
    if (!row) return;
    setBusy(true); setErr(null);
    try {
      await apiPost('/learning/approve', { table: 'adaptive_weights', row_id: row.id });
      if (onMutate) await onMutate();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  }
  async function onRollback() {
    if (!row) return;
    setBusy(true); setErr(null);
    try {
      await apiPost('/learning/rollback', { table: 'adaptive_weights', row_id: row.id });
      if (onMutate) await onMutate();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="v2-card" style={{
      display: 'grid',
      gridTemplateColumns: 'minmax(140px,1fr) 1fr 1fr minmax(110px,auto) minmax(140px,auto)',
      gap: 12,
      alignItems: 'center',
      opacity: insufficient ? 0.55 : 1,
      borderColor: approved ? 'var(--accent-green-dim)' : 'var(--border-subtle)',
    }}>
      <div>
        <div style={{ fontWeight: 700, color: 'var(--text-primary)', fontSize: 13 }}>
          {agentName}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 2 }}>
          Council agent
        </div>
      </div>
      <div>
        <div className="v2-stat__label">Base weight</div>
        <div className="mono" style={{ fontSize: 16, color: 'var(--text-primary)', fontWeight: 600 }}>
          {baseWeight.toFixed(3)}
        </div>
      </div>
      <div>
        <div className="v2-stat__label">Proposed</div>
        <div className="mono" style={{
          fontSize: 16,
          color: proposed != null ? 'var(--accent-purple)' : 'var(--text-muted)',
          fontWeight: 600,
        }}>
          {proposed != null ? Number(proposed).toFixed(3) : '—'}
        </div>
        <Pill tone={toneFor(conf)}>{conf.replace('_', ' ')}</Pill>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
        {row?.rationale || 'Awaiting closed-trade evidence to estimate calibration vs base.'}
        {reviewed && approved && (
          <div style={{ marginTop: 4 }}>
            <Pill tone={applyEnabled ? 'success' : 'warning'}>
              {applyEnabled ? 'ACTIVE' : 'QUEUED'}
            </Pill>
            {!applyEnabled && (
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
                will apply when <code>{WEIGHTS_ENV}=1</code>
              </div>
            )}
          </div>
        )}
        {reviewed && !approved && (
          <div style={{ marginTop: 4 }}>
            <Pill tone="warning">ROLLED BACK</Pill>
          </div>
        )}
      </div>
      <div style={{ whiteSpace: 'nowrap' }}>
        <button
          onClick={onApprove}
          disabled={insufficient || approved || busy}
          title={insufficient ? 'insufficient_data — cannot approve' : 'mark operator_approved=1'}
          style={{
            background: 'var(--bg-elevated)',
            color: 'var(--accent-green)',
            border: '1px solid var(--accent-green-dim)',
            borderRadius: 4,
            padding: '4px 10px',
            fontSize: 11,
            marginRight: 4,
            cursor: insufficient || approved ? 'not-allowed' : 'pointer',
            opacity: insufficient || approved ? 0.4 : 1,
          }}
        >Approve</button>
        <button
          onClick={onRollback}
          disabled={insufficient || !reviewed || busy}
          title="un-approve + write audit row"
          style={{
            background: 'var(--bg-elevated)',
            color: 'var(--accent-red)',
            border: '1px solid var(--accent-red-dim)',
            borderRadius: 4,
            padding: '4px 10px',
            fontSize: 11,
            cursor: insufficient || !reviewed ? 'not-allowed' : 'pointer',
            opacity: insufficient || !reviewed ? 0.4 : 1,
          }}
        >Rollback</button>
        {err && (
          <div style={{ color: 'var(--accent-red)', fontSize: 10, marginTop: 4 }}>{err}</div>
        )}
      </div>
    </div>
  );
}
