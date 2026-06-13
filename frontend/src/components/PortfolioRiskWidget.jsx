import React, { useEffect, useState } from 'react';
import { money, pct } from '../lib/format.js';

const RISK_COLORS = {
  HIGH:     { bg: 'var(--danger-soft)', fg: 'var(--danger)', border: 'var(--danger)' },
  MODERATE: { bg: 'rgba(214,158,46,0.18)', fg: 'var(--warn)', border: 'var(--warn)' },
  LOW:      { bg: 'var(--accent-soft)', fg: 'var(--accent)', border: 'var(--accent)' },
};

export default function PortfolioRiskWidget() {
  const [risk, setRisk] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/portfolio/risk')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (active && d) setRisk(d); })
      .catch((e) => active && setErr(String(e)));
    load();
    const id = setInterval(load, 8000);
    return () => { active = false; clearInterval(id); };
  }, []);

  if (!risk || risk.positions_count === 0) {
    return (
      <div className="panel">
        <div className="panel-head">
          <h2>🛡️ Portfolio risk</h2>
          <span className="panel-sub">{risk ? 'all cash' : (err ? 'unavailable' : 'loading…')}</span>
        </div>
        <div className="empty" style={{ padding: '20px 12px' }}>
          <div className="hint">Open positions will show concentration, theme overlap, net beta and macro-risk here.</div>
        </div>
      </div>
    );
  }

  const color = RISK_COLORS[risk.macro_risk] || RISK_COLORS.LOW;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2>🛡️ Portfolio risk</h2>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 4, fontWeight: 700, fontSize: 11,
          padding: '2px 9px', borderRadius: 4,
          background: color.bg, color: color.fg, border: `1px solid ${color.border}`,
        }}>{risk.macro_risk}</span>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, marginBottom: 12 }}>
        <Stat label="Account deployed" value={money(risk.total_market_value)} sub={`${risk.positions_count} positions`} />
        <Stat label="Net β" value={(risk.net_beta || 0).toFixed(2)} sub={risk.net_beta > 1.5 ? 'high' : risk.net_beta > 1.2 ? 'elevated' : 'normal'} />
        <Stat label="Diversification" value={`${Math.round((risk.diversification || 0) * 100)}%`} sub={risk.diversification > 0.7 ? 'broad' : risk.diversification > 0.5 ? 'moderate' : 'narrow'} />
        <Stat label="Net Δ" value={money(risk.net_delta || 0)} sub={'long-biased'} />
      </div>

      {risk.concentration_flags?.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
          {risk.concentration_flags.map((f, i) => (
            <span key={i} className="pill" style={{ fontSize: 11, color: 'var(--warn)', borderColor: 'var(--warn)' }}>⚠ {f}</span>
          ))}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 12.5 }}>
        <div>
          <div style={{ color: 'var(--muted)', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }}>By sector</div>
          {topNFromMap(risk.by_sector).map(([sec, info]) => (
            <BarRow key={sec} label={sec} pct={info.pct} />
          ))}
        </div>
        <div>
          <div style={{ color: 'var(--muted)', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }}>By theme</div>
          {Object.keys(risk.by_theme || {}).length === 0 ? (
            <div style={{ color: 'var(--muted)', fontSize: 11 }}>no thematic overlap</div>
          ) : (
            topNFromMap(risk.by_theme).map(([theme, info]) => (
              <BarRow key={theme} label={theme} pct={info.pct} sub={info.tickers?.join(', ')} />
            ))
          )}
        </div>
      </div>

      {risk.correlation_clusters?.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-soft)' }}>
          <span style={{ color: 'var(--muted)' }}>Correlation clusters · </span>
          {risk.correlation_clusters.slice(0, 3).map((c, i) => (
            <span key={i} style={{ marginRight: 10 }}>
              <strong>{c.label}</strong> ({c.tickers.join(', ')})
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, sub }) {
  return (
    <div style={{ minWidth: 90 }}>
      <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.04em', fontWeight: 600 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 18, fontFeatureSettings: '"tnum"' }}>{value}</div>
      {sub != null && <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>{sub}</div>}
    </div>
  );
}

function BarRow({ label, pct: p, sub }) {
  const width = Math.min(100, (p || 0) * 100);
  return (
    <div style={{ marginBottom: 4 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
        <span>{label}</span><span style={{ color: 'var(--muted)' }}>{Math.round(width)}%</span>
      </div>
      <div style={{ height: 5, background: 'var(--panel-2)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${width}%`, height: '100%', background: 'var(--info)' }} />
      </div>
      {sub && <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>{sub}</div>}
    </div>
  );
}

function topNFromMap(obj, n = 4) {
  return Object.entries(obj || {})
    .sort((a, b) => (b[1].pct || 0) - (a[1].pct || 0))
    .slice(0, n);
}
