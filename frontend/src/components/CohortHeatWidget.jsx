import React, { useEffect, useState } from 'react';

/**
 * Cockpit Cohort Heat widget — visualizes per-theme edge heat from
 * /cohorts/theme-heat. Each theme renders as a colored bar:
 *   green = hot (size multiplier > 1.0, recent cohort beats baseline)
 *   red   = cold (size multiplier < 1.0, recent cohort below baseline)
 *   gray  = neutral (no significant edge)
 *
 * The size multiplier is the same number the portfolio optimizer applies
 * to size requests, so the user sees DIRECTLY what's happening to their
 * positions.
 */
function HeatRow({ heat }) {
  const m = heat.size_multiplier;
  const pct = (m - 1.0) * 100;             // -30% .. +30%
  const isHot = m > 1.005;
  const isCold = m < 0.995;
  const color = isHot ? 'var(--accent, #38a169)'
              : isCold ? 'var(--danger, #e53e3e)'
              : 'var(--muted)';
  const bg = isHot ? 'var(--accent-soft)'
           : isCold ? 'var(--danger-soft)'
           : 'var(--panel-2)';
  // bar width: scale |pct| up to 100% of inner box
  const barWidth = Math.min(100, Math.abs(pct) * 3.33);   // 30% → 100%

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '180px 1fr 80px',
      gap: 10, alignItems: 'center',
      padding: '6px 0', borderBottom: '1px dashed var(--border)',
    }}>
      <div>
        <div style={{ fontWeight: 600, fontSize: 12.5 }}>{heat.theme}</div>
        <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>
          closed {heat.closed} · win {(heat.win_rate * 100).toFixed(0)}%
        </div>
      </div>
      <div style={{
        position: 'relative', height: 16, background: bg,
        borderRadius: 4, border: `1px solid ${color}`,
      }}>
        <div style={{
          position: 'absolute', top: 0, bottom: 0,
          [isCold ? 'right' : 'left']: '50%',
          width: `${barWidth / 2}%`, background: color,
          borderRadius: 3,
        }} />
        <div style={{
          position: 'absolute', top: 0, bottom: 0, left: '50%',
          width: 1, background: 'var(--border-strong)',
        }} />
      </div>
      <div style={{
        textAlign: 'right', fontWeight: 700, fontSize: 12.5,
        color, fontFeatureSettings: '"tnum"',
      }}>
        {pct >= 0 ? '+' : ''}{pct.toFixed(1)}%
      </div>
    </div>
  );
}

export default function CohortHeatWidget() {
  const [body, setBody] = useState(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/cohorts/theme-heat?recent_n=50')
      .then((r) => r.ok ? r.json() : null)
      .then((d) => { if (active && d) setBody(d); })
      .catch(() => {});
    load();
    const id = setInterval(load, 60 * 1000);   // refresh every minute
    return () => { active = false; clearInterval(id); };
  }, []);

  if (!body) return null;
  const heats = body.heats || [];

  return (
    <div className="panel" style={{ padding: '12px 14px', background: 'var(--bg-elev)' }}>
      <div className="panel-head">
        <h2 style={{ margin: 0, fontSize: 14 }}>🔥 Cohort heat — applied size multipliers</h2>
        <span className="panel-sub">
          per-theme edge over the last {body.recent_n} closed trades
        </span>
      </div>

      {heats.length === 0 ? (
        <div style={{
          color: 'var(--muted)', fontSize: 12.5, padding: 12,
          background: 'var(--panel-2)', borderRadius: 6, marginTop: 8,
        }}>
          No theme has ≥ 8 closed trades yet. Heat scores need a sample to
          measure edge against; this panel will populate as cohorts accumulate.
        </div>
      ) : (
        <div style={{ marginTop: 10 }}>
          {heats.map((h, i) => <HeatRow key={i} heat={h} />)}
          <div style={{
            marginTop: 8, fontSize: 11, color: 'var(--muted)',
          }}>
            Bars show the size multiplier the portfolio optimizer applies on
            top of every other cap. Cold themes (red) shrink size; hot themes
            (green) expand it up to ±30%.
          </div>
        </div>
      )}
    </div>
  );
}
