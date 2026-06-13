/**
 * Feature-Merge F3 — CounterfactualWhatIfPanel.
 *
 * Interactive 3-card panel that lets the operator ask "what if we'd
 * sized 2x?" / "what if we'd overridden the regime block?" /
 * "what if the Macro agent had voted buy?" without leaving the
 * cockpit. Each card POSTs to /learning/counterfactual/{provId}/{kind}
 * and renders the alternative.
 *
 * Cards:
 *   • Sizing  — POST /learning/counterfactual/{provId}/sizing
 *                body: { factors: [0.5, 1.0, 1.5, 2.0] }
 *                shows pnl_curve as (factor, projected_pnl) pairs
 *   • Policy  — POST /learning/counterfactual/{provId}/policy
 *                body: { rule_name: "..." }
 *                shows new_headline_blocker + other_blockers_still_firing
 *                + eligible_with_override
 *   • Consensus — POST /learning/counterfactual/{provId}/consensus
 *                body: { agent, new_stance, new_confidence }
 *                shows new consensus.recommendation + flipped flag
 *
 * Plain English throughout — operator-grade language, no jargon.
 *
 * Styling matches DecisionCockpit.jsx's Panel/PanelHeader/Pill chrome.
 */
import React, { useEffect, useMemo, useState } from 'react';

// ── chrome primitives (matching DecisionCockpit.jsx) ─────────────────

function Pill({ tone = 'info', children, title }) {
  const palette = {
    on: { bg: '#064e3b', fg: '#6ee7b7', border: '#10b981' },
    off: { bg: '#1f2937', fg: '#9ca3af', border: '#374151' },
    info: { bg: '#1e3a8a', fg: '#93c5fd', border: '#3b82f6' },
    warn: { bg: '#78350f', fg: '#fcd34d', border: '#f59e0b' },
    danger: { bg: '#7f1d1d', fg: '#fca5a5', border: '#ef4444' },
    purple: { bg: '#4c1d95', fg: '#c4b5fd', border: '#8b5cf6' },
  };
  const c = palette[tone] || palette.info;
  return (
    <span title={title || undefined} style={{
      display: 'inline-block', padding: '2px 8px', borderRadius: 12,
      fontSize: 11, fontWeight: 600,
      background: c.bg, color: c.fg, border: `1px solid ${c.border}`,
      marginRight: 4,
    }}>{children}</span>
  );
}

function Panel({ children }) {
  return (
    <div style={{
      background: '#111827', borderRadius: 8, padding: 16,
      border: '1px solid #1f2937', marginBottom: 16,
    }}>{children}</div>
  );
}

function PanelHeader({ icon, title, right }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between',
      alignItems: 'center', marginBottom: 12,
    }}>
      <h3 style={{ margin: 0, fontSize: 16, color: '#e5e7eb' }}>
        {icon} {title}
      </h3>
      <div>{right}</div>
    </div>
  );
}

const inputStyle = {
  width: '100%', padding: '6px 8px', fontSize: 12,
  fontFamily: 'monospace',
  background: '#0a0a0a', color: '#e5e7eb',
  border: '1px solid #1f2937', borderRadius: 4,
  marginBottom: 8,
  boxSizing: 'border-box',
};

const selectStyle = { ...inputStyle, fontFamily: 'inherit' };

const buttonStyle = (disabled) => ({
  background: disabled ? '#1f2937' : '#1e3a8a',
  color: disabled ? '#6b7280' : '#93c5fd',
  border: `1px solid ${disabled ? '#374151' : '#3b82f6'}`,
  borderRadius: 6, padding: '6px 14px', fontSize: 12, fontWeight: 600,
  cursor: disabled ? 'not-allowed' : 'pointer',
});

function CardShell({ title, subtitle, children }) {
  return (
    <div style={{
      padding: 12, background: '#0a0a0a',
      border: '1px solid #1f2937', borderRadius: 6,
      display: 'flex', flexDirection: 'column', minHeight: 200,
    }}>
      <div style={{
        fontSize: 11, color: '#93c5fd', fontWeight: 600,
        textTransform: 'uppercase', letterSpacing: '0.05em',
      }}>{title}</div>
      {subtitle && (
        <div style={{
          fontSize: 11, color: '#9ca3af',
          marginTop: 2, marginBottom: 8, lineHeight: 1.4,
        }}>{subtitle}</div>
      )}
      <div style={{ flex: 1 }}>
        {children}
      </div>
    </div>
  );
}

