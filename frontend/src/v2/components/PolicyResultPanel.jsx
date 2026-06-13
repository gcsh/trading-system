/* MITS Phase 19 Stream 3 — PolicyResultPanel.
 *
 * Renders `policy_result` from /decision/cockpit:
 *   { eligible, blocking_factors: [...], soft_penalties_total_pct,
 *     evaluated_at }
 *
 * UI:
 *   - top: eligible pill (success / error)
 *   - sorted blockers table (hard first, then soft, then ranked top 3)
 *   - "view all rules" toggle to expand the rest
 *   - sizing_penalty rollup on bottom
 *
 * Beginner-friendly tooltips: every rule_name surfaces a plain-English
 * tooltip explaining what blocked the trade.
 */
import React, { useMemo, useState } from 'react';
import { Card, Pill, EmptyState, Table } from '../../design/Components.jsx';

const RULE_EXPLANATIONS = {
  signal_hold:           'Brain output was HOLD — no directional signal to act on.',
  low_confidence:        'Brain confidence below the minimum threshold needed to trade.',
  risk_manager_rejected: 'Risk manager said no — likely max-positions, concentration, or hard cash cap.',
  cooldown_active:       'Ticker is in cooldown after a recent trade (anti-churn).',
  market_closed:         'Outside US regular trading hours.',
  data_stale:            'A required data source is too old to trust.',
  correlation_cap:       'Adding this would push portfolio correlation past the configured cap.',
  spread_too_wide:       'Bid/ask spread is too wide vs the executable threshold.',
  options_disabled:      'Options trading disabled for this ticker (per-ticker safety flag).',
  iv_rank_oob:           'IV rank is outside the band where this strategy has edge.',
};

function explainRule(name) {
  return RULE_EXPLANATIONS[name] || 'Operator-tunable policy rule (see /v2/learning for tuning).';
}

function severityTone(sev) {
  if (sev === 'hard') return 'error';
  if (sev === 'soft') return 'warning';
  return 'neutral';
}

export default function PolicyResultPanel({ policyResult }) {
  const [expanded, setExpanded] = useState(false);

  const blockers = useMemo(() => {
    if (!Array.isArray(policyResult?.blocking_factors)) return [];
    return [...policyResult.blocking_factors].sort((a, b) => {
      const sa = a.severity === 'hard' ? 0 : a.severity === 'soft' ? 1 : 2;
      const sb = b.severity === 'hard' ? 0 : b.severity === 'soft' ? 1 : 2;
      return sa - sb;
    });
  }, [policyResult]);

  if (!policyResult) {
    return (
      <Card>
        <PanelHead title="Policy result" subtitle="30-rule audit" />
        <EmptyState message="No policy result for this decision." />
      </Card>
    );
  }

  const eligible = !!policyResult.eligible;
  const showRows = expanded ? blockers : blockers.slice(0, 3);
  const cols = [
    { key: 'rule',     label: 'Rule',     mono: true },
    { key: 'category', label: 'Category' },
    { key: 'severity', label: 'Severity' },
    { key: 'reason',   label: 'Reason' },
  ];
  const rows = showRows.map((b, i) => ({
    __key: `${b.rule}-${i}`,
    rule: (
      <span title={explainRule(b.rule)}
            style={{ color: 'var(--accent-cyan)', borderBottom: '1px dotted var(--accent-cyan)', cursor: 'help' }}>
        {b.rule}
      </span>
    ),
    category: b.category || '—',
    severity: <Pill tone={severityTone(b.severity)}>{b.severity || '—'}</Pill>,
    reason:   <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}>{b.reason || '—'}</span>,
  }));

  return (
    <Card>
      <PanelHead
        title="Policy result"
        subtitle="30-rule audit"
        right={
          <Pill tone={eligible ? 'success' : 'error'} size="md">
            {eligible ? 'ELIGIBLE' : 'INELIGIBLE'}
          </Pill>
        }
      />
      {blockers.length === 0 ? (
        <EmptyState icon="✓" message="No blockers — clean pass." />
      ) : (
        <>
          <Table cols={cols} rows={rows} striped />
          {blockers.length > 3 && (
            <button
              type="button"
              onClick={() => setExpanded(e => !e)}
              style={{
                marginTop: 8, background: 'transparent',
                border: '1px solid var(--border-default)',
                color: 'var(--accent-cyan)', borderRadius: 4,
                padding: '4px 10px', fontSize: 12, cursor: 'pointer',
              }}>
              {expanded ? 'Hide' : `View all ${blockers.length} rules`}
            </button>
          )}
        </>
      )}
      <Footer>
        <span title="Total size reduction from soft penalties (each blocker may add a small size haircut).">
          Soft penalty total
        </span>
        <span className="mono" style={{ color: 'var(--accent-yellow)' }}>
          {policyResult.soft_penalties_total_pct != null
            ? `${Number(policyResult.soft_penalties_total_pct).toFixed(2)}%`
            : '—'}
        </span>
      </Footer>
    </Card>
  );
}

/* ── shared sub-components for all v2 panels ──────────────────────────── */
export function PanelHead({ title, subtitle, right }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'baseline', gap: 8,
      marginBottom: 10, paddingBottom: 8,
      borderBottom: '1px solid var(--border-subtle)',
    }}>
      <h3 style={{
        margin: 0, fontSize: 13, fontWeight: 700,
        textTransform: 'uppercase', letterSpacing: '0.08em',
        color: 'var(--text-primary)',
      }}>{title}</h3>
      {subtitle && (
        <span style={{
          fontSize: 11, color: 'var(--text-tertiary)',
        }}>· {subtitle}</span>
      )}
      <span style={{ flex: 1 }} />
      <span>{right}</span>
    </div>
  );
}
export function Footer({ children }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between',
      alignItems: 'center', marginTop: 10,
      paddingTop: 8, borderTop: '1px solid var(--border-subtle)',
      fontSize: 11, color: 'var(--text-tertiary)',
    }}>{children}</div>
  );
}
