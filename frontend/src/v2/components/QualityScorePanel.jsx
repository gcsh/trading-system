/* MITS Phase 19 Stream 3 — QualityScorePanel.
 *
 * Renders `decision_quality_score` (DQS):
 *   { composite, analysis_quality, council_agreement, risk_quality,
 *     execution_quality, components: {...} }
 *
 * Big composite number with color coding (<40 red, 40-60 yellow, >60 green).
 * 4 sub-score bars + plain-English tooltips on each.
 */
import React from 'react';
import { Card, EmptyState, Pill } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

const SUBSCORE_TOOLTIPS = {
  analysis_quality: 'How healthy is the data feeding our analysis? Higher = fresher data, more analog history, more pattern hits.',
  council_agreement: 'How much do the 8 council agents agree? 100 = unanimous, 0 = split-vote.',
  risk_quality: 'How well-sized is this trade vs portfolio risk? 100 = within all caps. Penalised by correlation + soft penalties.',
  execution_quality: 'How easy is this trade to execute? Higher = tight spread, fresh IV, deep liquidity.',
};

const SUBSCORE_LABELS = {
  analysis_quality: 'Analysis',
  council_agreement: 'Council',
  risk_quality: 'Risk',
  execution_quality: 'Execution',
};

function compositeColor(v) {
  if (v == null) return 'var(--text-tertiary)';
  if (v < 40)    return 'var(--accent-red)';
  if (v < 60)    return 'var(--accent-yellow)';
  return            'var(--accent-green)';
}

function compositeTone(v) {
  if (v == null) return 'neutral';
  if (v < 40)    return 'error';
  if (v < 60)    return 'warning';
  return            'success';
}

function SubScoreBar({ name, value }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0));
  const color = compositeColor(pct);
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontSize: 11, marginBottom: 2,
      }}>
        <span title={SUBSCORE_TOOLTIPS[name] || ''}
              style={{
                color: 'var(--text-secondary)',
                borderBottom: '1px dotted var(--text-tertiary)',
                cursor: 'help',
              }}>
          {SUBSCORE_LABELS[name] || name}
        </span>
        <span className="mono" style={{ color, fontWeight: 600 }}>
          {pct.toFixed(1)}
        </span>
      </div>
      <div style={{
        height: 6, background: 'var(--bg-tertiary)',
        borderRadius: 3, overflow: 'hidden',
      }}>
        <div style={{
          width: `${pct}%`, height: '100%', background: color,
          transition: 'width 200ms ease',
        }} />
      </div>
    </div>
  );
}

export default function QualityScorePanel({ dqs }) {
  if (!dqs) {
    return (
      <Card>
        <PanelHead title="Decision quality" subtitle="composite + 4 sub-scores" />
        <EmptyState message="No quality score for this decision." />
      </Card>
    );
  }

  const composite = Number(dqs.composite);
  const color = compositeColor(composite);
  const tone = compositeTone(composite);

  return (
    <Card>
      <PanelHead
        title="Decision quality"
        subtitle="DQS composite"
        right={<Pill tone={tone}>{tone === 'success' ? 'GREEN' : tone === 'warning' ? 'YELLOW' : 'RED'}</Pill>}
      />
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 8,
        marginBottom: 12,
      }}>
        <div style={{
          fontSize: 36, fontWeight: 700, color,
          fontFamily: 'var(--font-mono)', lineHeight: 1,
        }}>{Number.isFinite(composite) ? composite.toFixed(1) : '—'}</div>
        <div style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
          / 100 composite quality
        </div>
      </div>

      <SubScoreBar name="analysis_quality"  value={dqs.analysis_quality} />
      <SubScoreBar name="council_agreement" value={dqs.council_agreement} />
      <SubScoreBar name="risk_quality"      value={dqs.risk_quality} />
      <SubScoreBar name="execution_quality" value={dqs.execution_quality} />

      {dqs.components && typeof dqs.components === 'object' && (
        <details style={{ marginTop: 8 }}>
          <summary style={{
            fontSize: 11, color: 'var(--accent-cyan)', cursor: 'pointer',
          }}>Component detail (10+ inputs)</summary>
          <div style={{
            marginTop: 4, padding: 6,
            background: 'var(--bg-tertiary)',
            borderRadius: 4, fontSize: 10,
            fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)',
            maxHeight: 140, overflowY: 'auto',
          }}>
            {Object.entries(dqs.components).map(([k, v]) => (
              <div key={k} style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '1px 0',
              }}>
                <span>{k}</span>
                <span style={{ color: 'var(--text-primary)' }}>
                  {typeof v === 'number' ? v.toFixed(3) : String(v)}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}

      <Footer>
        <span title="Decision quality is calibrated against historic outcomes — a score of 60+ has historically produced positive expected value.">
          Edge gate
        </span>
        <span className="mono" style={{ color: 'var(--text-secondary)' }}>
          ≥ 60 historically profitable
        </span>
      </Footer>
    </Card>
  );
}
