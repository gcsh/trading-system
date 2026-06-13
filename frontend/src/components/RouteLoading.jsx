import React from 'react';

/**
 * Perf-Fix Pass — minimal route-transition fallback shown by <Suspense>
 * while a lazy-loaded page chunk is being fetched.
 *
 * Looks like the page is just rendering (not "loading"), so the operator
 * doesn't get yanked out of context every nav. Uses existing panel +
 * tokens from styles.css so it inherits the dark/light theme automatically.
 *
 * NOTE: Deliberately NOT a spinner. Spinners imply "wait", which sets the
 * wrong expectation for a sub-second lazy import. A faint skeleton is the
 * correct visual treatment.
 */
export default function RouteLoading() {
  return (
    <div
      style={{
        padding: 16,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        animation: 'tb-route-pulse 1.2s ease-in-out infinite',
      }}
      aria-busy="true"
      aria-label="Loading page"
    >
      <style>{`
        @keyframes tb-route-pulse {
          0%, 100% { opacity: 0.55; }
          50%      { opacity: 0.85; }
        }
        .tb-skel-block {
          background: var(--panel, #1a1d24);
          border: 1px solid var(--border, #2a2e38);
          border-radius: 10px;
        }
      `}</style>
      <div className="tb-skel-block" style={{ height: 56 }} />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
        <div className="tb-skel-block" style={{ height: 110 }} />
        <div className="tb-skel-block" style={{ height: 110 }} />
        <div className="tb-skel-block" style={{ height: 110 }} />
      </div>
      <div className="tb-skel-block" style={{ height: 360 }} />
    </div>
  );
}
