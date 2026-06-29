/**
 * MITS Phase 18.E — Hypothesis Studio.
 *
 * Single operator console for the 4 learning surfaces shipped in 18.A
 * through 18.D, with approve/rollback guardrails on top:
 *
 *   1. What's Working           — per-agent/axis/strategy calibration
 *   2. What If?                  — counterfactual replayer drill-in
 *   3. Policy Tuning Advisor     — 8 tunable rules + approve/rollback
 *   4. Weight Adaptation         — 8 council agents + approve/rollback
 *
 * Every approve/rollback action POSTs to /learning/approve or
 * /learning/rollback which sets operator_reviewed=1 + flips
 * operator_approved AND appends an audit row to learning_rollback_log.
 *
 * NOTHING in this page auto-applies. Recommendations stay advisory
 * until the operator flips the relevant env-var flag on the box; the
 * studio surfaces the 5 flag states at the top so it's obvious which
 * adaptive layer is or isn't live.
 *
 * Styling mirrors DecisionScorecard.jsx — same panel/table shell so
 * the look matches the rest of the cockpit pages.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
// MITS Phase 18-FU Gap 8 — proper modal form replaces 4 window.prompt()
// call-sites for the Policy and Consensus counterfactual recompute paths.
// The sizing CF was already prompt-free (uses default factor list); we
// route it through the modal too so the operator sees the factor curve
// inline without leaving the page.
import WhatIfModal from '../components/WhatIfModal.jsx';

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch (e) {
      detail = await res.text().catch(() => '');
    }
    throw new Error(`${path} -> ${res.status} ${detail}`);
  }
  return res.json();
}

// ── Shared shell primitives — copy DecisionScorecard look-and-feel ──

function Panel({ title, subtitle, right, children, collapsible }) {
  // Collapsible: persist open/closed in localStorage per title so the
  // operator's "always-collapsed" preferences survive a refresh.
  const storageKey = `hs-panel-${title}`;
  const initial = collapsible
    ? localStorage.getItem(storageKey) !== '0'
    : true;
  const [open, setOpen] = useState(initial);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (collapsible) localStorage.setItem(storageKey, next ? '1' : '0');
  };
  return (
    <div style={{
      background: '#111827', borderRadius: 8,
      border: '1px solid #1f2937', marginBottom: 16,
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'baseline',
        padding: '12px 14px',
        borderBottom: open ? '1px solid #1f2937' : 'none',
        cursor: collapsible ? 'pointer' : 'default',
      }} onClick={collapsible ? toggle : undefined}>
        <div>
          <div style={{ fontSize: 14, color: '#e5e7eb', fontWeight: 600 }}>
            {collapsible && (
              <span style={{ marginRight: 6, color: '#6b7280' }}>
                {open ? '▾' : '▸'}
              </span>
            )}
            {title}
          </div>
          {subtitle && (
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>
              {subtitle}
            </div>
          )}
        </div>
        {right}
      </div>
      {open && (
        <div style={{ padding: 12 }}>
          {children}
        </div>
      )}
    </div>
  );
}

function Pill({ tone = 'off', children, title }) {
  const palette = {
    on:   { bg: '#064e3b', fg: '#6ee7b7', bd: '#10b981' },
    off:  { bg: '#1f2937', fg: '#9ca3af', bd: '#374151' },
    warn: { bg: '#78350f', fg: '#fbbf24', bd: '#f59e0b' },
    err:  { bg: '#7f1d1d', fg: '#fca5a5', bd: '#ef4444' },
  }[tone] || { bg: '#1f2937', fg: '#9ca3af', bd: '#374151' };
  return (
    <span title={title} style={{
      display: 'inline-block',
      padding: '2px 8px', borderRadius: 999,
      background: palette.bg, color: palette.fg,
      border: `1px solid ${palette.bd}`,
      fontSize: 10.5, fontWeight: 600,
      letterSpacing: '0.03em',
    }}>{children}</span>
  );
}

function ActionButton({ onClick, disabled, tone = 'primary', children, title }) {
  const palette = {
    primary: { bg: '#1e3a8a', fg: '#93c5fd', bd: '#3b82f6' },
    danger:  { bg: '#7f1d1d', fg: '#fca5a5', bd: '#ef4444' },
    neutral: { bg: '#1f2937', fg: '#d1d5db', bd: '#374151' },
  }[tone] || { bg: '#1f2937', fg: '#d1d5db', bd: '#374151' };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        background: disabled ? '#0a0a0a' : palette.bg,
        color: disabled ? '#4b5563' : palette.fg,
        border: `1px solid ${disabled ? '#1f2937' : palette.bd}`,
        borderRadius: 6,
        padding: '4px 10px', fontSize: 11.5,
        cursor: disabled ? 'not-allowed' : 'pointer',
        marginRight: 4, fontWeight: 600,
      }}
    >{children}</button>
  );
}

// ── Section 1 — Attribution / "What's Working" ─────────────────────

function AttributionRow({ row, refresh }) {
  const insufficient = (row.notes || '').includes('insufficient_sample_size');
  const reviewed = row.operator_reviewed === 1;
  const approved = row.operator_approved === 1;
  const onApprove = async () => {
    try {
      await api('/learning/approve', {
        method: 'POST',
        body: JSON.stringify({
          table: 'learned_attribution', row_id: row.id,
        }),
      });
      await refresh();
    } catch (e) { window.alert(`Approve failed: ${e.message}`); }
  };
  const onRollback = async () => {
    try {
      await api('/learning/rollback', {
        method: 'POST',
        body: JSON.stringify({
          table: 'learned_attribution', row_id: row.id,
        }),
      });
      await refresh();
    } catch (e) { window.alert(`Rollback failed: ${e.message}`); }
  };
  const hit = row.hit_rate == null ? '—' : `${(row.hit_rate * 100).toFixed(1)}%`;
  const ci = (row.hit_rate_wilson_lower == null || row.hit_rate_wilson_upper == null)
    ? '—'
    : `${(row.hit_rate_wilson_lower * 100).toFixed(0)}–${(row.hit_rate_wilson_upper * 100).toFixed(0)}`;
  const pnl = row.mean_pnl_pct == null ? '—' : `${row.mean_pnl_pct.toFixed(2)}%`;
  const brier = row.brier_score == null ? '—' : row.brier_score.toFixed(3);
  const ece = row.ece == null ? '—' : row.ece.toFixed(3);
  return (
    <tr style={{
      borderTop: '1px solid #1f2937',
      opacity: insufficient ? 0.45 : 1,
    }} title={insufficient
        ? `n_closed=${row.n_closed} below min_n — re-checking nightly`
        : undefined}>
      <td style={{ padding: 8 }}>{row.scope_name}</td>
      <td style={{ padding: 8, textAlign: 'right', color: '#9ca3af' }}>
        {row.n_closed}
      </td>
      <td style={{ padding: 8, textAlign: 'right' }}>{hit}</td>
      <td style={{ padding: 8, textAlign: 'right', color: '#9ca3af', fontSize: 11 }}>
        {ci}
      </td>
      <td style={{ padding: 8, textAlign: 'right' }}>{pnl}</td>
      <td style={{ padding: 8, textAlign: 'right', color: '#9ca3af' }}>{brier}</td>
      <td style={{ padding: 8, textAlign: 'right', color: '#9ca3af' }}>{ece}</td>
      <td style={{ padding: 8, color: '#9ca3af', fontSize: 11 }}>
        {row.notes || '—'}
      </td>
      <td style={{ padding: 8 }}>
        {reviewed && (
          <Pill tone={approved ? 'on' : 'warn'} title={
            approved ? 'operator approved' : 'operator rolled back'
          }>
            {approved ? 'APPROVED' : 'ROLLED BACK'}
          </Pill>
        )}
      </td>
      <td style={{ padding: 8, whiteSpace: 'nowrap' }}>
        <ActionButton
          onClick={onApprove}
          disabled={insufficient}
          title={insufficient ? 'insufficient_sample_size — cannot approve' : 'mark operator_approved=1'}
        >Approve</ActionButton>
        <ActionButton
          onClick={onRollback}
          tone="danger"
          disabled={!reviewed && !approved}
          title="mark operator_approved=0"
        >Rollback</ActionButton>
      </td>
    </tr>
  );
}

function AttributionTable({ title, rows, refresh }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{
        fontSize: 12, color: '#d1d5db', fontWeight: 600,
        marginBottom: 6, textTransform: 'uppercase',
        letterSpacing: '0.06em',
      }}>{title}</div>
      <div style={{
        background: '#0a0a0a', borderRadius: 8, overflow: 'hidden',
        border: '1px solid #1f2937',
      }}>
        <table style={{
          width: '100%', borderCollapse: 'collapse', fontSize: 12.5,
        }}>
          <thead>
            <tr style={{ background: '#111827', color: '#9ca3af' }}>
              <th style={{ textAlign: 'left', padding: 8 }}>Scope</th>
              <th style={{ textAlign: 'right', padding: 8 }}>N closed</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Hit rate</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Wilson CI</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Mean P&L</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Brier</th>
              <th style={{ textAlign: 'right', padding: 8 }}>ECE</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Notes</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Review</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {(rows || []).map((r) => (
              <AttributionRow key={r.id} row={r} refresh={refresh} />
            ))}
            {(!rows || rows.length === 0) && (
              <tr>
                <td colSpan={10} style={{
                  padding: 16, color: '#6b7280', textAlign: 'center',
                }}>
                  No rows yet — the attribution writer hasn’t run for this scope
                  yet, or all rows fell below the min-n guardrail.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SectionAttribution({ refreshTick, bumpRefresh }) {
  const [agents, setAgents] = useState(null);
  const [axes, setAxes] = useState(null);
  const [strategies, setStrategies] = useState(null);
  const [err, setErr] = useState(null);
  const [recomputing, setRecomputing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [a, x, s] = await Promise.all([
        api('/learning/attribution/agents'),
        api('/learning/attribution/axes'),
        api('/learning/attribution/strategies'),
      ]);
      setAgents(a);
      setAxes(x);
      setStrategies(s);
      setErr(null);
    } catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  const onRecompute = async () => {
    setRecomputing(true);
    try {
      await api('/learning/attribution/recompute', { method: 'POST' });
      await load();
      bumpRefresh();
    } catch (e) { window.alert(`Recompute failed: ${e.message}`); }
    finally { setRecomputing(false); }
  };

  return (
    <Panel
      title="1. What's Working — Attribution"
      subtitle="Per-agent / per-axis / per-strategy calibration scoreboard from closed trades."
      collapsible
      right={
        <button onClick={onRecompute} disabled={recomputing} style={{
          background: '#0a0a0a', color: '#93c5fd',
          border: '1px solid #1f2937', borderRadius: 6,
          padding: '4px 10px', fontSize: 11.5, cursor: 'pointer',
        }}>{recomputing ? 'Recomputing…' : 'Refresh now'}</button>
      }
    >
      {err && <div style={{ color: '#fca5a5', padding: 8 }}>Error: {err}</div>}
      {!err && (
        <>
          <AttributionTable
            title={`Agents (window ${agents?.window_days || 30}d, min_n ${agents?.min_n})`}
            rows={agents?.agents}
            refresh={load}
          />
          <AttributionTable
            title={`Axes (window ${axes?.window_days || 30}d, min_n ${axes?.min_n})`}
            rows={axes?.axes}
            refresh={load}
          />
          <AttributionTable
            title={`Strategies (window ${strategies?.window_days || 30}d, min_n ${strategies?.min_n})`}
            rows={strategies?.strategies}
            refresh={load}
          />
        </>
      )}
    </Panel>
  );
}

// ── Section 2 — Counterfactuals / "What If?" ───────────────────────

function CFCard({ title, payload, onRecompute, busy }) {
  const cf = payload?.counterfactual;
  const cur = cf?.current;
  const alt = cf?.alternative;
  const noteFromCF = cf?.notes || cf?.note;
  const isCurves = cf?.pnl_curve;
  return (
    <div style={{
      background: '#0a0a0a', borderRadius: 6,
      border: '1px solid #1f2937', padding: 10, minWidth: 260, flex: 1,
    }}>
      <div style={{
        fontSize: 12, color: '#e5e7eb', fontWeight: 600,
        marginBottom: 4, display: 'flex', justifyContent: 'space-between',
      }}>
        <span>{title}</span>
        <button onClick={onRecompute} disabled={busy} style={{
          background: '#1e3a8a', color: '#93c5fd',
          border: '1px solid #3b82f6', borderRadius: 4,
          padding: '2px 6px', fontSize: 10.5,
          cursor: busy ? 'wait' : 'pointer',
        }}>{busy ? '…' : 'Recompute'}</button>
      </div>
      {!payload && (
        <div style={{ color: '#6b7280', fontSize: 11 }}>No data yet.</div>
      )}
      {payload && (
        <div style={{ fontSize: 11.5, color: '#d1d5db' }}>
          {cur != null && (
            <div>current: <strong>{JSON.stringify(cur)}</strong></div>
          )}
          {alt != null && (
            <div>alt: <strong>{JSON.stringify(alt)}</strong></div>
          )}
          {isCurves && (
            <div style={{
              marginTop: 6, padding: 6, background: '#111827',
              borderRadius: 4, fontFamily: 'monospace', fontSize: 10.5,
            }}>
              {Object.entries(cf.pnl_curve).map(([k, v]) => (
                <div key={k}>×{k}: {Number(v).toFixed(2)}%</div>
              ))}
            </div>
          )}
          {noteFromCF && (
            <div style={{ marginTop: 6, color: '#9ca3af' }}>{String(noteFromCF)}</div>
          )}
        </div>
      )}
    </div>
  );
}

function SectionCounterfactuals() {
  const [provs, setProvs] = useState([]);
  const [pickedProv, setPickedProv] = useState(null);
  const [bundle, setBundle] = useState(null);
  const [err, setErr] = useState(null);
  // MITS Phase 18-FU Gap 8 — kind ∈ {null, 'sizing', 'policy', 'consensus'}.
  // null = closed; a string opens the WhatIfModal with that variant.
  const [modalKind, setModalKind] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // We list recent provenance rows so the operator can drill in.
        // /decision/provenance returns the rolling head — first 20 is
        // enough for "what did we decide today?" inspection.
        const r = await api('/decision/provenance?limit=20');
        const rows = r?.rows || r || [];
        if (alive) {
          setProvs(rows);
          if (rows.length && !pickedProv) setPickedProv(rows[0].id);
        }
      } catch (e) {
        if (alive) setErr(String(e));
      }
    })();
    return () => { alive = false; };
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const reloadBundle = useCallback(async () => {
    if (!pickedProv) { setBundle(null); return; }
    try {
      const b = await api(`/learning/counterfactual/${pickedProv}`);
      setBundle(b);
    } catch (e) {
      setErr(String(e));
    }
  }, [pickedProv]);

  useEffect(() => { reloadBundle(); }, [reloadBundle]);

  const wrap = bundle?.counterfactuals || bundle?.bundle || bundle || {};
  const sizing = wrap.sizing ? { counterfactual: wrap.sizing } : null;
  const policy = wrap.policy ? { counterfactual: wrap.policy } : null;
  const consensus = wrap.consensus ? { counterfactual: wrap.consensus } : null;

  // Gap 8 — three openers, one modal. Each card's Recompute button now
  // opens the modal (form-driven, dropdowns + inline result), not a
  // chain of window.prompt() calls. On modal close we refresh the
  // bundle so the just-computed CF appears in the card.
  const openModal = (kind) => () => {
    if (!pickedProv) {
      window.alert('Pick a decision first.');
      return;
    }
    setModalKind(kind);
  };
  const closeModal = () => {
    const wasOpen = !!modalKind;
    setModalKind(null);
    if (wasOpen) reloadBundle();
  };

  return (
    <Panel
      title="2. What If? — Counterfactual Replayer"
      subtitle="Pick a recent decision and explore sizing / policy / consensus alternatives."
      collapsible
      right={
        <select
          value={pickedProv || ''}
          onChange={(e) => setPickedProv(Number(e.target.value) || null)}
          style={{
            background: '#0a0a0a', color: '#e5e7eb',
            border: '1px solid #1f2937', borderRadius: 6,
            padding: '4px 8px', fontSize: 11.5,
          }}
        >
          <option value="">-- pick a decision --</option>
          {provs.map((p) => (
            <option key={p.id} value={p.id}>
              #{p.id} · {p.ticker || '?'} · {p.event_status || '?'}
            </option>
          ))}
        </select>
      }
    >
      {err && <div style={{ color: '#fca5a5', padding: 8 }}>Error: {err}</div>}
      {!pickedProv && (
        <div style={{ color: '#9ca3af', padding: 8 }}>
          Select a recent decision to load its counterfactuals.
        </div>
      )}
      {pickedProv && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <CFCard
            title="Sizing"
            payload={sizing}
            onRecompute={openModal('sizing')}
            busy={false}
          />
          <CFCard
            title="Policy"
            payload={policy}
            onRecompute={openModal('policy')}
            busy={false}
          />
          <CFCard
            title="Consensus"
            payload={consensus}
            onRecompute={openModal('consensus')}
            busy={false}
          />
        </div>
      )}
      <WhatIfModal
        open={!!modalKind}
        kind={modalKind}
        provenanceId={pickedProv}
        onClose={closeModal}
      />
    </Panel>
  );
}

// ── Section 3 — Policy Tuning Advisor (18.C) ───────────────────────

// MITS Phase 18-FU Gap 5 — env-var label rendered next to the
// "queued" pill so the operator sees exactly which flag must be flipped
// to make the approved row go live. Auto-apply flag controls policy
// tuning, apply flag controls weight adaptation. The text is also
// surfaced as a tooltip on the disabled Approve button.
const POLICY_AUTO_APPLY_ENV = 'TB_POLICY_TUNING_AUTO_APPLY_ENABLED';
const WEIGHTS_APPLY_ENV = 'TB_ADAPTIVE_WEIGHTS_APPLY_ENABLED';

function PolicyTuningRow({ row, refresh, autoApplyEnabled }) {
  const insufficient = (row.recommendation_confidence === 'insufficient_data');
  const reviewed = row.operator_reviewed === 1;
  const approved = row.operator_approved === 1;
  const canApprove = !insufficient && !approved;
  const onApprove = async () => {
    try {
      await api('/learning/approve', {
        method: 'POST',
        body: JSON.stringify({ table: 'policy_tunings', row_id: row.id }),
      });
      await refresh();
    } catch (e) { window.alert(`Approve failed: ${e.message}`); }
  };
  const onRollback = async () => {
    try {
      await api('/learning/rollback', {
        method: 'POST',
        body: JSON.stringify({ table: 'policy_tunings', row_id: row.id }),
      });
      await refresh();
    } catch (e) { window.alert(`Rollback failed: ${e.message}`); }
  };
  const confTone = insufficient ? 'off'
    : row.recommendation_confidence === 'high' ? 'on'
    : row.recommendation_confidence === 'medium' ? 'warn'
    : 'off';
  return (
    <tr style={{
      borderTop: '1px solid #1f2937',
      opacity: insufficient ? 0.5 : 1,
    }}>
      <td style={{ padding: 8 }}>{row.rule_name}</td>
      <td style={{ padding: 8, textAlign: 'right', color: '#9ca3af' }}>
        {row.current_value}
      </td>
      <td style={{ padding: 8, textAlign: 'right' }}>
        {row.recommended_value == null ? '—' : row.recommended_value}
      </td>
      <td style={{ padding: 8 }}>
        <Pill tone={confTone}>{row.recommendation_confidence}</Pill>
      </td>
      <td style={{ padding: 8, color: '#9ca3af', fontSize: 11.5 }}>
        {row.rationale || '—'}
      </td>
      <td style={{ padding: 8 }}>
        {reviewed && approved && (
          <>
            <Pill tone={autoApplyEnabled ? 'on' : 'warn'}
              title={autoApplyEnabled
                ? 'auto-apply ON — engine will adopt this recommendation'
                : `${POLICY_AUTO_APPLY_ENV}=0; row is queued only`}>
              {autoApplyEnabled ? 'ACTIVE' : 'QUEUED'}
            </Pill>
            {!autoApplyEnabled && (
              <div style={{
                fontSize: 10, color: '#9ca3af', marginTop: 2,
              }}>
                will apply when <code>{POLICY_AUTO_APPLY_ENV}=1</code>
              </div>
            )}
          </>
        )}
        {reviewed && !approved && (
          <Pill tone="warn">ROLLED BACK</Pill>
        )}
      </td>
      <td style={{ padding: 8, whiteSpace: 'nowrap' }}>
        <ActionButton
          onClick={onApprove}
          disabled={!canApprove}
          title={
            insufficient ? 'insufficient_data — cannot approve' :
            approved ? 'already approved — flip env var to activate' :
            'mark operator_approved=1 (still advisory until env flag set)'
          }
        >Approve</ActionButton>
        <ActionButton
          onClick={onRollback}
          tone="danger"
          disabled={insufficient || !reviewed}
          title="un-approve and write rollback audit row"
        >Rollback</ActionButton>
      </td>
    </tr>
  );
}

function SectionPolicyTuning({ flags, refreshTick }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await api('/learning/policy-tuning');
      setData(d);
      setErr(null);
    } catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  return (
    <Panel
      title="3. Policy Tuning Advisor"
      subtitle="Threshold recommendations for the 8 tunable policy rules."
      collapsible
    >
      {err && <div style={{ color: '#fca5a5', padding: 8 }}>Error: {err}</div>}
      <div style={{
        background: '#0a0a0a', borderRadius: 8, overflow: 'hidden',
        border: '1px solid #1f2937', marginBottom: 10,
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: '#111827', color: '#9ca3af' }}>
              <th style={{ textAlign: 'left', padding: 8 }}>Rule</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Current</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Recommended</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Confidence</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Rationale</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Review</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rows || []).map((r) => (
              <PolicyTuningRow
                key={r.id}
                row={r}
                refresh={load}
                autoApplyEnabled={!!flags?.policy_tuning_auto_apply_enabled}
              />
            ))}
            {(!data?.rows || data.rows.length === 0) && (
              <tr>
                <td colSpan={7} style={{
                  padding: 16, color: '#6b7280', textAlign: 'center',
                }}>
                  No advisory rows yet. The nightly advisor pass runs at
                  22:30 ET when <code>TB_POLICY_TUNING_ENABLED=1</code>,
                  or POST <code>/learning/policy-tuning/recompute</code>
                  to compute on demand.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <ApprovalSummaryBanner
        rows={data?.rows}
        flagActive={!!flags?.policy_tuning_auto_apply_enabled}
        envVar={POLICY_AUTO_APPLY_ENV}
        flagLabel="auto-apply"
      />
    </Panel>
  );
}

// ── Gap 5 helper — operator-facing summary banner ───────────────────
//
// Renders "X approved+queued | Y pending | Z rolled back" under each
// advisor section, plus the env-var name the operator needs to flip to
// actually activate the queued rows. When the apply flag is True (which
// is False today on EC2), the banner text changes to "X approved rows
// ACTIVE in engine" so the operator sees the difference at a glance.

function ApprovalSummaryBanner({ rows, flagActive, envVar, flagLabel }) {
  const list = Array.isArray(rows) ? rows : [];
  let approved = 0;
  let pending = 0;
  let rolled = 0;
  for (const r of list) {
    if (r.operator_reviewed === 1 && r.operator_approved === 1) approved += 1;
    else if (r.operator_reviewed === 1) rolled += 1;
    else pending += 1;
  }
  const tone = flagActive ? '#6ee7b7' : '#fbbf24';
  return (
    <div style={{
      background: '#1f2937', borderRadius: 6, padding: 10,
      fontSize: 11.5, color: '#d1d5db', lineHeight: 1.5,
    }}>
      <div style={{ marginBottom: 4 }}>
        {flagActive ? (
          <>
            <strong style={{ color: tone }}>{approved} rows ACTIVE</strong>
            {' in engine · '}
            <strong>{pending}</strong> pending review · {' '}
            <strong>{rolled}</strong> rolled back
          </>
        ) : (
          <>
            <strong style={{ color: tone }}>{approved} rows approved + QUEUED</strong>
            {' · '}
            <strong>{pending}</strong> pending review · {' '}
            <strong>{rolled}</strong> rolled back
          </>
        )}
      </div>
      <div>
        <span style={{ color: '#9ca3af' }}>
          {flagLabel}: {' '}
        </span>
        <strong style={{ color: tone }}>
          {flagActive ? 'ON' : 'OFF'}
        </strong>
        <span style={{ color: '#9ca3af' }}>
          {flagActive
            ? ' — approved rows are live in the engine.'
            : (
                <>
                  {' — approved rows stay queued until '}
                  <code style={{ color: '#e5e7eb' }}>{envVar}=1</code>.
                </>
              )}
        </span>
      </div>
    </div>
  );
}

// ── Section 4 — Weight Adaptation (18.D) ───────────────────────────

function WeightRow({ row, refresh, applyEnabled }) {
  const insufficient = (row.confidence_level === 'insufficient_data');
  const reviewed = row.operator_reviewed === 1;
  const approved = row.operator_approved === 1;
  const canApprove = !insufficient && !approved;
  const onApprove = async () => {
    try {
      await api('/learning/approve', {
        method: 'POST',
        body: JSON.stringify({
          table: 'agent_weight_history', row_id: row.id,
        }),
      });
      await refresh();
    } catch (e) { window.alert(`Approve failed: ${e.message}`); }
  };
  const onRollback = async () => {
    try {
      await api('/learning/rollback', {
        method: 'POST',
        body: JSON.stringify({
          table: 'agent_weight_history', row_id: row.id,
        }),
      });
      await refresh();
    } catch (e) { window.alert(`Rollback failed: ${e.message}`); }
  };
  const tone = insufficient ? 'off'
    : row.confidence_level === 'high' ? 'on'
    : row.confidence_level === 'medium' ? 'warn'
    : 'off';
  return (
    <tr style={{
      borderTop: '1px solid #1f2937',
      opacity: insufficient ? 0.5 : 1,
    }}>
      <td style={{ padding: 8 }}>{row.agent}</td>
      <td style={{ padding: 8, textAlign: 'right', color: '#9ca3af' }}>
        {row.base_weight?.toFixed?.(2) ?? row.base_weight}
      </td>
      <td style={{ padding: 8, textAlign: 'right' }}>
        {row.weight_proposed?.toFixed?.(2) ?? row.weight_proposed}
      </td>
      <td style={{ padding: 8 }}>
        <Pill tone={tone}>{row.confidence_level}</Pill>
      </td>
      <td style={{ padding: 8, color: '#9ca3af', fontSize: 11.5 }}>
        {row.rationale || '—'}
      </td>
      <td style={{ padding: 8 }}>
        {reviewed && approved && (
          <>
            <Pill tone={applyEnabled ? 'on' : 'warn'}
              title={applyEnabled
                ? 'apply ON — engine uses these weights'
                : `${WEIGHTS_APPLY_ENV}=0; row is queued only`}>
              {applyEnabled ? 'ACTIVE' : 'QUEUED'}
            </Pill>
            {!applyEnabled && (
              <div style={{
                fontSize: 10, color: '#9ca3af', marginTop: 2,
              }}>
                will apply when <code>{WEIGHTS_APPLY_ENV}=1</code>
              </div>
            )}
          </>
        )}
        {reviewed && !approved && (
          <Pill tone="warn">ROLLED BACK</Pill>
        )}
      </td>
      <td style={{ padding: 8, whiteSpace: 'nowrap' }}>
        <ActionButton
          onClick={onApprove}
          disabled={!canApprove}
          title={
            insufficient ? 'insufficient_data — cannot approve' :
            approved ? 'already approved — flip env var to activate' :
            'mark operator_approved=1 (still advisory until env flag set)'
          }
        >Approve</ActionButton>
        <ActionButton
          onClick={onRollback}
          tone="danger"
          disabled={insufficient || !reviewed}
          title="un-approve and write rollback audit row"
        >Rollback</ActionButton>
      </td>
    </tr>
  );
}

function SectionWeights({ flags, refreshTick }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await api('/learning/weights');
      setData(d);
      setErr(null);
    } catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  return (
    <Panel
      title="4. Weight Adaptation Advisor"
      subtitle="Proposed adaptive multipliers for the 8 council agents."
      collapsible
    >
      {err && <div style={{ color: '#fca5a5', padding: 8 }}>Error: {err}</div>}
      <div style={{
        background: '#0a0a0a', borderRadius: 8, overflow: 'hidden',
        border: '1px solid #1f2937', marginBottom: 10,
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: '#111827', color: '#9ca3af' }}>
              <th style={{ textAlign: 'left', padding: 8 }}>Agent</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Base weight</th>
              <th style={{ textAlign: 'right', padding: 8 }}>Proposed</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Confidence</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Rationale</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Review</th>
              <th style={{ textAlign: 'left', padding: 8 }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rows || []).map((r) => (
              <WeightRow
                key={r.id}
                row={r}
                refresh={load}
                applyEnabled={!!flags?.adaptive_weights_apply_enabled}
              />
            ))}
            {(!data?.rows || data.rows.length === 0) && (
              <tr>
                <td colSpan={7} style={{
                  padding: 16, color: '#6b7280', textAlign: 'center',
                }}>
                  No advisory rows yet. The nightly advisor pass runs at
                  22:45 ET when <code>TB_ADAPTIVE_WEIGHTS_ENABLED=1</code>,
                  or POST <code>/learning/weights/recompute</code> to
                  compute on demand.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <ApprovalSummaryBanner
        rows={data?.rows}
        flagActive={!!flags?.adaptive_weights_apply_enabled}
        envVar={WEIGHTS_APPLY_ENV}
        flagLabel="apply"
      />
    </Panel>
  );
}

// ── Top-of-page safety-flags banner + audit ribbon ─────────────────

function FlagsBanner({ flags }) {
  const flagsList = [
    { key: 'decision_rollback_enabled', label: 'Decision rollback hook' },
    { key: 'policy_tuning_advisory_enabled', label: 'Policy advisory' },
    { key: 'policy_tuning_auto_apply_enabled', label: 'Policy auto-apply' },
    { key: 'adaptive_weights_advisory_enabled', label: 'Weights advisory' },
    { key: 'adaptive_weights_apply_enabled', label: 'Weights apply' },
  ];
  return (
    <div style={{
      background: '#111827', borderRadius: 8, padding: 10,
      border: '1px solid #1f2937', marginBottom: 12,
      display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
    }}>
      <div style={{ color: '#9ca3af', fontSize: 11.5, marginRight: 6 }}>
        Safety flags:
      </div>
      {flagsList.map(({ key, label }) => (
        <Pill key={key} tone={flags?.[key] ? 'on' : 'off'} title={key}>
          {label}: {flags?.[key] ? 'ON' : 'OFF'}
        </Pill>
      ))}
    </div>
  );
}

function AuditRibbon({ refreshTick }) {
  const [rows, setRows] = useState([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await api('/learning/audit-log?limit=10');
        if (alive) setRows(d?.rows || []);
      } catch (e) { /* swallow — non-critical */ }
    })();
    return () => { alive = false; };
  }, [refreshTick]);
  if (!rows.length) return null;
  return (
    <Panel
      title="Recent operator actions (audit log)"
      subtitle="Last 10 approve/rollback entries — newest first."
      collapsible
    >
      <div style={{
        background: '#0a0a0a', borderRadius: 8, overflow: 'hidden',
        border: '1px solid #1f2937',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}>
          <thead>
            <tr style={{ background: '#111827', color: '#9ca3af' }}>
              <th style={{ textAlign: 'left', padding: 6 }}>When</th>
              <th style={{ textAlign: 'left', padding: 6 }}>Table</th>
              <th style={{ textAlign: 'right', padding: 6 }}>Row</th>
              <th style={{ textAlign: 'left', padding: 6 }}>Action</th>
              <th style={{ textAlign: 'left', padding: 6 }}>Notes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} style={{ borderTop: '1px solid #1f2937' }}>
                <td style={{ padding: 6, color: '#d1d5db' }}>
                  {r.created_at ? new Date(r.created_at).toLocaleString() : '—'}
                </td>
                <td style={{ padding: 6 }}>{r.table_name}</td>
                <td style={{ padding: 6, textAlign: 'right' }}>#{r.row_id}</td>
                <td style={{ padding: 6 }}>
                  <Pill tone={r.action === 'approve' ? 'on' : 'warn'}>
                    {r.action}
                  </Pill>
                </td>
                <td style={{ padding: 6, color: '#9ca3af' }}>
                  {r.notes || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

// ── Page shell ─────────────────────────────────────────────────────

export default function HypothesisStudio() {
  const [flags, setFlags] = useState(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const bumpRefresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const f = await api('/learning/flags');
        if (alive) setFlags(f);
      } catch (e) { /* fallback to no flags; UI degrades gracefully */ }
    })();
    return () => { alive = false; };
  }, [refreshTick]);

  return (
    <div style={{ padding: 16 }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'baseline', marginBottom: 12, gap: 12,
        flexWrap: 'wrap',
      }}>
        <div>
          <h1 style={{ fontSize: 22, margin: 0 }}>Hypothesis Studio</h1>
          <div style={{ fontSize: 12, color: '#9ca3af', marginTop: 4 }}>
            Operator console for 18.A attribution · 18.B counterfactuals
            · 18.C policy tuning · 18.D weight adaptation.
            All actions are advisory — nothing auto-applies.
          </div>
        </div>
        <div>
          <Link to="/decision-cockpit" style={{
            color: '#93c5fd', textDecoration: 'none', fontSize: 12,
            border: '1px solid #1f2937', padding: '6px 12px',
            borderRadius: 6, background: '#0a0a0a',
          }}>← Decision Cockpit</Link>
        </div>
      </div>

      <FlagsBanner flags={flags} />

      <SectionAttribution
        refreshTick={refreshTick}
        bumpRefresh={bumpRefresh}
      />
      <SectionCounterfactuals />
      <SectionPolicyTuning flags={flags} refreshTick={refreshTick} />
      <SectionWeights flags={flags} refreshTick={refreshTick} />

      <AuditRibbon refreshTick={refreshTick} />
    </div>
  );
}
