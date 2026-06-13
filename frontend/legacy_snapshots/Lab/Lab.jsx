/**
 * Lab — Strategy workspace + analytics tabs.
 *
 * Tabs:
 *   strategies — the existing strategy builder/list
 *   calibration — Brier/ECE reliability diagram (P2 + live)
 *   curated — institutional curated-rule audit (P2.2)
 *   autopsy — losing-trade autopsy memos (Stage-17 / Stage-9)
 */
import React, { Suspense, lazy } from 'react';
import { useSearchParams } from 'react-router-dom';

const Strategies = lazy(() => import('./Strategies.jsx'));
const Calibration = lazy(() => import('./Calibration.jsx'));
const CuratedRules = lazy(() => import('./CuratedRules.jsx'));
const TradeAutopsy = lazy(() => import('./TradeAutopsy.jsx'));
const GateStack = lazy(() => import('./GateStack.jsx'));
const PricingTelemetry = lazy(() => import('./PricingTelemetry.jsx'));

const TABS = [
  { id: 'strategies', label: 'Strategies', icon: '🧪', Component: Strategies },
  { id: 'gates', label: 'Gate Stack', icon: '🚦', Component: GateStack },
  { id: 'pricing', label: 'Pricing', icon: '💰', Component: PricingTelemetry },
  { id: 'calibration', label: 'Calibration', icon: '🎯', Component: Calibration },
  { id: 'curated', label: 'Curated Rules', icon: '📜', Component: CuratedRules },
  { id: 'autopsy', label: 'Autopsy', icon: '🔬', Component: TradeAutopsy },
];

export default function Lab() {
  const [sp, setSp] = useSearchParams();
  const active = sp.get('tab') || 'strategies';
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
