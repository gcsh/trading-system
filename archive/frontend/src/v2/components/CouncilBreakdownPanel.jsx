/* MITS Phase 19 Stream 3 — CouncilBreakdownPanel.
 *
 * Renders `council_breakdown.consensus + votes + silent_agents + quorum`.
 *
 * One card per agent showing stance, confidence bar, weight, reasoning.
 * Click → expand to show supporting_factors / concerns / invalidation.
 * Bottom strip shows the consensus recommendation + quorum status.
 */
import React, { useMemo, useState } from 'react';
import { Card, Pill, EmptyState } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

function stanceTone(stance) {
  switch ((stance || '').toLowerCase()) {
    case 'buy':  case 'long':  return 'success';
    case 'sell': case 'short': return 'error';
    case 'hold':               return 'warning';
    case 'abstain':            return 'neutral';
    default:                   return 'neutral';
  }
}

function ConfidenceBar({ confidence, tone }) {
  const pct = Math.max(0, Math.min(100, Number(confidence) * 100));
  const color = tone === 'success' ? 'var(--accent-green)'
              : tone === 'error'   ? 'var(--accent-red)'
              : tone === 'warning' ? 'var(--accent-yellow)'
              :                      'var(--accent-cyan)';
  return (
    <div style={{
      height: 4, background: 'var(--bg-tertiary)',
      borderRadius: 2, overflow: 'hidden', margin: '4px 0',
    }}>
      <div style={{
        width: `${pct}%`, height: '100%', background: color,
        transition: 'width 200ms ease',
      }} />
    </div>
  );
}

function AgentCard({ vote, onClick, expanded }) {
  const tone = stanceTone(vote.stance);
  const conf = Number(vote.confidence) || 0;
  return (
    <div
      role="button"
      onClick={onClick}
      style={{
        background: 'var(--bg-tertiary)',
        border: `1px solid var(--border-subtle)`,
        borderRadius: 6, padding: 8, cursor: 'pointer',
        transition: 'background 100ms ease',
      }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6,
        marginBottom: 4,
      }}>
        <span style={{
          fontSize: 12, fontWeight: 600, color: 'var(--text-primary)',
          fontFamily: 'var(--font-mono)',
        }}>{vote.agent}</span>
        <span style={{ flex: 1 }} />
        <Pill tone={tone}>{vote.stance}</Pill>
      </div>
      <div style={{
        fontSize: 10, color: 'var(--text-tertiary)', marginBottom: 2,
      }}>{vote.role || ''}</div>
      <ConfidenceBar confidence={conf} tone={tone} />
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontSize: 10, color: 'var(--text-tertiary)',
      }}>
        <span className="mono">conf {(conf * 100).toFixed(0)}%</span>
        <span className="mono">w {Number(vote.weight || 0).toFixed(2)}</span>
      </div>
      {expanded && (
        <div style={{
          marginTop: 6, paddingTop: 6,
          borderTop: '1px solid var(--border-subtle)',
          fontSize: 11, color: 'var(--text-secondary)',
        }}>
          <div style={{ marginBottom: 4 }}>
            <strong style={{ color: 'var(--accent-cyan)' }}>Why:</strong>{' '}
            {vote.reasoning || '—'}
          </div>
          {Array.isArray(vote.invalidators) && vote.invalidators.length > 0 && (
            <div style={{ marginBottom: 4 }}>
              <strong style={{ color: 'var(--accent-yellow)' }}>Invalidators:</strong>
              <ul style={{ margin: '2px 0 0 16px', padding: 0 }}>
                {vote.invalidators.map((v, i) => (
                  <li key={i} style={{ fontSize: 10 }}>{v}</li>
                ))}
              </ul>
            </div>
          )}
          {Array.isArray(vote.key_drivers) && vote.key_drivers.length > 0 && (
            <div>
              <strong style={{ color: 'var(--accent-green)' }}>Key drivers:</strong>
              <ul style={{ margin: '2px 0 0 16px', padding: 0 }}>
                {vote.key_drivers.slice(0, 4).map((d, i) => (
                  <li key={i} style={{ fontSize: 10 }}>
                    {d.description || '—'}
                    {d.weight != null && <span style={{ color: 'var(--text-tertiary)' }}>
                      {' '}(w {Number(d.weight).toFixed(2)})
                    </span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function CouncilBreakdownPanel({ council }) {
  const [expanded, setExpanded] = useState(null);

  const consensus = council?.consensus || null;
  const votes     = Array.isArray(consensus?.votes) ? consensus.votes : [];
  const silent    = Array.isArray(consensus?.silent_agents) ? consensus.silent_agents : [];

  const recTone = useMemo(() => stanceTone(consensus?.recommendation), [consensus]);

  if (!council || !consensus) {
    return (
      <Card>
        <PanelHead title="Council breakdown" subtitle="8 agents · stance + confidence" />
        <EmptyState message="No council breakdown for this decision." />
      </Card>
    );
  }

  return (
    <Card>
      <PanelHead
        title="Council breakdown"
        subtitle={`${votes.length} agents · ${silent.length} silent`}
        right={
          <Pill tone={consensus.quorum_met ? 'success' : 'warning'}>
            {consensus.quorum_met ? `quorum ${consensus.quorum_count || 0}/${consensus.quorum_required || 0}` : 'no quorum'}
          </Pill>
        }
      />
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
        gap: 6,
      }}>
        {votes.map((v, i) => (
          <AgentCard
            key={`${v.agent}-${i}`}
            vote={v}
            expanded={expanded === i}
            onClick={() => setExpanded(e => (e === i ? null : i))}
          />
        ))}
      </div>
      {silent.length > 0 && (
        <div style={{
          marginTop: 10, fontSize: 11, color: 'var(--text-tertiary)',
        }}>
          <span style={{ marginRight: 6 }}>Silent / insufficient signal:</span>
          {silent.map((s, i) => (
            <Pill key={i} tone="neutral">{s}</Pill>
          ))}
        </div>
      )}
      <Footer>
        <span title="Recommendation produced after weighting all agents and applying disagreement penalties.">
          Consensus
        </span>
        <span>
          <Pill tone={recTone} size="md">{consensus.recommendation || '—'}</Pill>
          <span className="mono" style={{ marginLeft: 6, color: 'var(--text-secondary)' }}>
            conf {(Number(consensus.confidence) * 100).toFixed(0)}%
          </span>
          <span className="mono" style={{ marginLeft: 6, color: 'var(--text-tertiary)' }}>
            disag {Number(consensus.disagreement_score || 0).toFixed(2)}
          </span>
        </span>
      </Footer>
    </Card>
  );
}
