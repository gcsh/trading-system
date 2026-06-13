/* MITS Phase 19 Stream 3 — ChairmanMemoPanel.
 *
 * Renders `chairman_memo`:
 *   { decision, decision_reason, kill_condition, structured_why,
 *     main_risk, confidence_pct, conviction, position_size_modifier,
 *     evidence_correlation, independent_signal_count,
 *     bull_case, bear_case }
 *
 * Big decision pill, kill condition prominent, structured_why bullets.
 */
import React from 'react';
import { Card, Pill, EmptyState } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

function decisionTone(d) {
  switch ((d || '').toUpperCase()) {
    case 'SUPPORTED':                return 'success';
    case 'OPPOSED': case 'REJECTED': return 'error';
    case 'ABSTAINED':                return 'warning';
    default:                         return 'neutral';
  }
}

export default function ChairmanMemoPanel({ memo }) {
  if (!memo || Object.keys(memo).length === 0) {
    return (
      <Card>
        <PanelHead title="Chairman memo" subtitle="decision verbatim" />
        <EmptyState message="Chairman did not memo this decision (likely market_closed or pre-cycle)." />
      </Card>
    );
  }

  const tone = decisionTone(memo.decision);
  const why = Array.isArray(memo.structured_why) ? memo.structured_why : [];
  const confPct = Number(memo.confidence_pct) || 0;
  const conv = Number(memo.conviction) || 0;

  return (
    <Card>
      <PanelHead
        title="Chairman memo"
        subtitle={memo.decision_reason || ''}
        right={
          <Pill tone={tone} size="md">{memo.decision || '—'}</Pill>
        }
      />
      <div style={{
        display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 8,
      }}>
        <div style={{
          fontSize: 26, fontWeight: 700, color: 'var(--text-primary)',
          fontFamily: 'var(--font-mono)', lineHeight: 1,
        }}>{confPct}%</div>
        <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
          confidence · conviction {conv.toFixed(2)}
        </div>
      </div>

      {memo.kill_condition && (
        <div style={{
          padding: 8, marginBottom: 8,
          background: 'rgba(255, 51, 85, 0.10)',
          border: '1px solid var(--accent-red)',
          borderRadius: 4, fontSize: 12,
          color: 'var(--accent-red)', fontStyle: 'italic',
        }} title="If this condition fires, the chairman would flip the decision.">
          <strong>Kill if:</strong> {memo.kill_condition}
        </div>
      )}

      {why.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <div style={{
            fontSize: 11, color: 'var(--accent-cyan)',
            textTransform: 'uppercase', letterSpacing: '0.06em',
            marginBottom: 4,
          }}>Why (verbatim)</div>
          <ul style={{ margin: '0 0 0 16px', padding: 0, fontSize: 12, color: 'var(--text-secondary)' }}>
            {why.map((line, i) => (
              <li key={i}>{typeof line === 'string' ? line : JSON.stringify(line)}</li>
            ))}
          </ul>
        </div>
      )}

      {memo.main_risk && (
        <div style={{
          padding: 8, marginBottom: 8,
          background: 'rgba(255, 215, 0, 0.10)',
          border: '1px solid var(--accent-yellow)',
          borderRadius: 4, fontSize: 12,
          color: 'var(--accent-yellow)',
        }}>
          <strong>Main risk:</strong> {memo.main_risk}
        </div>
      )}

      {(memo.bull_case || memo.bear_case) && (
        <div style={{
          display: 'grid', gridTemplateColumns: '1fr 1fr',
          gap: 6, marginBottom: 8,
        }}>
          {memo.bull_case && (
            <div style={{
              padding: 6, background: 'rgba(0, 255, 136, 0.08)',
              borderLeft: '2px solid var(--accent-green)',
              fontSize: 11, color: 'var(--text-secondary)', borderRadius: 2,
            }}>
              <strong style={{ color: 'var(--accent-green)' }}>Bull:</strong>{' '}
              {memo.bull_case}
            </div>
          )}
          {memo.bear_case && (
            <div style={{
              padding: 6, background: 'rgba(255, 51, 85, 0.08)',
              borderLeft: '2px solid var(--accent-red)',
              fontSize: 11, color: 'var(--text-secondary)', borderRadius: 2,
            }}>
              <strong style={{ color: 'var(--accent-red)' }}>Bear:</strong>{' '}
              {memo.bear_case}
            </div>
          )}
        </div>
      )}

      <Footer>
        <span title="How many independent evidence streams supported this decision.">
          Independent signals
        </span>
        <span className="mono" style={{ color: 'var(--text-secondary)' }}>
          {memo.independent_signal_count != null ? memo.independent_signal_count : '—'}
          {memo.evidence_correlation && (
            <span style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>
              ({memo.evidence_correlation})
            </span>
          )}
        </span>
      </Footer>
    </Card>
  );
}
