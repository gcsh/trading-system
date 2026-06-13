import React, { useEffect, useState } from 'react';

function bpsColor(v) {
  const n = Number(v) || 0;
  if (n > 5) return 'var(--danger)';
  if (n > 1) return 'var(--warn)';
  if (n < -1) return 'var(--accent)';
  return 'var(--text)';
}

export default function ExecutionQualityCard() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/execution/insights')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (active && d) setData(d); })
      .catch(() => {});
    load();
    const id = setInterval(load, 15000);
    return () => { active = false; clearInterval(id); };
  }, []);

  if (!data || data.count === 0) {
    return (
      <div className="panel">
        <div className="panel-head"><h2>⚡ Execution quality</h2><span className="panel-sub">no fills yet</span></div>
        <div className="empty" style={{ padding: '14px 12px' }}>
          <div className="hint">Slippage between signal-time price and fill price will land here once the bot executes trades.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>⚡ Execution quality</h2>
        <span className="panel-sub">{data.count} fills tracked</span>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, marginBottom: 12 }}>
        <Stat label="Avg slippage" value={`${data.avg_slippage_bps >= 0 ? '+' : ''}${data.avg_slippage_bps} bps`}
              color={bpsColor(data.avg_slippage_bps)}
              sub={data.avg_slippage_bps > 5 ? 'paying through' : data.avg_slippage_bps < -1 ? 'price improvement' : 'within tolerance'} />
        <Stat label="Adverse rate" value={`${Math.round((data.adverse_rate || 0) * 100)}%`}
              color={data.adverse_rate > 0.6 ? 'var(--warn)' : 'var(--text)'} sub="fills worse than expected" />
        <Stat label="Buys" value={`${(data.by_side?.BUY?.avg_slippage_bps ?? 0).toFixed(1)} bps`}
              sub={`${data.by_side?.BUY?.count ?? 0} fills`} color={bpsColor(data.by_side?.BUY?.avg_slippage_bps)} />
        <Stat label="Sells" value={`${(data.by_side?.SELL?.avg_slippage_bps ?? 0).toFixed(1)} bps`}
              sub={`${data.by_side?.SELL?.count ?? 0} fills`} color={bpsColor(data.by_side?.SELL?.avg_slippage_bps)} />
      </div>
      {data.by_ticker && Object.keys(data.by_ticker).length > 0 && (
        <div>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600, marginBottom: 6 }}>Per ticker</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))', gap: 8, fontSize: 12 }}>
            {Object.entries(data.by_ticker).slice(0, 10).map(([t, info]) => (
              <div key={t} style={{ display: 'flex', justifyContent: 'space-between', background: 'var(--panel-2)', borderRadius: 6, padding: '4px 8px' }}>
                <span><strong>{t}</strong> <span style={{ color: 'var(--muted)' }}>({info.count})</span></span>
                <span style={{ color: bpsColor(info.avg_slippage_bps), fontFeatureSettings: '"tnum"' }}>{info.avg_slippage_bps >= 0 ? '+' : ''}{info.avg_slippage_bps} bps</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, sub, color }) {
  return (
    <div style={{ minWidth: 110 }}>
      <div style={{ fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color || 'var(--text)', fontFeatureSettings: '"tnum"', marginTop: 2 }}>{value}</div>
      {sub != null && <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>{sub}</div>}
    </div>
  );
}
