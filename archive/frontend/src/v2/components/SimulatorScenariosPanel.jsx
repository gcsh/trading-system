/* MITS Phase 19 Stream 3 — SimulatorScenariosPanel.
 *
 * Renders `simulator_scenarios` (list of cluster cards) + `simulator_verdict`.
 *
 * Backend may return scenarios=null for the current cohort (typical when
 * analog_cohort_size = 0). We show an EmptyState in that case rather than
 * fabricating placeholders.
 */
import React from 'react';
import { Card, EmptyState, Pill, Stat } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

const CLUSTER_LABELS = {
  continuation:    'Continuation',
  fake_breakout:   'Fake breakout',
  stop_out:        'Stop out',
  macro_shock:     'Macro shock',
};

function clusterTone(name) {
  switch ((name || '').toLowerCase()) {
    case 'continuation': return 'success';
    case 'fake_breakout': return 'warning';
    case 'stop_out':     return 'error';
    case 'macro_shock':  return 'error';
    default:             return 'neutral';
  }
}

function fmtPct(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}
function fmtMoney(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  const n = Number(v);
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${n.toFixed(2)}`;
}

export default function SimulatorScenariosPanel({ scenarios, verdict }) {
  const hasScenarios = Array.isArray(scenarios) && scenarios.length > 0;
  const hasVerdict   = verdict && typeof verdict === 'object';

  if (!hasScenarios && !hasVerdict) {
    return (
      <Card>
        <PanelHead title="Simulator scenarios" subtitle="forward-payoff decomposition" />
        <EmptyState message="No scenario decomposition for this decision (typically: analog cohort = 0 or pre-execution)." />
      </Card>
    );
  }

  return (
    <Card>
      <PanelHead
        title="Simulator scenarios"
        subtitle="forward-payoff decomposition"
        right={hasVerdict && verdict.conviction_score != null && (
          <Pill tone={Number(verdict.conviction_score) > 0.6 ? 'success' : 'warning'}>
            conviction {Number(verdict.conviction_score).toFixed(2)}
          </Pill>
        )}
      />

      {hasVerdict && (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
          gap: 8, marginBottom: 12,
          padding: 8, background: 'var(--bg-tertiary)',
          borderRadius: 4,
        }}>
          <Stat label="p(win)"     value={fmtPct(verdict.p_win)}                 mono />
          <Stat label="E[payoff]"  value={fmtMoney(verdict.expected_payoff)}     mono />
          <Stat label="p(max loss)" value={fmtPct(verdict.p_max_loss)}            mono />
          <Stat label="sample"     value={verdict.sample_size != null ? verdict.sample_size.toLocaleString() : '—'} mono />
        </div>
      )}

      {hasScenarios ? (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
          gap: 6,
        }}>
          {scenarios.map((s, i) => {
            const tone = clusterTone(s.name);
            const color = tone === 'success' ? 'var(--accent-green)'
                        : tone === 'warning' ? 'var(--accent-yellow)'
                        : tone === 'error'   ? 'var(--accent-red)'
                        :                      'var(--accent-cyan)';
            return (
              <div key={i} style={{
                padding: 8, background: 'var(--bg-tertiary)',
                borderLeft: `2px solid ${color}`,
                borderRadius: 4,
              }}>
                <div style={{
                  fontSize: 11, color, fontWeight: 600,
                  marginBottom: 4,
                }}>{CLUSTER_LABELS[s.name] || s.name}</div>
                <div style={{
                  fontSize: 14, color: 'var(--text-primary)',
                  fontFamily: 'var(--font-mono)', marginBottom: 2,
                }}>{fmtPct(s.probability)}</div>
                <div style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>
                  payoff {fmtMoney(s.expected_payoff)}
                </div>
                {s.n_analogs != null && (
                  <div style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>
                    n={s.n_analogs}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState message="Verdict surfaced, no per-cluster breakdown." />
      )}

      <Footer>
        <span title="Analog basis is the set of historical situations that resembled today's conditions.">
          Analog basis
        </span>
        <span className="mono" style={{ color: 'var(--text-secondary)' }}>
          {hasVerdict && verdict.sample_size
            ? `${verdict.sample_size.toLocaleString()} samples`
            : '—'}
        </span>
      </Footer>
    </Card>
  );
}
