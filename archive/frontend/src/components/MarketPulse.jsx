import React, { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  Tooltip,
  YAxis,
} from 'recharts';
import { money, num, pct, shortTime } from '../lib/format.js';

function MiniChart({ data, positive }) {
  const color = positive ? 'var(--accent)' : 'var(--danger)';
  if (!data || data.length === 0) {
    return <div style={{ height: 60, color: 'var(--muted)', fontSize: 11, display: 'grid', placeItems: 'center' }}>no intraday data</div>;
  }
  return (
    <div style={{ height: 60 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 2, left: 2, bottom: 2 }}>
          <defs>
            <linearGradient id={`mini-${positive}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis hide domain={['auto', 'auto']} />
          <Tooltip
            contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 11 }}
            labelFormatter={shortTime}
            formatter={(v) => [money(v), 'Price']}
          />
          <Area type="monotone" dataKey="price" stroke={color} strokeWidth={1.5} fill={`url(#mini-${positive})`} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export default function MarketPulse({ compact = false, onSelectTicker }) {
  const [data, setData] = useState(null);

  const load = async () => {
    try {
      const r = await fetch('/market/overview');
      if (!r.ok) return;
      setData(await r.json());
    } catch (e) {
      /* ignore */
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  if (!data) {
    return (
      <div className={`panel ${compact ? 'col-12' : 'col-12'}`}>
        <div className="panel-head">
          <h2>Market pulse</h2>
          <span className="panel-sub">loading…</span>
        </div>
      </div>
    );
  }

  const { indices, sectors, vix, breadth_pct, advancers, decliners, regime, market_status } = data;
  const vixColor = vix.value >= 30 ? 'var(--danger)' : vix.value >= 20 ? 'var(--warn)' : 'var(--accent)';

  return (
    <>
      <div className="panel col-12">
        <div className="panel-head">
          <h2>Market pulse</h2>
          <div className="row">
            <span className={`pill ${market_status === 'open' ? 'on' : 'off'}`}>
              <span className="dot" />
              market {market_status}
            </span>
            {regime && <span className="pill purple">regime · {regime}</span>}
            <span className="pill" style={{ background: 'var(--panel-2)' }}>
              VIX <strong style={{ color: vixColor, marginLeft: 4 }}>{num(vix.value).toFixed(2)}</strong>
              <span style={{ color: 'var(--muted)', marginLeft: 4 }}>· {vix.label}</span>
            </span>
            <span className="pill">
              breadth <strong style={{ marginLeft: 4 }}>{breadth_pct}%</strong>
              <span style={{ color: 'var(--muted)', marginLeft: 4 }}>
                {advancers}↑ / {decliners}↓
              </span>
            </span>
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
          {indices.map((idx) => {
            const change = num(idx.change_pct);
            const positive = change >= 0;
            return (
              <div
                key={idx.ticker}
                style={{
                  background: 'var(--panel-2)',
                  border: '1px solid var(--border)',
                  borderRadius: 9,
                  padding: 12,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <strong style={{ fontSize: 14 }}>{idx.ticker}</strong>
                  <span style={{ fontSize: 12, color: 'var(--muted)' }}>{idx.source}</span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 4 }}>
                  <span style={{ fontSize: 18, fontWeight: 600, fontFeatureSettings: '"tnum"' }}>
                    {money(idx.price)}
                  </span>
                  <span className={positive ? 'pos' : 'neg'} style={{ fontFeatureSettings: '"tnum"' }}>
                    {positive ? '+' : ''}
                    {change.toFixed(2)}%
                  </span>
                </div>
                <MiniChart data={idx.curve} positive={positive} />
              </div>
            );
          })}
        </div>
      </div>

      {!compact && (
        <div className="panel col-12">
          <div className="panel-head">
            <h2>Sectors</h2>
            <span className="panel-sub">SPDR ETFs</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 8 }}>
            {sectors.map((s) => {
              const change = num(s.change_pct);
              const positive = change >= 0;
              const intensity = Math.min(1, Math.abs(change) / 3);
              const bg = positive
                ? `rgba(14, 138, 95, ${0.07 + 0.18 * intensity})`
                : `rgba(212, 72, 92, ${0.07 + 0.18 * intensity})`;
              return (
                <div
                  key={s.ticker}
                  onClick={() => onSelectTicker && onSelectTicker(s.ticker)}
                  title={onSelectTicker ? `View ${s.ticker} chart` : ''}
                  style={{
                    padding: 12,
                    border: '1px solid var(--border)',
                    background: bg,
                    borderRadius: 8,
                    cursor: onSelectTicker ? 'pointer' : 'default',
                  }}
                >
                  <div style={{ fontSize: 12, color: 'var(--muted)' }}>{s.name}</div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 2 }}>
                    <strong>{s.ticker}</strong>
                    <span className={positive ? 'pos' : 'neg'}>
                      {positive ? '+' : ''}
                      {change.toFixed(2)}%
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}
