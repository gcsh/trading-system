/* MITS Phase 19 Cluster D — Safety Flags (/v2/settings/flags).
 *
 * Read-only dashboard for the 5+ safety flags exposed via /learning/flags.
 *
 * Flag groups:
 *   Decision Layer  — decision_rollback_enabled
 *   Learning Layer  — policy_tuning_advisory / policy_tuning_auto_apply
 *                     adaptive_weights_advisory / adaptive_weights_apply
 *                     learning_backfill_enabled
 *
 * The page does NOT toggle any flag. Each row carries an inline "how to
 * flip" .env command + the systemctl restart line.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Pill, Section, EmptyState, AlertBanner,
} from '../../design/Components.jsx';
import FlagsTable, { FLAG_META } from '../components/FlagsTable.jsx';

const POLL_MS = 60_000;

export default function SettingsFlags() {
  const [flags, setFlags] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function fetchFlags() {
      try {
        const r = await fetch('/learning/flags');
        if (!r.ok) throw new Error(`${r.status}`);
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('json')) throw new Error('non-JSON');
        const j = await r.json();
        if (!cancelled) { setFlags(j); setErr(null); }
      } catch (e) {
        if (!cancelled) { setFlags(null); setErr(`/learning/flags failed: ${e.message}`); }
      }
    }
    fetchFlags();
    const id = setInterval(fetchFlags, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const groups = useMemo(() => {
    if (!flags) return {};
    const m = {};
    for (const k of Object.keys(flags)) {
      const g = FLAG_META[k]?.group || 'Other';
      if (!m[g]) m[g] = {};
      m[g][k] = flags[k];
    }
    return m;
  }, [flags]);

  const allOff = flags && Object.values(flags).every(v => !v);
  const total = flags ? Object.keys(flags).length : 0;
  const on = flags ? Object.values(flags).filter(Boolean).length : 0;

  return (
    <div className="v2-root v2-flg">
      <Section title="Safety Flags"
               subtitle={flags ? `${on}/${total} flags ON` : 'Loading…'}>
        <AlertBanner severity="critical">
          <strong>Operator review required.</strong> Flipping these flags without a dry-run
          can cause real-money execution issues. Each flag has a description below — read
          it before changing. Auto-apply flags also require the matching advisory flag to be on.
        </AlertBanner>

        {err && <AlertBanner severity="warning">{err}</AlertBanner>}

        {flags && allOff && (
          <AlertBanner severity="info">
            All flags currently OFF — the system is in "advisory-only learning" mode.
            Policy thresholds + agent weights remain frozen at the baseline shipped with
            the deployed build.
          </AlertBanner>
        )}

        {!flags && !err && <EmptyState icon="⚑" message="Loading flag state…" />}

        {flags && Object.entries(groups).map(([groupName, groupFlags]) => (
          <Card key={groupName}>
            <h3 className="v2-flg-h3">{groupName}</h3>
            <div className="v2-flg-meta">
              {Object.keys(groupFlags).length} flag{Object.keys(groupFlags).length === 1 ? '' : 's'} in this group
            </div>
            <FlagsTable flags={groupFlags} group={groupName} />
          </Card>
        ))}

        {flags && Object.keys(flags).length === 0 && (
          <EmptyState icon="∅" message="No safety flags exposed by /learning/flags." />
        )}

        {/* Footer reference card */}
        <Card>
          <h3 className="v2-flg-h3">Flag Reference</h3>
          <div className="v2-flg-ref">
            <div>
              <Pill tone="success">low</Pill> advisory only — surfaces a number to the operator, no live behaviour change.
            </div>
            <div>
              <Pill tone="info">medium</Pill> affects nightly learning / data ingestion. No live execution change.
            </div>
            <div>
              <Pill tone="warning">high</Pill> changes live decision behaviour or order routing on next engine restart.
            </div>
          </div>
        </Card>
      </Section>

      <style>{`
        .v2-flg { padding: var(--space-4) var(--space-6); }
        .v2-flg-h3 {
          font-size: var(--font-size-base);
          font-weight: 700;
          color: var(--text-primary);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin: 0 0 var(--space-2);
        }
        .v2-flg-meta {
          font-size: 11px;
          color: var(--text-tertiary);
          margin-bottom: 12px;
        }
        .v2-flg-ref {
          display: flex; flex-direction: column; gap: 8px;
          font-size: var(--font-size-sm);
          color: var(--text-secondary);
        }
        .v2-flg-ref > div {
          display: flex; align-items: center; gap: 8px;
        }
      `}</style>
    </div>
  );
}
