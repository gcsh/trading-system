/* MITS Phase 19 Cluster D — FlagsTable.
 *
 * Read-only table of safety flags from /learning/flags. Each row shows:
 *   - name (humanised)
 *   - status: ON (green) / OFF (red)
 *   - env-var name (uppercase form used in /opt/trading-bot/.env)
 *   - description: plain-English meaning of the flag
 *   - how-to-flip: copy-paste-able env line
 *
 * Props:
 *   flags:    { flag_name: bool }
 *   metadata: optional { flag_name: { description, env, group, danger } }
 *             if not provided, the built-in FLAG_META below is used.
 *   group:    optional filter by group label
 */
import React from 'react';
import { Pill, EmptyState } from '../../design/Components.jsx';

// Built-in metadata for the 5 known safety flags. Operator can override
// via the `metadata` prop if the backend grows additional flags.
export const FLAG_META = {
  decision_rollback_enabled: {
    label: 'Decision Rollback',
    env: 'DECISION_ROLLBACK_ENABLED',
    group: 'Decision Layer',
    danger: 'high',
    description:
      'Allow the operator to roll back a recently-made decision. ' +
      'Touches live order routing — only flip after running the rollback ' +
      'in dry-run mode first.',
  },
  policy_tuning_advisory_enabled: {
    label: 'Policy Tuning (Advisory)',
    env: 'POLICY_TUNING_ADVISORY_ENABLED',
    group: 'Learning Layer',
    danger: 'low',
    description:
      'Compute suggested policy-threshold deltas nightly and surface ' +
      'them in the cockpit. Read-only — no thresholds actually change.',
  },
  policy_tuning_auto_apply_enabled: {
    label: 'Policy Tuning (Auto-Apply)',
    env: 'POLICY_TUNING_AUTO_APPLY_ENABLED',
    group: 'Learning Layer',
    danger: 'high',
    description:
      'AUTOMATICALLY apply advisory policy-threshold deltas at next ' +
      'engine restart. Changes real decision behaviour. Requires ' +
      'advisory flag also ON.',
  },
  adaptive_weights_advisory_enabled: {
    label: 'Adaptive Weights (Advisory)',
    env: 'ADAPTIVE_WEIGHTS_ADVISORY_ENABLED',
    group: 'Learning Layer',
    danger: 'low',
    description:
      'Compute per-agent weight multipliers based on rolling Brier ' +
      'attribution. Surfaced in Hypothesis Studio only — no agent weight ' +
      'is actually changed.',
  },
  adaptive_weights_apply_enabled: {
    label: 'Adaptive Weights (Apply)',
    env: 'ADAPTIVE_WEIGHTS_APPLY_ENABLED',
    group: 'Learning Layer',
    danger: 'high',
    description:
      'APPLY the computed agent-weight multipliers at next engine ' +
      'restart. Changes how each agent contributes to consensus. ' +
      'Requires advisory flag also ON.',
  },
  learning_backfill_enabled: {
    label: 'Learning Backfill',
    env: 'LEARNING_BACKFILL_ENABLED',
    group: 'Learning Layer',
    danger: 'medium',
    description:
      'Allow the nightly backfill job to ingest closed trades into the ' +
      'learning store. Off means new closed trades are not learned from.',
  },
};

function fmtLabel(key) {
  const meta = FLAG_META[key];
  if (meta) return meta.label;
  return key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

export default function FlagsTable({ flags = {}, metadata = {}, group }) {
  const merged = { ...FLAG_META, ...metadata };
  const entries = Object.entries(flags || {}).filter(([k]) =>
    !group || (merged[k]?.group === group)
  );

  if (entries.length === 0) {
    return <EmptyState icon="⚑" message="No safety flags returned." />;
  }

  return (
    <div className="v2-flags">
      {entries.map(([name, val]) => {
        const meta = merged[name] || {};
        const env = meta.env || name.toUpperCase();
        const desc = meta.description || `Backend flag: ${name}`;
        const danger = meta.danger || 'low';
        return (
          <div key={name} className={`v2-flags__row v2-flags__row--${danger}`}>
            <div className="v2-flags__head">
              <div className="v2-flags__name">{fmtLabel(name)}</div>
              <div className="v2-flags__pills">
                <Pill tone={val ? 'success' : 'error'} size="md">
                  {val ? 'ON' : 'OFF'}
                </Pill>
                {danger === 'high' && <Pill tone="warning" size="sm">High Impact</Pill>}
                {danger === 'medium' && <Pill tone="info" size="sm">Medium</Pill>}
              </div>
            </div>
            <div className="v2-flags__env mono">{env}</div>
            <div className="v2-flags__desc">{desc}</div>
            <div className="v2-flags__howto">
              <span className="v2-flags__howto-lbl">How to flip:</span>
              <code className="v2-flags__howto-cmd mono">
                {env}={val ? '0' : '1'}
              </code>
              <span className="v2-flags__howto-where">
                in <code className="mono">/opt/trading-bot/.env</code>, then
                <code className="mono"> sudo systemctl restart trading-bot.service</code>
              </span>
            </div>
          </div>
        );
      })}
      <style>{`
        .v2-flags {
          display: flex; flex-direction: column; gap: 10px;
        }
        .v2-flags__row {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-subtle);
          border-left: 3px solid var(--border-default);
          border-radius: var(--radius-md);
          padding: 14px 16px;
        }
        .v2-flags__row--high { border-left-color: var(--accent-red); }
        .v2-flags__row--medium { border-left-color: var(--accent-yellow); }
        .v2-flags__row--low { border-left-color: var(--accent-green); }
        .v2-flags__head {
          display: flex; align-items: center; justify-content: space-between;
          gap: 12px;
          margin-bottom: 6px;
        }
        .v2-flags__name {
          font-weight: 700;
          font-size: var(--font-size-base);
          color: var(--text-primary);
        }
        .v2-flags__pills { display: flex; gap: 6px; align-items: center; }
        .v2-flags__env {
          font-size: 11px;
          color: var(--accent-cyan);
          letter-spacing: 0.02em;
          margin-bottom: 6px;
        }
        .v2-flags__desc {
          font-size: var(--font-size-sm);
          color: var(--text-secondary);
          line-height: 1.5;
          margin-bottom: 8px;
        }
        .v2-flags__howto {
          font-size: 11px;
          color: var(--text-tertiary);
          padding-top: 6px;
          border-top: 1px dashed var(--border-subtle);
          display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
        }
        .v2-flags__howto-lbl { font-weight: 600; color: var(--text-tertiary); }
        .v2-flags__howto-cmd {
          background: var(--bg-primary);
          padding: 2px 6px;
          border-radius: 3px;
          color: var(--accent-yellow);
        }
        .v2-flags__howto-where code {
          background: var(--bg-primary);
          padding: 1px 4px;
          border-radius: 3px;
          color: var(--text-secondary);
        }
      `}</style>
    </div>
  );
}
