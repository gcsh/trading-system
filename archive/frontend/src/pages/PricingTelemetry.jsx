/**
 * P4.1 — PricingTelemetry: what fraction of fills used real chain data?
 *
 * Reads /pricing/telemetry. Answers the audit question: "is my paper
 * P&L based on real prices or stubs?"
 */
import React, { useEffect, useMemo, useState } from 'react';

const SOURCE_COLOR = {
  thetadata: 'var(--accent)',
  alpaca: '#5dc6ff',
  yfinance_intraday: '#ffd84d',
  yfinance_previous: '#ff944d',
  bs_fallback: '#a98bff',
  paper_stub: 'var(--danger)',
  unknown: 'var(--muted)',
  stale: '#ff5d5d',
  thetadata_eod: '#9be65a',
};

const SOURCE_LABEL = {
  thetadata: 'ThetaData (real chain)',
  alpaca: 'Alpaca quote',
  yfinance_intraday: 'Yahoo intraday',
  yfinance_previous: 'Yahoo previous close',
  bs_fallback: 'Black-Scholes fallback',
  paper_stub: 'Paper stub (no real data)',
  unknown: 'Unknown',
  stale: 'Stale quote',
  thetadata_eod: 'ThetaData EOD',
};


function StackedBar({ counts, total }) {
  if (!total) return <div style={{ fontSize: 10, color: 'var(--muted)' }}>—</div>;
  return (
    <div style={{
      display: 'flex', height: 14, background: 'var(--panel-2)',
      borderRadius: 2, overflow: 'hidden',
    }}>
      {Object.entries(counts).map(([src, n]) => {
        const pct = (n / total) * 100;
        return (
          <div key={src}
               title={`${SOURCE_LABEL[src] || src}: ${n} (${pct.toFixed(0)}%)`}
               style={{
                 width: `${pct}%`,
                 background: SOURCE_COLOR[src] || 'var(--muted)',
                 opacity: 0.85,
               }} />
        );
      })}
    </div>
  );
}


export default function PricingTelemetry() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [hours, setHours] = useState(168);

  useEffect(() => {
    let cancelled = false;
    fetch(`/pricing/telemetry?hours=${hours}`)
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(String(e)); });
    return () => { cancelled = true; };
  }, [hours]);

  const sortedBySource = useMemo(() => {
    if (!data?.by_source) return [];
    return Object.entries(data.by_source).sort(([, a], [, b]) => b - a);
  }, [data]);

  if (err) return <div className="empty">telemetry error: {err}</div>;
  if (!data) return <div className="empty">Loading pricing telemetry…</div>;

  const total = data.total_trades || 0;

  return (
    <div>
      <div className="row" style={{ gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <div className="panel" style={{ padding: '10px 14px', flex: 1, minWidth: 200 }}>
          <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase',
                            letterSpacing: '0.06em', fontWeight: 600 }}>
            Total trades in window
          </div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2,
                            fontFeatureSettings: '"tnum"' }}>
            {total}
          </div>
          <div style={{ fontSize: 10, color: 'var(--muted-2)', marginTop: 4 }}>
            last {data.window_hours}h · excludes synthetic corpus
          </div>
        </div>
        <div style={{ alignSelf: 'center', marginLeft: 'auto' }}>
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}
            style={{ padding: '6px 10px', background: 'var(--panel)',
                       color: 'var(--text)', border: '1px solid var(--border)',
                       borderRadius: 6, fontSize: 12 }}>
            <option value={24}>last 24h</option>
            <option value={72}>last 72h</option>
            <option value={168}>last 7d</option>
            <option value={720}>last 30d</option>
          </select>
        </div>
      </div>

      <div className="panel" style={{ padding: 14, marginBottom: 14 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
                          letterSpacing: '0.05em', fontWeight: 600, marginBottom: 8 }}>
          Pricing source mix
        </div>
        {total === 0 ? (
          <div className="empty" style={{ padding: 12, fontSize: 12 }}>
            No live trades in this window yet.
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 6 }}>
            {sortedBySource.map(([src, n]) => {
              const pct = (n / total) * 100;
              const color = SOURCE_COLOR[src] || 'var(--muted)';
              return (
                <div key={src} style={{
                  display: 'grid', gridTemplateColumns: '200px 1fr 80px',
                  alignItems: 'center', gap: 8,
                }}>
                  <div style={{ fontSize: 12, fontWeight: 600, color }}>
                    {SOURCE_LABEL[src] || src}
                  </div>
                  <div style={{ height: 12, background: 'var(--panel-2)',
                                  borderRadius: 2, overflow: 'hidden' }}>
                    <div style={{ width: `${pct}%`, height: '100%',
                                       background: color, opacity: 0.75 }} />
                  </div>
                  <div style={{ fontSize: 13, fontWeight: 700, textAlign: 'right',
                                  fontFeatureSettings: '"tnum"' }}>
                    {n} <span style={{ fontSize: 10, color: 'var(--muted)' }}>
                      ({pct.toFixed(0)}%)
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="panel" style={{ padding: 14, marginBottom: 14 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
                          letterSpacing: '0.05em', fontWeight: 600, marginBottom: 8 }}>
          By strategy
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ color: 'var(--muted)' }}>
              <th style={{ textAlign: 'left', padding: '4px 8px' }}>Strategy</th>
              <th style={{ textAlign: 'left', padding: '4px 8px' }}>Source mix</th>
              <th style={{ textAlign: 'right', padding: '4px 8px' }}>Trades</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.by_strategy_source || {}).map(([strategy, counts]) => {
              const subTotal = Object.values(counts).reduce((a, b) => a + b, 0);
              return (
                <tr key={strategy} style={{ borderTop: '1px solid var(--border)' }}>
                  <td style={{ padding: '4px 8px', fontWeight: 600 }}>{strategy}</td>
                  <td style={{ padding: '4px 8px' }}>
                    <StackedBar counts={counts} total={subTotal} />
                  </td>
                  <td style={{ padding: '4px 8px', textAlign: 'right',
                                  fontFeatureSettings: '"tnum"' }}>{subTotal}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel" style={{ padding: 14 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
                          letterSpacing: '0.05em', fontWeight: 600, marginBottom: 8 }}>
          Accounting version mix
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-soft)' }}>
          {Object.entries(data.by_accounting_version || {})
              .sort(([a], [b]) => Number(a) - Number(b))
              .map(([v, n]) => (
                <span key={v} style={{ marginRight: 18 }}>
                  v{v}: <strong>{n}</strong>
                </span>
              ))}
        </div>
        <div style={{ marginTop: 8, fontSize: 10, color: 'var(--muted-2)' }}>
          v1 = stubbed option math, v2 = real chain + BS pricing (Phase 2).
          After paper-trial reset, all rows will be v2.
        </div>
      </div>
    </div>
  );
}
