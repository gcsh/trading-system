import React, { useState } from 'react';

// Turn a raw strategy action into a beginner-friendly verdict.
function verdict(action) {
  const a = action || 'HOLD';
  if (a.startsWith('BUY')) return { label: 'BUY', tone: 'pos', icon: '🟢', color: 'var(--accent)' };
  if (a.startsWith('SELL')) return { label: 'AVOID', tone: 'neg', icon: '🔴', color: 'var(--danger)' };
  return { label: 'WAIT', tone: 'muted', icon: '⚪', color: 'var(--muted)' };
}

export default function AIPicks({ tickers }) {
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const rate = async () => {
    setBusy(true); setErr(null);
    try {
      const qs = tickers && tickers.length ? `?tickers=${encodeURIComponent(tickers.join(','))}` : '';
      const r = await fetch(`/diagnostics/strategy/adaptive${qs}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const sorted = (d.results || []).slice().sort((a, b) => b.confidence - a.confidence);
      setRows(sorted);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>🔮 AI ratings — buy, wait, or avoid</h2>
        <button className="btn small primary" onClick={rate} disabled={busy}>{busy ? 'Analyzing…' : rows ? '↻ Re-rate' : '✨ Rate my stocks'}</button>
      </div>
      {err && <div className="hint" style={{ color: 'var(--danger)' }}>{err}</div>}
      {!rows && !err && (
        <div className="empty" style={{ padding: '20px 12px' }}>
          <div className="title">See what the AI thinks</div>
          <div className="hint">Click "Rate my stocks" — the AI checks each of your tickers and gives a plain Buy / Wait / Avoid call with a reason.</div>
        </div>
      )}
      {rows && (
        <div style={{ display: 'grid', gap: 6 }}>
          {rows.map((r) => {
            const v = verdict(r.action);
            return (
              <div key={r.ticker} style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '8px 10px', background: 'var(--panel-2)', borderRadius: 9, border: '1px solid var(--border)' }}>
                <div style={{ width: 52, fontWeight: 700 }}>{r.ticker}</div>
                <div style={{ width: 92 }}>
                  <span className="pill" style={{ color: v.color, borderColor: v.color }}>{v.icon} {v.label}</span>
                </div>
                <div style={{ width: 90 }}>
                  <div className="gauge" style={{ margin: 0 }}>
                    <div className="gauge-track" style={{ height: 6 }}>
                      <div className="gauge-fill" style={{ width: `${Math.round((r.confidence || 0) * 100)}%`, background: v.color }} />
                    </div>
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 2 }}>{Math.round((r.confidence || 0) * 100)}% sure</div>
                </div>
                <div style={{ flex: 1, fontSize: 12, color: 'var(--muted)' }}>{r.reason || '—'}</div>
              </div>
            );
          })}
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
            "Avoid" means a sell/bearish signal — for a beginner that usually means stay out, not short it. Not financial advice.
          </div>
        </div>
      )}
    </div>
  );
}
