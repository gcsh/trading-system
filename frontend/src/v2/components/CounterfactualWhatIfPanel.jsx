/* MITS Phase 19 Stream 3 — CounterfactualWhatIfPanel.
 *
 * Interactive 3-card layout. Each card POSTs to
 *   /learning/counterfactual/{prov_id}/{sizing|policy|consensus}
 * and renders the alternative.
 *
 * Sizing:    factor multiplier input (CSV) → pnl_curve
 * Policy:    override one blocker rule_name → new_headline_blocker
 * Consensus: flip one agent's stance → new consensus.recommendation
 *
 * Caller provides:
 *   provId          → numeric provenance ID (required for POST)
 *   initialCf       → starting cf bundle from /decision/cockpit
 *   policyBlockers  → list of policy_result.blocking_factors[].rule names
 *   knownAgents     → list of agent names from learning_insights.active_weight_proposals.known_agents
 *   recompute       → from useCounterfactual.recompute(kind, body)
 *   computing       → boolean from useCounterfactual.computing
 */
import React, { useState } from 'react';
import { Card, EmptyState, Pill } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

const STANCE_OPTIONS = ['buy', 'sell', 'hold', 'abstain'];

function CFCard({ title, tone, children }) {
  return (
    <div style={{
      padding: 10, background: 'var(--bg-tertiary)',
      border: `1px solid var(--border-subtle)`,
      borderRadius: 6,
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'baseline', marginBottom: 6,
      }}>
        <div style={{
          fontSize: 11, color: 'var(--accent-cyan)',
          textTransform: 'uppercase', letterSpacing: '0.06em',
          fontWeight: 600,
        }}>{title}</div>
        {tone}
      </div>
      {children}
    </div>
  );
}

function Btn({ onClick, disabled, children }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      style={{
        background: disabled ? 'var(--bg-tertiary)' : 'var(--accent-cyan)',
        color: disabled ? 'var(--text-tertiary)' : '#0a0e1a',
        border: 'none', borderRadius: 4, padding: '4px 10px',
        fontSize: 11, fontWeight: 600, cursor: disabled ? 'not-allowed' : 'pointer',
      }}>
      {children}
    </button>
  );
}

function fmtPctNum(v, digits = 2) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return `${Number(v).toFixed(digits)}%`;
}

function SizingCFCard({ sizing, onRecompute, computing }) {
  const [factors, setFactors] = useState('0.5,1.0,1.5,2.0');
  return (
    <CFCard title="Sizing — what if we'd scaled?">
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 4 }}>
        Factors (CSV)
      </div>
      <input
        type="text"
        value={factors}
        onChange={(e) => setFactors(e.target.value)}
        style={{
          width: '100%', padding: '4px 6px', fontSize: 11,
          fontFamily: 'var(--font-mono)',
          background: 'var(--bg-primary)',
          color: 'var(--text-primary)',
          border: '1px solid var(--border-default)',
          borderRadius: 4, marginBottom: 6,
        }}
      />
      <Btn disabled={computing} onClick={() => {
        const parsed = factors.split(',').map(s => Number(s.trim())).filter(Number.isFinite);
        onRecompute('sizing', { factors: parsed });
      }}>
        {computing ? 'Computing…' : 'Recompute'}
      </Btn>
      {sizing && (
        <div style={{
          marginTop: 8, padding: 6, background: 'var(--bg-primary)',
          borderRadius: 4, fontSize: 10, fontFamily: 'var(--font-mono)',
        }}>
          {sizing.pnl_curve && typeof sizing.pnl_curve === 'object'
            ? Object.entries(sizing.pnl_curve).map(([k, v]) => {
                const num = Number(v);
                return (
                  <div key={k} style={{
                    display: 'flex', justifyContent: 'space-between',
                    color: num >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                  }}>
                    <span>×{k}</span>
                    <span>{Number.isFinite(num) ? `${num.toFixed(2)}%` : String(v)}</span>
                  </div>
                );
              })
            : <span style={{ color: 'var(--text-tertiary)' }}>
                {sizing.notes || sizing.note || 'no curve computed'}
              </span>}
        </div>
      )}
    </CFCard>
  );
}

function PolicyCFCard({ policy, blockers, onRecompute, computing }) {
  const initial = policy?.rule_overridden || (blockers[0] || '');
  const [rule, setRule] = useState(initial);
  return (
    <CFCard title="Policy — what if we'd overridden?">
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 4 }}>
        Override rule
      </div>
      <select value={rule} onChange={(e) => setRule(e.target.value)}
        style={{
          width: '100%', padding: '4px 6px', fontSize: 11,
          background: 'var(--bg-primary)', color: 'var(--text-primary)',
          border: '1px solid var(--border-default)',
          borderRadius: 4, marginBottom: 6,
        }}>
        {blockers.length === 0 && <option value="">(no blockers)</option>}
        {blockers.map(b => <option key={b} value={b}>{b}</option>)}
      </select>
      <Btn disabled={computing || !rule} onClick={() => onRecompute('policy', { rule_name: rule })}>
        {computing ? 'Computing…' : 'Recompute'}
      </Btn>
      {policy && (
        <div style={{
          marginTop: 8, padding: 6, background: 'var(--bg-primary)',
          borderRadius: 4, fontSize: 11,
        }}>
          <div style={{ marginBottom: 2 }}>
            <span style={{ color: 'var(--text-tertiary)' }}>Was:</span>{' '}
            <span className="mono" style={{ color: 'var(--accent-yellow)' }}>{policy.original_headline_blocker || '—'}</span>
          </div>
          <div style={{ marginBottom: 2 }}>
            <span style={{ color: 'var(--text-tertiary)' }}>Now:</span>{' '}
            <span className="mono" style={{ color: 'var(--accent-cyan)' }}>{policy.new_headline_blocker || '—'}</span>
          </div>
          <div style={{ marginBottom: 2 }}>
            <span style={{ color: 'var(--text-tertiary)' }}>Eligible:</span>{' '}
            <Pill tone={policy.eligible_with_override ? 'success' : 'error'}>
              {policy.eligible_with_override ? 'yes' : 'no'}
            </Pill>
          </div>
          {Array.isArray(policy.other_blockers_still_firing) && policy.other_blockers_still_firing.length > 0 && (
            <div style={{ marginTop: 4, fontSize: 10, color: 'var(--text-tertiary)' }}>
              Still firing: {policy.other_blockers_still_firing.join(', ')}
            </div>
          )}
        </div>
      )}
    </CFCard>
  );
}