function ResultBox({ children, tone = 'info' }) {
  const palette = {
    info: { bg: '#111827', border: '#1f2937' },
    on: { bg: '#064e3b', border: '#10b981' },
    warn: { bg: '#78350f', border: '#f59e0b' },
    danger: { bg: '#7f1d1d', border: '#ef4444' },
  };
  const p = palette[tone] || palette.info;
  return (
    <div style={{
      marginTop: 8, padding: 8,
      background: p.bg, border: `1px solid ${p.border}`,
      borderRadius: 4, fontSize: 12,
    }}>{children}</div>
  );
}

// ── Sizing sub-card ──────────────────────────────────────────────────

function fmtPctText(v, digits = 2) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  const n = Number(v);
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(digits)}%`;
}

function SizingCard({ data, computing, error, onRecompute }) {
  const [factorsText, setFactorsText] = useState('0.5, 1.0, 1.5, 2.0');
  const handle = () => {
    const factors = factorsText
      .split(',')
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n) && n >= 0);
    onRecompute('sizing', { factors });
  };

  const pnlCurve = data?.pnl_curve;
  const realized = data?.realized_pnl_pct;
  const originalFactor = data?.original_factor;

  return (
    <CardShell
      title="Sizing — what if we'd scaled the position?"
      subtitle="Tries the position at different size multipliers and shows the projected P&L curve."
    >
      <label style={{ fontSize: 11, color: '#9ca3af', display: 'block', marginBottom: 4 }}>
        Size factors (comma-separated multipliers)
      </label>
      <input
        type="text"
        value={factorsText}
        onChange={(e) => setFactorsText(e.target.value)}
        placeholder="0.5, 1.0, 1.5, 2.0"
        style={inputStyle}
      />
      <button
        type="button"
        onClick={handle}
        disabled={computing}
        style={buttonStyle(computing)}
      >
        {computing ? 'Computing…' : 'Recompute sizing'}
      </button>

      {(originalFactor != null || realized != null) && (
        <div style={{ marginTop: 8, fontSize: 11, color: '#9ca3af' }}>
          {originalFactor != null && (
            <>Original size: <strong style={{ color: '#e5e7eb' }}>
              {Number(originalFactor).toFixed(2)}x
            </strong>{' · '}</>
          )}
          {realized != null && (
            <>Realized: <strong style={{
              color: Number(realized) >= 0 ? '#6ee7b7' : '#fca5a5',
            }}>
              {fmtPctText(realized)}
            </strong></>
          )}
        </div>
      )}

      {pnlCurve && typeof pnlCurve === 'object' && (
        <ResultBox tone="info">
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>
            Projected P&amp;L per factor:
          </div>
          {Object.entries(pnlCurve).map(([k, v]) => {
            const n = Number(v);
            const ok = Number.isFinite(n);
            return (
              <div key={k} style={{
                display: 'flex', justifyContent: 'space-between',
                fontFamily: 'monospace',
                color: ok && n >= 0 ? '#6ee7b7' : ok ? '#fca5a5' : '#9ca3af',
              }}>
                <span>x{k}</span>
                <span>{ok ? fmtPctText(n) : String(v)}</span>
              </div>
            );
          })}
        </ResultBox>
      )}

      {data?.notes && (
        <div style={{ marginTop: 6, fontSize: 11, color: '#9ca3af' }}>
          {String(data.notes)}
        </div>
      )}

      {error && (
        <ResultBox tone="danger">
          <span style={{ color: '#fca5a5' }}>{error}</span>
        </ResultBox>
      )}
    </CardShell>
  );
}

// ── Policy sub-card ──────────────────────────────────────────────────

function PolicyCard({ data, blockers, computing, error, onRecompute }) {
  const [rule, setRule] = useState(blockers[0] || '');
  useEffect(() => {
    if (!rule && blockers.length > 0) setRule(blockers[0]);
  }, [blockers, rule]);
  const handle = () => onRecompute('policy', { rule_name: rule });

  return (
    <CardShell
      title="Policy — what if we'd overridden a block?"
      subtitle="Pick one blocking rule. The engine recomputes whether the decision would have been eligible without it."
    >
      <label style={{ fontSize: 11, color: '#9ca3af', display: 'block', marginBottom: 4 }}>
        Override which blocking rule?
      </label>
      <select
        value={rule}
        onChange={(e) => setRule(e.target.value)}
        style={selectStyle}
      >
        {blockers.length === 0 && (
          <option value="">(no blockers fired)</option>
        )}
        {blockers.map((b) => (
          <option key={b} value={b}>{b}</option>
        ))}
      </select>
      <button
        type="button"
        onClick={handle}
        disabled={computing || !rule}
        style={buttonStyle(computing || !rule)}
      >
        {computing ? 'Computing…' : 'Recompute policy'}
      </button>

      {data && (
        <ResultBox tone={data.eligible_with_override ? 'on' : 'warn'}>
          <div style={{ marginBottom: 4 }}>
            <span style={{ color: '#9ca3af' }}>Was blocked by:</span>{' '}
            <span style={{ color: '#fcd34d', fontFamily: 'monospace' }}>
              {data.original_headline_blocker || '—'}
            </span>
          </div>
          <div style={{ marginBottom: 4 }}>
            <span style={{ color: '#9ca3af' }}>Now blocked by:</span>{' '}
            <span style={{ color: '#93c5fd', fontFamily: 'monospace' }}>
              {data.new_headline_blocker || 'nothing — would have passed'}
            </span>
          </div>
          <div style={{ marginBottom: 4 }}>
            <span style={{ color: '#9ca3af' }}>Would have been eligible?</span>{' '}
            <Pill tone={data.eligible_with_override ? 'on' : 'danger'}>
              {data.eligible_with_override ? 'yes' : 'no'}
            </Pill>
          </div>
          {Array.isArray(data.other_blockers_still_firing)
            && data.other_blockers_still_firing.length > 0 && (
            <div style={{ marginTop: 4, fontSize: 11, color: '#9ca3af' }}>
              Other blockers still firing:{' '}
              <span style={{ color: '#fca5a5', fontFamily: 'monospace' }}>
                {data.other_blockers_still_firing.join(', ')}
              </span>
            </div>
          )}
        </ResultBox>
      )}

      {error && (
        <ResultBox tone="danger">
          <span style={{ color: '#fca5a5' }}>{error}</span>
        </ResultBox>
      )}
    </CardShell>
  );
}

// ── Consensus sub-card ───────────────────────────────────────────────

// 8 known agents — read from learning_insights.active_weight_proposals
// .known_agents at runtime, but seed with the published roster so the
// dropdown is usable even on cold-start cockpits.
const FALLBACK_AGENTS = [
  'market_structure',
  'technical',
  'options',
  'historical_analog',
  'simulator',
  'macro',
  'risk',
  'chairman',
];
const STANCE_OPTIONS = ['buy', 'sell', 'hold', 'abstain'];

function ConsensusCard({ data, agents, computing, error, onRecompute }) {
  const agentList = useMemo(() => (
    Array.isArray(agents) && agents.length > 0 ? agents : FALLBACK_AGENTS
  ), [agents]);
  const [agent, setAgent] = useState(agentList[0] || '');
  const [stance, setStance] = useState('buy');
  const [confidence, setConfidence] = useState('70');

  useEffect(() => {
    if (!agentList.includes(agent)) setAgent(agentList[0] || '');
  }, [agentList, agent]);

  const handle = () => onRecompute('consensus', {
    agent, new_stance: stance, new_confidence: Number(confidence) || 0,
  });

  const newReco = data?.new_consensus?.recommendation;
  const sizeMul = data?.new_consensus?.size_multiplier;
  const flipped = !!data?.flipped_recommendation;

  return (
    <CardShell
      title="Consensus — what if an agent had voted differently?"
      subtitle="Flip one agent's vote and re-run the council vote. Shows whether the headline recommendation would have flipped."
    >
      <label style={{ fontSize: 11, color: '#9ca3af', display: 'block', marginBottom: 4 }}>
        Which agent?
      </label>
      <select
        value={agent}
        onChange={(e) => setAgent(e.target.value)}
        style={selectStyle}
      >
        {agentList.map((a) => (
          <option key={a} value={a}>{a}</option>
        ))}
      </select>

      <div style={{
        display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6,
      }}>
        <div>
          <label style={{ fontSize: 11, color: '#9ca3af', display: 'block', marginBottom: 4 }}>
            New stance
          </label>
          <select
            value={stance}
            onChange={(e) => setStance(e.target.value)}
            style={selectStyle}
          >
            {STANCE_OPTIONS.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
        <div>
          <label style={{ fontSize: 11, color: '#9ca3af', display: 'block', marginBottom: 4 }}>
            Confidence (0-100)
          </label>
          <input
            type="number"
            min={0}
            max={100}
            value={confidence}
            onChange={(e) => setConfidence(e.target.value)}
            style={inputStyle}
          />
        </div>
      </div>

      <button
        type="button"
        onClick={handle}
        disabled={computing || !agent}
        style={buttonStyle(computing || !agent)}
      >
        {computing ? 'Computing…' : 'Recompute consensus'}
      </button>

      {data && (
        <ResultBox tone={flipped ? 'warn' : 'info'}>
          <div style={{ marginBottom: 4 }}>
            <span style={{ color: '#9ca3af' }}>New recommendation:</span>{' '}
            <Pill tone={
              newReco === 'EXECUTE' ? 'on'
              : newReco === 'ABSTAIN' ? 'off'
              : 'info'
            }>{newReco || '—'}</Pill>
            {flipped && <Pill tone="warn">flipped from original</Pill>}
          </div>
          {sizeMul != null && (
            <div style={{ fontSize: 11, color: '#9ca3af' }}>
              New size multiplier:{' '}
              <strong style={{ color: '#e5e7eb' }}>
                {Number(sizeMul).toFixed(2)}x
              </strong>
            </div>
          )}
          {data?.new_consensus?.confidence != null && (
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>
              New council confidence:{' '}
              <strong style={{ color: '#e5e7eb' }}>
                {Math.round(Number(data.new_consensus.confidence) * 100)}%
              </strong>
            </div>
          )}
        </ResultBox>
      )}

      {error && (
        <ResultBox tone="danger">
          <span style={{ color: '#fca5a5' }}>{error}</span>
        </ResultBox>
      )}
    </CardShell>
  );
}

// ── public panel ─────────────────────────────────────────────────────

export default function CounterfactualWhatIfPanel({
  provId,
  cf,
  recompute,
  computing,
  error,
  policyBlockers = [],
  knownAgents = [],
}) {
  if (!provId) {
    return (
      <Panel>
        <PanelHeader
          icon="(W)"
          title="Counterfactual what-if (interactive)"
          right={<Pill tone="off">unavailable</Pill>}
        />
        <div style={{ color: '#9ca3af', fontSize: 13, lineHeight: 1.55 }}>
          Counterfactual recompute requires a numeric decision_provenance
          ID. Open a specific decision (the picker above takes trade IDs,
          provenance IDs, or tickers) and the three what-if cards will
          activate.
        </div>
      </Panel>
    );
  }

  const sizing = cf?.sizing || null;
  const policy = cf?.policy || null;
  const consensus = cf?.consensus || null;

  return (
    <Panel>
      <PanelHeader
        icon="(W)"
        title="Counterfactual what-if (interactive)"
        right={
          <>
            <Pill tone="info">prov #{provId}</Pill>
            {cf?.computed_at && (
              <Pill tone="off" title={cf.computed_at}>
                last {String(cf.computed_at).slice(11, 19)}
              </Pill>
            )}
          </>
        }
      />
      <div style={{
        fontSize: 12, color: '#9ca3af', marginBottom: 12, lineHeight: 1.55,
      }}>
        Re-run a single dimension of this decision and see what would
        have changed. Each card POSTs to <code>/learning/counterfactual</code>;
        the cockpit page itself isn&apos;t reloaded.
      </div>
      <div style={{
        display: 'grid', gap: 10,
        gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
      }}>
        <SizingCard
          data={sizing}
          computing={computing}
          error={error}
          onRecompute={recompute}
        />
        <PolicyCard
          data={policy}
          blockers={policyBlockers}
          computing={computing}
          error={error}
          onRecompute={recompute}
        />
        <ConsensusCard
          data={consensus}
          agents={knownAgents}
          computing={computing}
          error={error}
          onRecompute={recompute}
        />
      </div>
    </Panel>
  );
}
