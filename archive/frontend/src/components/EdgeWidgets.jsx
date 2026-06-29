import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { money, shortTime } from '../lib/format.js';

function prem(v) {
  const n = Number(v) || 0;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

export function GexMini({ symbol = 'SPY' }) {
  const [g, setG] = useState(null);
  useEffect(() => {
    let active = true;
    const load = () => fetch(`/heatseeker/regime?symbol=${symbol}`).then((r) => r.ok && r.json()).then((d) => active && d && setG(d)).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { active = false; clearInterval(id); };
  }, [symbol]);

  const on = g?.dealer_regime === 'long_gamma';
  return (
    <div className="panel">
      <div className="panel-head"><h2>🔥 GEX — {symbol}</h2><Link to="/heatseeker" className="btn small ghost">open Heatseeker →</Link></div>
      <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'center' }}>
        <div><div style={{ fontSize: 11, color: 'var(--muted)' }}>Call wall</div><div style={{ fontWeight: 700, color: 'var(--accent)' }}>{g?.call_wall ? money(g.call_wall).replace(/\.00$/, '') : '—'}</div></div>
        <div><div style={{ fontSize: 11, color: 'var(--muted)' }}>Put wall</div><div style={{ fontWeight: 700, color: 'var(--danger)' }}>{g?.put_wall ? money(g.put_wall).replace(/\.00$/, '') : '—'}</div></div>
        <div><div style={{ fontSize: 11, color: 'var(--muted)' }}>Gamma flip</div><div style={{ fontWeight: 700, color: 'var(--warn)' }}>{g?.gamma_flip ? money(g.gamma_flip).replace(/\.00$/, '') : '—'}</div></div>
        <div style={{ marginLeft: 'auto' }}>
          <span className="pill" style={{ fontWeight: 700, color: on ? 'var(--accent)' : 'var(--danger)', borderColor: on ? 'var(--accent)' : 'var(--danger)' }}>
            {g?.dealer_regime === 'long_gamma' ? '🟢 LONG GAMMA' : g?.dealer_regime === 'short_gamma' ? '🔴 SHORT GAMMA' : '—'}
          </span>
        </div>
      </div>
    </div>
  );
}

export function FlowStrip() {
  const [alerts, setAlerts] = useState([]);
  useEffect(() => {
    let active = true;
    const load = () => fetch('/flow/live').then((r) => r.ok && r.json()).then((d) => active && Array.isArray(d) && setAlerts(d.slice(0, 5))).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => { active = false; clearInterval(id); };
  }, []);

  return (
    <div className="panel">
      <div className="panel-head"><h2>🌊 Latest options flow</h2><Link to="/flowseeker" className="btn small ghost">open Flowseeker →</Link></div>
      {alerts.length === 0 ? (
        <div className="hint" style={{ padding: '8px 0' }}>No unusual flow right now.</div>
      ) : (
        <div style={{ display: 'grid', gap: 4 }}>
          {alerts.map((a, i) => {
            const c = a.trade_type === 'darkpool' ? 'var(--purple)' : a.sentiment === 'bullish' ? 'var(--accent)' : 'var(--danger)';
            return (
              <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12.5, padding: '4px 6px', borderLeft: `3px solid ${c}` }}>
                <span style={{ color: 'var(--muted)', width: 58 }}>{shortTime(a.timestamp)}</span>
                <strong style={{ width: 52 }}>{a.ticker}</strong>
                <span style={{ color: a.option_type === 'call' ? 'var(--accent)' : 'var(--danger)', width: 64 }}>{a.option_type} {a.strike}</span>
                <span style={{ fontWeight: 700, width: 64 }}>{prem(a.premium)}</span>
                <span style={{ color: c }}>{a.trade_type}</span>
                <span style={{ marginLeft: 'auto', color: 'var(--muted)' }}>urg {Math.round(a.urgency_score * 100)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
