/**
 * MITS Phase 18-FU (Gap 8) — Studio "What If?" modal.
 *
 * Replaces the three window.prompt() call-sites in HypothesisStudio §2 with
 * a proper modal form that handles all three counterfactual variations:
 *
 *   * kind="sizing"    — comma-separated factor list → POST .../sizing
 *   * kind="policy"    — rule_name dropdown (30 entries from GET /policy/rules)
 *                        → POST .../policy
 *   * kind="consensus" — agent dropdown (8 entries from GET /agents/list)
 *                        + stance dropdown (buy/sell/hold/abstain)
 *                        + confidence int 0..100
 *                        → POST .../consensus
 *
 * Dropdown options are fetched ONCE on first open and cached on the parent.
 * The modal renders the POST result inline so the operator can audit the
 * counterfactual without leaving the form.
 *
 * Styling matches the rest of the studio dark theme:
 *   background '#111827', borders '#1f2937', accents '#3b82f6'.
 *
 * Closes on Esc, on backdrop click, and on successful submit (after a
 * 1-second "result visible" hold so the operator can read the output).
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';

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

const STANCES = ['buy', 'sell', 'hold', 'abstain'];

const DEFAULT_SIZING_FACTORS = '0.5, 1.0, 1.5, 2.0';

function parseFactorString(raw) {
  // Robust parse: split on commas/whitespace, drop empties, coerce to
  // floats. Reject any non-finite values so the API never sees garbage.
  const tokens = String(raw || '')
    .split(/[,\s]+/)
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
  const factors = [];
  for (const t of tokens) {
    const n = Number(t);
    if (!Number.isFinite(n) || n <= 0) {
      throw new Error(`invalid factor "${t}" — must be a positive number`);
    }
    factors.push(n);
  }
  if (factors.length === 0) {
    throw new Error('enter at least one positive factor (e.g. "0.5, 1.0, 1.5")');
  }
  return factors;
}

function FieldLabel({ children, htmlFor }) {
  return (
    <label htmlFor={htmlFor} style={{
      display: 'block', fontSize: 11, color: '#9ca3af',
      textTransform: 'uppercase', letterSpacing: '0.05em',
      marginBottom: 4, fontWeight: 600,
    }}>{children}</label>
  );
}

function Input(props) {
  return (
    <input
      {...props}
      style={{
        width: '100%', boxSizing: 'border-box',
        background: '#0a0a0a', color: '#e5e7eb',
        border: '1px solid #1f2937', borderRadius: 6,
        padding: '8px 10px', fontSize: 13,
        ...(props.style || {}),
      }}
    />
  );
}

function Select(props) {
  return (
    <select
      {...props}
      style={{
        width: '100%', boxSizing: 'border-box',
        background: '#0a0a0a', color: '#e5e7eb',
        border: '1px solid #1f2937', borderRadius: 6,
        padding: '8px 10px', fontSize: 13,
        ...(props.style || {}),
      }}
    >
      {props.children}
    </select>
  );
}

function CFResultBlock({ payload }) {
  if (!payload) return null;
  const cf = payload.counterfactual || payload;
  const cur = cf?.current;
  const alt = cf?.alternative;
  const curves = cf?.pnl_curve;
  const note = cf?.notes || cf?.note;
  return (
    <div style={{
      marginTop: 10, padding: 10, background: '#0a0a0a',
      border: '1px solid #1f2937', borderRadius: 6,
      fontSize: 12, color: '#d1d5db',
    }}>
      <div style={{
        fontSize: 11, color: '#10b981',
        textTransform: 'uppercase', letterSpacing: '0.05em',
        fontWeight: 600, marginBottom: 6,
      }}>Result</div>
      {cur != null && (
        <div style={{ marginBottom: 3 }}>
          current: <strong style={{ color: '#e5e7eb' }}>{JSON.stringify(cur)}</strong>
        </div>
      )}
      {alt != null && (
        <div style={{ marginBottom: 3 }}>
          alternative: <strong style={{ color: '#e5e7eb' }}>{JSON.stringify(alt)}</strong>
        </div>
      )}
      {curves && (
        <div style={{
          marginTop: 6, padding: 6, background: '#111827',
          borderRadius: 4, fontFamily: 'monospace', fontSize: 11,
        }}>
          {Object.entries(curves).map(([k, v]) => (
            <div key={k}>×{k}: {Number(v).toFixed(2)}%</div>
          ))}
        </div>
      )}
      {note && (
        <div style={{ marginTop: 6, color: '#9ca3af', fontSize: 11 }}>
          note: {String(note)}
        </div>
      )}
    </div>
  );
}

/**
 * <WhatIfModal>
 *   open: bool                       — controls visibility
 *   kind: 'sizing'|'policy'|'consensus'
 *   provenanceId: number             — required; counterfactual target id
 *   onClose(): void                  — fired on Esc/backdrop/cancel/success
 *
 * Lazy-loads the dropdown roster from /policy/rules and /agents/list. The
 * cache lives for the modal lifetime so re-opening doesn't refetch.
 */
