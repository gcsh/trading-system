import React, { useEffect, useState } from 'react';

const RISK_STYLE = {
  HIGH:     { bg: 'var(--danger-soft)', fg: 'var(--danger)', border: 'var(--danger)' },
  MODERATE: { bg: 'rgba(214,158,46,0.18)', fg: 'var(--warn)', border: 'var(--warn)' },
  LOW:      { bg: 'var(--accent-soft)', fg: 'var(--accent)', border: 'var(--accent)' },
};

export default function NarrativeStrip() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/narrative')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (active && d) setData(d); })
      .catch(() => {});
    load();
    const id = setInterval(load, 5 * 60 * 1000);   // narrative shifts slowly
    return () => { active = false; clearInterval(id); };
  }, []);

  if (!data) return null;
  const risk = RISK_STYLE[data.macro_risk] || RISK_STYLE.LOW;
  const sourcePill = data.source === 'claude' ? '🧠 Claude' : '📰 heuristic';

  return (
    <div className="panel" style={{ padding: '10px 14px', background: 'var(--bg-elev)' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 18 }}>📰</span>
        <div style={{ minWidth: 160 }}>
          <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600 }}>Today's narrative</div>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{data.dominant_theme || '—'}</div>
        </div>
        <span style={{
          fontWeight: 700, fontSize: 10.5, padding: '2px 9px', borderRadius: 4,
          background: risk.bg, color: risk.fg, border: `1px solid ${risk.border}`,
        }}>Macro {data.macro_risk || 'LOW'}</span>
        {data.beneficiaries?.length > 0 && (
          <div style={{ fontSize: 12, color: 'var(--text-soft)' }}>
            <span style={{ color: 'var(--muted)' }}>Beneficiaries · </span>
            {data.beneficiaries.slice(0, 5).map((t, i) => (
              <span key={t} style={{ fontWeight: 700, marginRight: 6 }}>{t}{i < data.beneficiaries.length - 1 ? ',' : ''}</span>
            ))}
          </div>
        )}
        <span style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--muted)' }}>{sourcePill} · {data.headlines_seen || 0} headlines</span>
      </div>
      {data.summary && (
        <div style={{ fontSize: 12.5, color: 'var(--text-soft)', marginTop: 6, lineHeight: 1.4 }}>{data.summary}</div>
      )}
    </div>
  );
}