function ConsensusCFCard({ consensus, agents, onRecompute, computing }) {
  const [agent, setAgent]           = useState(consensus?.agent_flipped || agents[0] || '');
  const [stance, setStance]         = useState(consensus?.new_stance || 'buy');
  const [confidence, setConfidence] = useState(consensus?.new_confidence != null ? String(consensus.new_confidence) : '70');
  return (
    <CFCard title="Consensus — what if we'd flipped?">
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 4 }}>
        Flip agent
      </div>
      <select value={agent} onChange={(e) => setAgent(e.target.value)}
        style={{
          width: '100%', padding: '4px 6px', fontSize: 11,
          background: 'var(--bg-primary)', color: 'var(--text-primary)',
          border: '1px solid var(--border-default)',
          borderRadius: 4, marginBottom: 6,
        }}>
        {agents.length === 0 && <option value="">(no agents)</option>}
        {agents.map(a => <option key={a} value={a}>{a}</option>)}
      </select>
      <div style={{
        display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, marginBottom: 6,
      }}>
        <select value={stance} onChange={(e) => setStance(e.target.value)}
          style={{
            padding: '4px 6px', fontSize: 11,
            background: 'var(--bg-primary)', color: 'var(--text-primary)',
            border: '1px solid var(--border-default)', borderRadius: 4,
          }}>
          {STANCE_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          type="number" min={0} max={100}
          value={confidence}
          onChange={(e) => setConfidence(e.target.value)}
          style={{
            padding: '4px 6px', fontSize: 11,
            background: 'var(--bg-primary)', color: 'var(--text-primary)',
            border: '1px solid var(--border-default)', borderRadius: 4,
            fontFamily: 'var(--font-mono)',
          }}
        />
      </div>
      <Btn disabled={computing || !agent} onClick={() => onRecompute('consensus', {
        agent, new_stance: stance, new_confidence: Number(confidence) || 0,
      })}>
        {computing ? 'Computing…' : 'Recompute'}
      </Btn>
      {consensus && (
        <div style={{
          marginTop: 8, padding: 6, background: 'var(--bg-primary)',
          borderRadius: 4, fontSize: 11,
        }}>
          <div>
            <span style={{ color: 'var(--text-tertiary)' }}>New:</span>{' '}
            <Pill tone={consensus.flipped_recommendation ? 'success' : 'neutral'}>
              {consensus.new_consensus?.recommendation || '—'}
            </Pill>
            {consensus.flipped_recommendation && (
              <Pill tone="warning">flipped</Pill>
            )}
          </div>
          {consensus.new_consensus?.size_multiplier != null && (
            <div style={{ marginTop: 2, fontSize: 10, color: 'var(--text-tertiary)' }}>
              size × {Number(consensus.new_consensus.size_multiplier).toFixed(2)}
            </div>
          )}
        </div>
      )}
    </CFCard>
  );
}

export default function CounterfactualWhatIfPanel({
  provId, cf, recompute, computing,
  policyBlockers = [], knownAgents = [],
}) {
  if (!provId) {
    return (
      <Card>
        <PanelHead title="Counterfactual what-if" subtitle="sizing · policy · consensus" />
        <EmptyState message="Counterfactuals require a numeric decision provenance ID (open a specific decision from the picker)." />
      </Card>
    );
  }

  const sizing    = cf?.sizing    || null;
  const policy    = cf?.policy    || null;
  const consensus = cf?.consensus || null;

  return (
    <Card>
      <PanelHead
        title="Counterfactual what-if"
        subtitle="POST to /learning/counterfactual"
        right={<Pill tone="info">interactive</Pill>}
      />
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
        gap: 8,
      }}>
        <SizingCFCard    sizing={sizing}    onRecompute={recompute} computing={computing} />
        <PolicyCFCard    policy={policy}    blockers={policyBlockers} onRecompute={recompute} computing={computing} />
        <ConsensusCFCard consensus={consensus} agents={knownAgents}   onRecompute={recompute} computing={computing} />
      </div>
      <Footer>
        <span>Replay drift held at 0.0 (Phase 17 invariant).</span>
        <span className="mono" style={{ color: 'var(--text-tertiary)' }}>
          {cf?.computed_at ? `last cf ${cf.computed_at.slice(11, 19)}` : ''}
        </span>
      </Footer>
    </Card>
  );
}