export default function WhatIfModal({
  open, kind, provenanceId, onClose,
}) {
  const [policyRules, setPolicyRules] = useState(null);
  const [agentRoster, setAgentRoster] = useState(null);
  const [rosterErr, setRosterErr] = useState(null);

  const [factorsRaw, setFactorsRaw] = useState(DEFAULT_SIZING_FACTORS);
  const [ruleName, setRuleName] = useState('');
  const [agent, setAgent] = useState('');
  const [stance, setStance] = useState('buy');
  const [confidence, setConfidence] = useState(60);

  const [busy, setBusy] = useState(false);
  const [submitErr, setSubmitErr] = useState(null);
  const [result, setResult] = useState(null);
  const backdropRef = useRef(null);

  // Reset form state every time the modal opens so a previous submission
  // doesn't bleed visible state into a fresh attempt.
  useEffect(() => {
    if (open) {
      setSubmitErr(null);
      setResult(null);
      setBusy(false);
      if (kind === 'sizing') setFactorsRaw(DEFAULT_SIZING_FACTORS);
    }
  }, [open, kind]);

  // Lazy-load roster the first time a policy or consensus modal opens.
  useEffect(() => {
    if (!open) return;
    if (kind === 'policy' && policyRules == null) {
      api('/policy/rules')
        .then((rows) => {
          // /policy/rules returns either an array or {rules:[...]}
          const arr = Array.isArray(rows) ? rows : (rows?.rules || []);
          setPolicyRules(arr);
          if (arr.length && !ruleName) setRuleName(arr[0].name);
        })
        .catch((e) => setRosterErr(`policy/rules: ${e.message}`));
    }
    if (kind === 'consensus' && agentRoster == null) {
      api('/agents/list')
        .then((d) => {
          const arr = d?.agents || [];
          setAgentRoster(arr);
          if (arr.length && !agent) setAgent(arr[0].agent);
        })
        .catch((e) => setRosterErr(`agents/list: ${e.message}`));
    }
  }, [open, kind, policyRules, agentRoster, ruleName, agent]);

  // Esc to close.
  const handleClose = useCallback(() => {
    if (!busy) onClose?.();
  }, [busy, onClose]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') handleClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, handleClose]);

  if (!open) return null;

  const onSubmit = async (e) => {
    e?.preventDefault?.();
    if (!provenanceId) {
      setSubmitErr('no provenance id selected');
      return;
    }
    setBusy(true);
    setSubmitErr(null);
    setResult(null);
    try {
      let body;
      let path;
      if (kind === 'sizing') {
        const factors = parseFactorString(factorsRaw);
        body = { factors };
        path = `/learning/counterfactual/${provenanceId}/sizing`;
      } else if (kind === 'policy') {
        if (!ruleName) throw new Error('pick a rule');
        body = { rule_name: ruleName };
        path = `/learning/counterfactual/${provenanceId}/policy`;
      } else {
        if (!agent) throw new Error('pick an agent');
        if (!STANCES.includes(stance)) throw new Error('pick a stance');
        body = {
          agent,
          new_stance: stance,
          new_confidence: Math.max(0, Math.min(100, Number(confidence) || 0)),
        };
        path = `/learning/counterfactual/${provenanceId}/consensus`;
      }
      const out = await api(path, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      setResult(out);
    } catch (e2) {
      setSubmitErr(e2.message);
    } finally {
      setBusy(false);
    }
  };

  const onBackdropClick = (e) => {
    if (e.target === backdropRef.current) handleClose();
  };

  const title = kind === 'sizing' ? 'What If — Sizing factors'
              : kind === 'policy' ? 'What If — Policy override'
              : 'What If — Flip an agent';

  return (
    <div
      ref={backdropRef}
      onClick={onBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.65)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 9999,
      }}
    >
      <div style={{
        background: '#111827', border: '1px solid #1f2937',
        borderRadius: 10, padding: 16, minWidth: 340, maxWidth: 540,
        width: '90%', maxHeight: '85vh', overflowY: 'auto',
        boxShadow: '0 10px 50px rgba(0,0,0,0.6)',
      }}>
        <div style={{
          display: 'flex', justifyContent: 'space-between',
          alignItems: 'center', marginBottom: 12,
        }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: '#e5e7eb' }}>
            {title}
          </div>
          <button
            onClick={handleClose}
            disabled={busy}
            aria-label="Close modal"
            style={{
              background: 'transparent', border: 'none',
              color: '#9ca3af', cursor: busy ? 'not-allowed' : 'pointer',
              fontSize: 18, padding: '0 6px',
            }}
          >x</button>
        </div>
        <div style={{ fontSize: 11, color: '#6b7280', marginBottom: 10 }}>
          provenance #{provenanceId}
        </div>

        {rosterErr && (
          <div style={{
            color: '#fca5a5', fontSize: 12, marginBottom: 8,
          }}>
            roster fetch failed: {rosterErr}
          </div>
        )}

        <form onSubmit={onSubmit}>
          {kind === 'sizing' && (
            <div style={{ marginBottom: 12 }}>
              <FieldLabel htmlFor="cf-factor-list">
                Factor: comma-separated multipliers
              </FieldLabel>
              <Input
                id="cf-factor-list"
                type="text"
                value={factorsRaw}
                onChange={(e) => setFactorsRaw(e.target.value)}
                placeholder="0.5, 1.0, 1.5, 2.0"
                autoFocus
              />
              <div style={{ fontSize: 11, color: '#6b7280', marginTop: 4 }}>
                Each factor scales the realized size linearly so the
                replayer can plot a sizing curve at no extra cost.
              </div>
            </div>
          )}

          {kind === 'policy' && (
            <div style={{ marginBottom: 12 }}>
              <FieldLabel htmlFor="cf-rule-name">
                Rule: which blocking rule should we relax?
              </FieldLabel>
              <Select
                id="cf-rule-name"
                value={ruleName}
                onChange={(e) => setRuleName(e.target.value)}
                autoFocus
              >
                {(policyRules || []).map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name} ({r.category}/{r.severity})
                  </option>
                ))}
                {(!policyRules || policyRules.length === 0) && (
                  <option value="">loading rules...</option>
                )}
              </Select>
              <div style={{ fontSize: 11, color: '#6b7280', marginTop: 4 }}>
                Replayer will recompute the decision as if this rule had
                not fired. 404 if the rule wasn't a blocker originally.
              </div>
            </div>
          )}

          {kind === 'consensus' && (
            <>
              <div style={{ marginBottom: 12 }}>
                <FieldLabel htmlFor="cf-agent">
                  Agent: who should we flip?
                </FieldLabel>
                <Select
                  id="cf-agent"
                  value={agent}
                  onChange={(e) => setAgent(e.target.value)}
                  autoFocus
                >
                  {(agentRoster || []).map((a) => (
                    <option key={a.agent} value={a.agent}>
                      {a.agent} ({a.role})
                    </option>
                  ))}
                  {(!agentRoster || agentRoster.length === 0) && (
                    <option value="">loading agents...</option>
                  )}
                </Select>
              </div>
              <div style={{ marginBottom: 12 }}>
                <FieldLabel htmlFor="cf-stance">
                  Stance: flip to
                </FieldLabel>
                <Select
                  id="cf-stance"
                  value={stance}
                  onChange={(e) => setStance(e.target.value)}
                >
                  {STANCES.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </Select>
              </div>
              <div style={{ marginBottom: 12 }}>
                <FieldLabel htmlFor="cf-conf">
                  Confidence: 0..100
                </FieldLabel>
                <Input
                  id="cf-conf"
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  value={confidence}
                  onChange={(e) => setConfidence(Number(e.target.value))}
                />
              </div>
            </>
          )}

          {submitErr && (
            <div style={{
              color: '#fca5a5', fontSize: 12, padding: 8,
              background: '#7f1d1d', borderRadius: 6,
              border: '1px solid #ef4444', marginBottom: 10,
            }}>
              {submitErr}
            </div>
          )}

          <CFResultBlock payload={result} />

          <div style={{
            display: 'flex', justifyContent: 'flex-end',
            gap: 8, marginTop: 12,
          }}>
            <button
              type="button"
              onClick={handleClose}
              disabled={busy}
              style={{
                background: '#0a0a0a', color: '#d1d5db',
                border: '1px solid #1f2937', borderRadius: 6,
                padding: '8px 14px', fontSize: 12, fontWeight: 600,
                cursor: busy ? 'not-allowed' : 'pointer',
              }}
            >Close</button>
            <button
              type="submit"
              disabled={busy}
              style={{
                background: busy ? '#0a0a0a' : '#1e3a8a',
                color: busy ? '#4b5563' : '#93c5fd',
                border: `1px solid ${busy ? '#1f2937' : '#3b82f6'}`,
                borderRadius: 6,
                padding: '8px 14px', fontSize: 12, fontWeight: 600,
                cursor: busy ? 'wait' : 'pointer',
              }}
            >{busy ? 'Running...' : 'Submit'}</button>
          </div>
        </form>
      </div>
    </div>
  );
}
