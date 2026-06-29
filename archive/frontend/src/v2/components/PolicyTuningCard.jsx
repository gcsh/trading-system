/* MITS Phase 19 Cluster C — PolicyTuningCard.
 *
 * Renders one of /learning/policy-tuning's 8 tunable_rules. Always-
 * present "current" + optional "recommended" + confidence pill.
 * Approve / Rollback only enable when a recommended_value row exists
 * (otherwise everything is informational).
 *
 * The endpoint returns 2 separate shapes — see Step 0 in the task
 * brief:
 *
 *   tunable_rules[]  — the 8 rules in their static, default form
 *                       (always 8, regardless of data sufficiency)
 *   rows[]           — backend-computed recommendations (could be 0)
 *
 * This component takes the merged shape: { rule_meta, row|null }.
 */
import React, { useState } from 'react';
import { Pill } from '../../design/Components.jsx';

const POLICY_ENV = 'TB_POLICY_TUNING_AUTO_APPLY_ENABLED';

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
  return 'neutral'; // insufficient_data
}

export default function PolicyTuningCard({ ruleMeta, row, autoApply, onMutate }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const insufficient = !row || (row.recommendation_confidence === 'insufficient_data');
  const reviewed = row?.operator_reviewed === 1;
  const approved = row?.operator_approved === 1;
  const conf = row?.recommendation_confidence || 'insufficient_data';
  const recValue = row?.recommended_value;

  async function onApprove() {
    if (!row) return;
    setBusy(true); setErr(null);
    try {
      await apiPost('/learning/approve', { table: 'policy_tunings', row_id: row.id });
      if (onMutate) await onMutate();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  }
  async function onRollback() {
    if (!row) return;
    setBusy(true); setErr(null);
    try {
      await apiPost('/learning/rollback', { table: 'policy_tunings', row_id: row.id });
      if (onMutate) await onMutate();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="v2-card" style={{
      display: 'grid',
      gridTemplateColumns: 'minmax(180px,1.4fr) 1fr 1fr minmax(110px,auto) minmax(140px,auto)',
      gap: 12,
      alignItems: 'center',
      opacity: insufficient ? 0.55 : 1,
      borderColor: approved ? 'var(--accent-green-dim)' : 'var(--border-subtle)',
    }}>
      <div>
        <div style={{ fontWeight: 700, color: 'var(--text-primary)', fontSize: 13 }}>
          {ruleMeta.rule_name}
        </div>
        <div className="mono" style={{
          fontSize: 10.5, color: 'var(--text-tertiary)', marginTop: 2,
        }}>{ruleMeta.threshold_attr}</div>
        <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>
          {ruleMeta.description}
        </div>
      </div>
      <div>
        <div className="v2-stat__label">Current</div>
        <div className="mono" style={{ fontSize: 16, color: 'var(--text-primary)', fontWeight: 600 }}>
          {ruleMeta.current_value}
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{ruleMeta.units}</div>
      </div>
      <div>
        <div className="v2-stat__label">Recommended</div>
        <div className="mono" style={{
          fontSize: 16,
          color: recValue != null ? 'var(--accent-cyan)' : 'var(--text-muted)',
          fontWeight: 600,
        }}>
          {recValue != null ? recValue : '—'}
        </div>
        <Pill tone={toneFor(conf)}>{conf.replace('_', ' ')}</Pill>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
        {row?.rationale || ruleMeta.description}
        {reviewed && approved && (
          <div style={{ marginTop: 4 }}>
            <Pill tone={autoApply ? 'success' : 'warning'}>
              {autoApply ? 'ACTIVE' : 'QUEUED'}
            </Pill>
            {!autoApply && (
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
                will apply when <code>{POLICY_ENV}=1</code>
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
