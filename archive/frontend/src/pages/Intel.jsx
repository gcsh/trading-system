/**
 * Intel — tabs for the market-context surfaces (Markets, GEX, Flow,
 * Earnings, Sources, AI Signals). Lazy-mount each tab so we don't pay
 * the data-fetch cost for tabs the operator isn't looking at.
 *
 * URL: /intel?tab=gex (etc). Persists across reloads.
 */
import React, { Suspense, lazy } from 'react';
import { useSearchParams } from 'react-router-dom';

const Markets = lazy(() => import('./Market.jsx'));
// Phase B (2026-06-14) — GEX tab now uses the V2 dashboard (dark-neon
// design, freshness pill, 398-strike chart). The legacy Heatseeker.jsx
// is archived but kept on disk for now; remove on the next round.
const GEX = lazy(() => import('../v2/pages/GexDashboard.jsx'));
const Flow = lazy(() => import('./Flowseeker.jsx'));
const Earnings = lazy(() => import('./EarningsIntel.jsx'));
const Sources = lazy(() => import('./SourceAttribution.jsx'));
const AISignals = lazy(() => import('./AISignals.jsx'));
const IVRegime = lazy(() => import('./IVRegime.jsx'));
const CohortMatrix = lazy(() => import('./CohortMatrix.jsx'));

const TABS = [
  { id: 'markets', label: 'Markets', icon: '🌐', Component: Markets },
  { id: 'gex', label: 'GEX', icon: '🔥', Component: GEX },
  { id: 'flow', label: 'Flow', icon: '🌊', Component: Flow },
  { id: 'iv-regime', label: 'IV Regime', icon: '📈', Component: IVRegime },
  { id: 'cohort', label: 'Cohort Matrix', icon: '🧮', Component: CohortMatrix },
  { id: 'earnings', label: 'Earnings', icon: '📞', Component: Earnings },
  { id: 'sources', label: 'Sources', icon: '📊', Component: Sources },
  { id: 'ai', label: 'AI Signals', icon: '⚡', Component: AISignals },
];

export default function Intel() {
  const [sp, setSp] = useSearchParams();
  const active = sp.get('tab') || 'markets';
  const ActiveTab = (TABS.find((t) => t.id === active) || TABS[0]).Component;
  return (
    <>
      <div className="row" style={{
        gap: 6, marginBottom: 18, flexWrap: 'wrap',
        padding: '4px', background: 'var(--panel-2)',
        border: '1px solid var(--border)', borderRadius: 10,
      }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setSp({ tab: t.id }, { replace: true })}
            className={`btn small ${active === t.id ? 'primary' : ''}`}
            style={{
              background: active === t.id ? undefined : 'transparent',
              border: active === t.id ? undefined : '1px solid transparent',
            }}
          >
            <span style={{ marginRight: 6 }}>{t.icon}</span>{t.label}
          </button>
        ))}
      </div>
      <Suspense fallback={<div className="empty"><div className="title">Loading…</div></div>}>
        <ActiveTab />
      </Suspense>
    </>
  );
}
