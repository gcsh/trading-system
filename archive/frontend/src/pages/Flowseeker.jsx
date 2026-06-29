import React, { useEffect, useMemo, useRef, useState } from 'react';
import { shortTime } from '../lib/format.js';

const URGENCY_TOAST = 0.8;

function prem(v) {
  const n = Number(v) || 0;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function rowAccent(a) {
  if (a.trade_type === 'darkpool') return 'var(--purple)';
  if (a.trade_type === 'block') return 'var(--info)';
  if (a.sentiment === 'bullish') return 'var(--accent)';
  return 'var(--danger)';
}

export default function Flowseeker() {
  const [alerts, setAlerts] = useState([]);
  const [connected, setConnected] = useState(false);
  const [toast, setToast] = useState(null);
  const [minPrem, setMinPrem] = useState(0);
  const [side, setSide] = useState('all');     // all | call | put
  const [ttype, setTtype] = useState('all');    // all | sweep | block | darkpool
  const seen = useRef(new Set());

  const ingest = (incoming) => {
    const next = [];
    for (const a of incoming) {
      const key = `${a.ticker}-${a.strike}-${a.expiry}-${a.timestamp}`;
      if (!seen.current.has(key)) {
        if (a.urgency_score >= URGENCY_TOAST) {
          setToast({ ...a, key });
          setTimeout(() => setToast((t) => (t && t.key === key ? null : t)), 6000);
        }
        seen.current.add(key);
        next.push(a);
      }
    }
    // The WS now sends only newly-seen alerts, so merge (newest first) and cap
    // rather than replace — otherwise the list would blank out between ticks.
    if (next.length) {
      setAlerts((prev) => [...next, ...prev]
        .sort((x, y) => (y.urgency_score || 0) - (x.urgency_score || 0))
        .slice(0, 200));
    }
  };

  // Initial snapshot.
  useEffect(() => {
    fetch('/flow/live').then((r) => r.ok && r.json()).then((d) => d && ingest(d)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live updates over WebSocket.
  useEffect(() => {
    let ws;
    try {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${window.location.host}/ws/flow`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => setConnected(false);
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'flow' && Array.isArray(msg.alerts)) ingest(msg.alerts);
        } catch { /* ignore */ }
      };
    } catch { setConnected(false); }
    return () => { try { ws && ws.close(); } catch { /* ignore */ } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => alerts.filter((a) => (
    a.premium >= minPrem
    && (side === 'all' || a.option_type === side)
    && (ttype === 'all' || a.trade_type === ttype)
  )), [alerts, minPrem, side, ttype]);

  return (
    <div>
      <div className="panel-head" style={{ marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>🌊 Flowseeker — Institutional Options Flow</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            Unusual sweeps, blocks &amp; dark-pool prints — where big money is positioning. Live over WebSocket.
          </div>
        </div>
        <span className="pill" style={{ color: connected ? 'var(--accent)' : 'var(--muted)', borderColor: connected ? 'var(--accent)' : 'var(--border)' }}>
          <span className="dot pulse" style={{ width: 6, height: 6, borderRadius: '50%', background: connected ? 'var(--accent)' : 'var(--muted)', display: 'inline-block' }} />
          {connected ? 'LIVE' : 'connecting…'}
        </span>
      </div>

      {/* filters */}
      <div className="row" style={{ gap: 10, marginBottom: 12, alignItems: 'center' }}>
        <label style={{ margin: 0, fontSize: 12, color: 'var(--muted)' }}>Min premium
          <select value={minPrem} onChange={(e) => setMinPrem(Number(e.target.value))} style={{ width: 110, marginLeft: 6 }}>
            <option value={0}>any</option><option value={50000}>$50K</option><option value={100000}>$100K</option><option value={250000}>$250K</option><option value={1000000}>$1M</option>
          </select>
        </label>
        <div className="row" style={{ gap: 4 }}>
          {['all', 'call', 'put'].map((s) => <button key={s} className={`btn small ${side === s ? 'primary' : ''}`} onClick={() => setSide(s)}>{s}</button>)}
        </div>
        <div className="row" style={{ gap: 4 }}>
          {['all', 'sweep', 'block', 'darkpool'].map((s) => <button key={s} className={`btn small ${ttype === s ? 'primary' : ''}`} onClick={() => setTtype(s)}>{s}</button>)}
        </div>
        <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 'auto' }}>{filtered.length} alerts</span>
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <div className="scroll" style={{ maxHeight: 560, border: 'none' }}>
          <table>
            <thead>
              <tr>
                <th>Time</th><th>Ticker</th><th>Type</th><th className="num">Strike</th><th>Expiry</th>
                <th className="num">Premium</th><th>Sentiment</th><th>Trade</th><th className="num">Urgency</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={9}><div className="empty" style={{ padding: '28px 12px' }}>No flow matching your filters. Big prints will stream in here live.</div></td></tr>
              ) : filtered.map((a, i) => {
                const c = rowAccent(a);
                return (
                  <tr key={`${a.ticker}-${a.strike}-${i}`} style={{ borderLeft: `3px solid ${c}` }}>
                    <td style={{ color: 'var(--muted)' }}>{shortTime(a.timestamp)}</td>
                    <td><strong>{a.ticker}</strong></td>
                    <td style={{ color: a.option_type === 'call' ? 'var(--accent)' : 'var(--danger)' }}>{a.option_type}</td>
                    <td className="num">{a.strike}</td>
                    <td style={{ color: 'var(--muted)' }}>{a.expiry}</td>
                    <td className="num" style={{ fontWeight: 700 }}>{prem(a.premium)}</td>
                    <td><span style={{ color: a.sentiment === 'bullish' ? 'var(--accent)' : 'var(--danger)', fontWeight: 600 }}>{a.sentiment}</span></td>
                    <td><span className="pill" style={{ color: c, borderColor: c }}>{a.trade_type}</span></td>
                    <td className="num">
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ width: 40, height: 6, background: 'var(--panel-2)', borderRadius: 999, overflow: 'hidden', display: 'inline-block' }}>
                          <span style={{ display: 'block', height: '100%', width: `${Math.round(a.urgency_score * 100)}%`, background: a.urgency_score >= URGENCY_TOAST ? 'var(--danger)' : 'var(--warn)' }} />
                        </span>
                        {Math.round(a.urgency_score * 100)}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {toast && (
        <div style={{ position: 'fixed', bottom: 20, right: 20, zIndex: 200, background: 'var(--panel)', border: `1px solid ${rowAccent(toast)}`, borderLeft: `4px solid ${rowAccent(toast)}`, borderRadius: 10, padding: '12px 16px', boxShadow: 'var(--shadow-md)', maxWidth: 320 }}>
          <div style={{ fontSize: 11, color: 'var(--danger)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em' }}>⚡ High-urgency flow</div>
          <div style={{ fontWeight: 700, marginTop: 2 }}>{toast.ticker} {toast.option_type.toUpperCase()} {toast.strike}</div>
          <div style={{ fontSize: 12.5, color: 'var(--text-soft)' }}>{prem(toast.premium)} · {toast.trade_type} · {toast.sentiment} · urgency {Math.round(toast.urgency_score * 100)}</div>
        </div>
      )}
    </div>
  );
}
