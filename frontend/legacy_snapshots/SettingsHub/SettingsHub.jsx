/**
 * Settings hub — left sub-nav across the configuration surfaces:
 *   • General   (broker, signals, environment, API keys)
 *   • Watchlist (tickers the operator cares about)
 *   • Risk      (loss limits, stop-losses, position caps)
 *   • Alerts    (bot events)
 *
 * URL: /settings?section=watchlist (etc). Anchor-scroll support too.
 */
import React, { useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import Settings from './Settings.jsx';
import WatchlistPage from './WatchlistPage.jsx';
import Risk from './Risk.jsx';
import AlertsPage from './AlertsPage.jsx';
import TelegramSettings from './TelegramSettings.jsx';
import DetectorSettings from './DetectorSettings.jsx';

const SECTIONS = [
  { id: 'general', label: 'General', icon: '⚙️', Component: Settings },
  { id: 'watchlist', label: 'Watchlist', icon: '⭐', Component: WatchlistPage },
  { id: 'detectors', label: 'Detectors', icon: '🔍', Component: DetectorSettings },
  { id: 'risk', label: 'Risk & Safety', icon: '🛡️', Component: Risk },
  { id: 'alerts', label: 'Alerts', icon: '🔔', Component: AlertsPage },
  { id: 'telegram', label: 'Telegram', icon: '📱', Component: TelegramSettings },
];

export default function SettingsHub() {
  const [sp, setSp] = useSearchParams();
  // Support both ?section= and #anchor for old-bookmark compatibility.
  const sectionFromHash = (typeof window !== 'undefined'
    ? window.location.hash?.replace('#', '') : '') || '';
  const active = sp.get('section') || sectionFromHash || 'general';
  const ActiveSection = (SECTIONS.find((s) => s.id === active) || SECTIONS[0]).Component;

  // If we arrived via #hash, mirror it into ?section= so the URL is canonical.
  useEffect(() => {
    if (sectionFromHash && !sp.get('section')) {
      setSp({ section: sectionFromHash }, { replace: true });
    }
  }, [sectionFromHash, sp, setSp]);

  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '200px 1fr',
      gap: 18, alignItems: 'start',
    }}>
      <aside style={{
        position: 'sticky', top: 16,
        padding: 10, background: 'var(--panel-2)',
        border: '1px solid var(--border)', borderRadius: 12,
        display: 'flex', flexDirection: 'column', gap: 4,
      }}>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            onClick={() => setSp({ section: s.id }, { replace: true })}
            className="row"
            style={{
              gap: 10, padding: '9px 12px', border: 'none',
              background: active === s.id ? 'var(--panel)' : 'transparent',
              color: active === s.id ? 'var(--text)' : 'var(--text-soft)',
              borderRadius: 8, cursor: 'pointer',
              fontSize: 13, fontWeight: active === s.id ? 600 : 500,
              borderLeft: active === s.id
                ? '3px solid var(--accent)'
                : '3px solid transparent',
              textAlign: 'left', justifyContent: 'flex-start',
            }}
          >
            <span style={{ fontSize: 14 }}>{s.icon}</span>
            <span>{s.label}</span>
          </button>
        ))}
      </aside>
      <div style={{ minWidth: 0 }}>
        <ActiveSection />
      </div>
    </div>
  );
}
