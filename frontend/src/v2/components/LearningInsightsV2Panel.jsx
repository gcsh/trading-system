/* MITS Phase 19 Stream 3 — LearningInsightsV2Panel.
 *
 * Renders `learning_insights`:
 *   { attribution_summary, active_policy_recommendations,
 *     active_weight_proposals, funnel_snapshot }
 *
 * 2x2 grid of cards. Each clickable → deep-dive routes:
 *   attribution           → /v2/hypothesis-studio (placeholder for now)
 *   active_policy         → /v2/hypothesis-studio
 *   active_weights        → /v2/hypothesis-studio
 *   funnel_snapshot       → /v2/learning/funnel
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { Card, EmptyState, Pill, Stat } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

function SubCard({ title, to, tone, children, hint }) {
  const inner = (
    <div style={{
      padding: 8, background: 'var(--bg-tertiary)',
      border: `1px solid var(--border-subtle)`,
      borderRadius: 4, cursor: to ? 'pointer' : 'default',
      transition: 'background 100ms ease, border-color 100ms ease',
      height: '100%',
    }} title={hint}>
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
  return to ? (
    <Link to={to} style={{ textDecoration: 'none', color: 'inherit' }}>
      {inner}
    </Link>
  ) : inner;
}

function fmtAge(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60)   return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export default function LearningInsightsV2Panel({ insights }) {
  if (!insights) {
    return (
      <Card>
        <PanelHead title="Learning insights" subtitle="attribution + tuning + funnel" />
        <EmptyState message="No learning insights for this decision (18.A-D passes not run, or compute failed)." />
      </Card>
    );
  }

  const attr   = insights.attribution_summary;
  const policy = insights.active_policy_recommendations;
  const weight = insights.active_weight_proposals;
  const funnel = insights.funnel_snapshot;

  return (
    <Card>
      <PanelHead
        title="Learning insights"
        subtitle="from /learning attribution + tuning + funnel"
        right={
          <>
            {policy?.advisory_enabled && (
              <Pill tone={policy.auto_apply_enabled ? 'success' : 'warning'}>
                policy {policy.auto_apply_enabled ? 'auto-apply' : 'advisory'}
              </Pill>
            )}
            {weight?.advisory_enabled && (
              <Pill tone={weight.apply_enabled ? 'success' : 'warning'}>
                weights {weight.apply_enabled ? 'apply' : 'advisory'}
              </Pill>
            )}
          </>
        }
      />
      <div style={{
        display: 'grid', gap: 8,
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
      }}>
        <SubCard
          title="Attribution"
          to="/v2/hypothesis-studio"
          tone={attr?.n_rows >= 30 ? <Pill tone="success">n={attr.n_rows}</Pill> : <Pill tone="warning">insufficient</Pill>}
          hint="How well each agent's predictions have matched outcomes (Brier / ECE per scope).">
          {attr ? (
            <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
              <Stat label="Window"   value={attr.window_days != null ? `${attr.window_days}d` : '—'} mono />
              <Stat label="Rows"     value={attr.n_rows != null ? attr.n_rows : '—'} mono />
              {attr.by_scope && (
                <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 4 }}>
                  by scope:{' '}
                  {Object.entries(attr.by_scope).map(([k, v]) => `${k}=${v}`).join(' · ')}
                </div>
              )}
            </div>
          ) : <EmptyState icon="∅" message="No attribution yet" />}
        </SubCard>

        <SubCard
          title="Policy tuning"
          to="/v2/hypothesis-studio"
          tone={<Pill tone={policy?.auto_apply_enabled ? 'success' : policy?.advisory_enabled ? 'warning' : 'neutral'}>
            {policy?.auto_apply_enabled ? 'auto' : policy?.advisory_enabled ? 'advisory' : 'off'}
          </Pill>}
          hint="Recommendations to tweak the 30 policy rules' thresholds. Advisory-only by default.">
          {policy ? (
            <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
              <Stat label="Recs"      value={policy.n_recommendations || 0} mono />
              <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 4 }}>
                {policy.computed_at ? fmtAge(policy.computed_at) : 'never run'}
              </div>
            </div>
          ) : <EmptyState icon="∅" message="No policy recs" />}
        </SubCard>

        <SubCard
          title="Weight proposals"
          to="/v2/hypothesis-studio"
          tone={<Pill tone={weight?.apply_enabled ? 'success' : weight?.advisory_enabled ? 'warning' : 'neutral'}>
            {weight?.apply_enabled ? 'live' : weight?.advisory_enabled ? 'advisory' : 'off'}
          </Pill>}
          hint="Adaptive per-agent weight adjustments. Bayesian-shrunk from realised outcomes.">
          {weight ? (
            <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
              <Stat label="Proposals" value={weight.n_proposals || 0} mono />
              <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 4 }}>
                {(weight.known_agents || []).length} agents tracked
              </div>
            </div>
          ) : <EmptyState icon="∅" message="No weight proposals" />}
        </SubCard>

        <SubCard
          title="Funnel snapshot"
          to="/v2/learning/funnel"
          tone={funnel?.n_submitted > 0
            ? <Pill tone="success">{funnel.n_submitted} submitted</Pill>
            : <Pill tone="error">0 submitted</Pill>}
          hint="Today's decision throughput from watchlist → submitted trade. The 'smoking gun' analysis is at /v2/learning/funnel.">
          {funnel ? (
            <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
              <Stat label="Evals"     value={funnel.n_evaluations != null ? Number(funnel.n_evaluations).toLocaleString() : '—'} mono />
              <Stat label="Submitted" value={funnel.n_submitted != null ? funnel.n_submitted : '—'} mono />
              <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 4 }}>
                pass rate{' '}
                {funnel.n_evaluations && funnel.n_submitted != null
                  ? `${((Number(funnel.n_submitted) / Number(funnel.n_evaluations)) * 100).toFixed(3)}%`
                  : '—'}
              </div>
            </div>
          ) : <EmptyState icon="∅" message="No funnel today" />}
        </SubCard>
      </div>

      <Footer>
        <span>Click any panel → deep-dive</span>
        <span className="mono" style={{ color: 'var(--text-tertiary)' }}>
          {attr?.computed_at ? `attribution ${fmtAge(attr.computed_at)}` : ''}
        </span>
      </Footer>
    </Card>
  );
}
