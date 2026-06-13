import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import CumulativeGexPanel from './CumulativeGexPanel.jsx';
import ExpiryDecompositionPanel from './ExpiryDecompositionPanel.jsx';
import FlowIQPanel from './FlowIQPanel.jsx';

/**
 * Phase 19 — Heatseeker drill-in tabs.
 *
 * Sits beneath the hero heatmap and exposes the four legacy panels
 * (per-strike table, cumulative, by-expiry, flow) one at a time so they
 * don't compete with each other for visual weight. Each child component
 * is mounted lazily — only the active tab is rendered, so we don't pay
 * the network/CPU cost for the other three until the operator clicks in.
 *
 * Props:
 *   ticker      — symbol, threaded straight through to children.
 *   dte         — DTE filter from the page-level controls bar (not used
 *                 by every child today, but plumbed for future fan-out).
 *   rows        — per-strike rows already computed on Heatseeker so the
 *                 "Per Strike" tab matches the legacy GexTable view.
 *   spotStrike, callWall, putWall, flip — passed through to the table.
 *   tab/setTab  — RENDER the parent's GexTable inside Per Strike; the
 *                 table is a closure inside Heatseeker.jsx, so the parent
 *                 hands us the JSX as `perStrikeNode` rather than us
 *                 re-implementing it.
 *   perStrikeNode — render-prop for the existing per-strike panel JSX.
 *
 * URL state: `?tab=` query param so reloads/deep-links keep the tab.
 * Valid values: per-strike | cumulative | by-expiry | flow.
 */

const TABS = [
  { id: 'per-strike',  label: 'Per Strike' },
  { id: 'cumulative',  label: 'Cumulative' },
  { id: 'by-expiry',   label: 'By Expiry' },
  { id: 'flow',        label: 'Flow' },
];

const VALID = new Set(TABS.map((t) => t.id));

export default function GexDrillIns({
  ticker,
  dte,
  rows = [],
  spotStrike,
  callWall,
  putWall,
  flip,
  perStrikeNode,
}) {
  const [params, setParams] = useSearchParams();
  const urlTab = params.get('tab');
  const initial = VALID.has(urlTab) ? urlTab : 'per-strike';
  const [active, setActive] = useState(initial);

  // Reflect URL → state for back/forward nav.
  useEffect(() => {
    if (urlTab && VALID.has(urlTab) && urlTab !== active) setActive(urlTab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlTab]);

  const setTab = useCallback((id) => {
    setActive(id);
    const next = new URLSearchParams(params);
    if (id === 'per-strike') next.delete('tab');
    else next.set('tab', id);
    setParams(next, { replace: true });
  }, [params, setParams]);

  // Sub-stats per tab — sourced from the data we already have so we
  // don't fire extra requests. Each tab button can show a quick number
  // (e.g. row count) for a glanceable preview before clicking in.
  const subStats = useMemo(() => ({
    'per-strike': rows.length ? `${rows.length} strikes` : null,
    'cumulative': rows.length ? `${rows.length} rows` : null,
    'by-expiry':  null,   // child fetches its own; expose count via aria-live later
    'flow':       null,
  }), [rows]);

  return (
    <div className="panel" style={{ marginTop: 14, marginBottom: 0 }}>
      {/* Tab strip. Buttons styled with existing `btn` tokens so the
          look matches the rest of the page (no new classes). */}
      <div
        role="tablist"
        aria-label="GEX drill-ins"
        style={{
          display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap',
          borderBottom: '1px solid var(--border)', paddingBottom: 8,
        }}
      >
        {TABS.map((t) => {
          const isActive = t.id === active;
          const stat = subStats[t.id];
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={isActive}
              className={`btn small ${isActive ? 'primary' : ''}`}
              onClick={() => setTab(t.id)}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
            >
              <span>{t.label}</span>
              {stat && (
                <span style={{
                  fontSize: 10, color: isActive ? 'var(--bg)' : 'var(--muted)',
                  fontWeight: 600, fontFeatureSettings: '"tnum"',
                }}>
                  {stat}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Lazy panel slot — only the active tab is mounted. */}
      <div role="tabpanel" style={{ minHeight: 320 }}>
        {active === 'per-strike' && (
          perStrikeNode || (
            <div className="empty" style={{ padding: 20 }}>
              Per-strike view requires Heatseeker page context.
            </div>
          )
        )}
        {active === 'cumulative' && (
          <CumulativeGexPanel rows={rows} spotStrike={spotStrike} flip={flip} />
        )}
        {active === 'by-expiry' && (
          <ExpiryDecompositionPanel ticker={ticker} spotStrike={spotStrike} />
        )}
        {active === 'flow' && (
          <FlowIQPanel ticker={ticker} />
        )}
      </div>
    </div>
  );
}
