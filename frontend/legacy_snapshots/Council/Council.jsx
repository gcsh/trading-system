/**
 * Council — 5-agent panel + Shadow comparison + Promotion readiness.
 *
 * Three scrolling sections. Anchor IDs (#shadow, #promotion) supported so
 * old /shadow and /trial bookmarks still work after redirect.
 */
import React, { useEffect } from 'react';
import CouncilOverview from './CouncilOverview.jsx';
import ShadowComparison from './ShadowComparison.jsx';
import Trial from './Trial.jsx';

function SectionHeader({ id, icon, title, sub }) {
  return (
    <div id={id} style={{
      display: 'flex', alignItems: 'baseline', gap: 12,
      padding: '20px 4px 12px', borderTop: '1px solid var(--border)',
      marginTop: 24,
    }}>
      <div style={{ fontSize: 22 }}>{icon}</div>
      <div>
        <div style={{
          fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
          color: 'var(--muted)', fontWeight: 600,
        }}>{sub}</div>
        <h2 style={{ margin: 0 }}>{title}</h2>
      </div>
    </div>
  );
}

export default function Council() {
  // Anchor-scroll to the right section when arriving via redirect.
  useEffect(() => {
    const hash = window.location.hash?.replace('#', '');
    if (!hash) return;
    const el = document.getElementById(hash);
    if (el) setTimeout(() => el.scrollIntoView({ behavior: 'smooth' }), 100);
  }, []);

  return (
    <>
      <CouncilOverview />
      <SectionHeader
        id="shadow" icon="🪞"
        sub="Decision quality" title="Shadow comparison"
      />
      <ShadowComparison />
      <SectionHeader
        id="promotion" icon="⛩️"
        sub="9-gate contract" title="Promotion readiness"
      />
      <Trial />
    </>
  );
}
