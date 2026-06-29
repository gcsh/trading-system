import React, { useEffect, useState } from 'react';
import { money, num } from '../lib/format.js';

function ActionBadge({ action }) {
  if (!action) return <span className="pill off">—</span>;
  if (action === 'HOLD') return <span className="pill off">HOLD</span>;
  if (action === 'ERROR') return <span className="pill danger">ERROR</span>;
  const isBuy = action.startsWith('BUY');
  return (
    <span className={`pill ${isBuy ? 'on' : 'danger'}`}>
      {action.replace(/_/g, ' ')}
    </span>
  );
}

function SnapshotChip({ label, value, format }) {
  return (
    <div style={{ background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 6, padding: '6px 10px' }}>
      <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</div>
      <div style={{ fontWeight: 600, fontFeatureSettings: '"tnum"' }}>{format ? format(value) : value ?? '—'}</div>
    </div>
  );
}

function TickerCard({ diag, expanded, onToggle }) {
  const snap = diag.snapshot || {};
  const best = diag.best;
  const risk = diag.risk_decision;
  const actionable = diag.strategies.filter((s) => s.action !== 'HOLD' && s.action !== 'ERROR');
  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      <div className="panel-head" onClick={onToggle} style={{ cursor: 'pointer' }}>
        <div>
          <h2 style={{ margin: 0 }}>
            {diag.ticker}{' '}
            <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 12, marginLeft: 6 }}>
              {money(snap.price)}  ·  RSI {num(snap.rsi).toFixed(1)}  ·  MACD {num(snap.macd).toFixed(3)} {num(snap.macd) > num(snap.macd_signal) ? '↑' : '↓'} signal
            </span>
          </h2>
        </div>
        <div className="row">
          {best ? (
            <>
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>best:</span>
              <strong style={{ fontSize: 12 }}>{best.name}</strong>
              <ActionBadge action={best.action} />
              <span className="pill info">{(best.confidence * 100).toFixed(0)}% conf</span>
              {risk && (
                <span className={`pill ${risk.approved ? 'on' : 'warn'}`}>
                  {risk.approved ? `risk ok · ${num(risk.quantity).toFixed(2)}x` : `rejected: ${risk.reason}`}
                </span>
              )}
            </>
          ) : (
            <span className="pill off">all HOLD</span>
          )}
          <span style={{ color: 'var(--muted)' }}>{expanded ? '▲' : '▼'}</span>
        </div>
      </div>

      {expanded && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 6, marginBottom: 12 }}>
            <SnapshotChip label="Price" value={snap.price} format={money} />
            <SnapshotChip label="RSI" value={num(snap.rsi).toFixed(1)} />
            <SnapshotChip label="MACD" value={num(snap.macd).toFixed(3)} />
            <SnapshotChip label="MACD sig" value={num(snap.macd_signal).toFixed(3)} />
            <SnapshotChip label="MA50" value={snap.ma50 ? money(snap.ma50) : '—'} />
            <SnapshotChip label="MA200" value={snap.ma200 ? money(snap.ma200) : '—'} />
            <SnapshotChip label="ADX" value={num(snap.adx).toFixed(1)} />
            <SnapshotChip label="VIX" value={num(snap.vix).toFixed(1)} />
            <SnapshotChip label="News" value={num(snap.news_score).toFixed(2)} />
            <SnapshotChip label="IV rank" value={num(snap.iv_rank).toFixed(0)} />
          </div>

          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Action</th>
                <th className="num">Conf</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {diag.strategies
                .slice()
                .sort((a, b) => b.confidence - a.confidence)
                .map((s) => (
                  <tr key={s.name}>
                    <td><strong style={{ textTransform: 'capitalize' }}>{s.name.replace(/_/g, ' ')}</strong></td>
                    <td><ActionBadge action={s.action} /></td>
                    <td className="num">{(s.confidence * 100).toFixed(0)}%</td>
                    <td style={{ color: 'var(--muted)', fontSize: 12 }}>{s.reason}</td>
                  </tr>
                ))}
            </tbody>
          </table>
          {diag.source_errors && diag.source_errors.length > 0 && (
            <div style={{ marginTop: 10, color: 'var(--warn)', fontSize: 12 }}>
              data warnings: {diag.source_errors.join('; ')}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function CycleDiagnostics() {
  const [data, setData] = useState(null);
  const [expanded, setExpanded] = useState(new Set());
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch('/diagnostics/cycle');
      if (!r.ok) return;
      setData(await r.json());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  const toggle = (ticker) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(ticker)) next.delete(ticker);
      else next.add(ticker);
      return next;
    });
  };

  if (!data) {
    return (
      <div className="panel col-12">
        <div className="panel-head">
          <h2>Cycle diagnostics</h2>
          <span className="panel-sub">{loading ? 'loading…' : 'no data'}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="col-12">
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <h2>Why no trades?</h2>
          <div className="row">
            <span className={`pill ${data.auto_execute ? 'on' : 'warn'}`}>
              auto-exec {data.auto_execute ? 'ON' : 'OFF'}
            </span>
            <span className="pill info">
              min confidence {(data.min_confidence * 100).toFixed(0)}%
            </span>
            <span className={`pill ${data.actionable_count > 0 ? 'on' : 'off'}`}>
              {data.actionable_count} of {data.tickers_scanned} actionable
            </span>
            <button className="btn small" onClick={load}>Refresh</button>
          </div>
        </div>
        <div style={{ fontSize: 13, color: 'var(--text-soft)' }}>
          {data.actionable_count === 0 && (
            <>
              Every strategy returned <strong>HOLD</strong> or scored below your <strong>{(data.min_confidence * 100).toFixed(0)}%</strong> threshold.
              {' '}Expand a ticker below to see what each strategy thought and why.
              {!data.auto_execute && (
                <> Even if signals fire, <strong>auto-execute is OFF</strong> — only alerts will trigger.</>
              )}
            </>
          )}
          {data.actionable_count > 0 && !data.auto_execute && (
            <>{data.actionable_count} actionable signal(s) found but <strong>auto-execute is OFF</strong>. Turn it on (AI Signals page) to send these as real orders.</>
          )}
          {data.actionable_count > 0 && data.auto_execute && (
            <>{data.actionable_count} actionable signal(s). The next live cycle will execute them (subject to risk checks).</>
          )}
        </div>
      </div>

      {data.diagnostics.map((d) => (
        <TickerCard
          key={d.ticker}
          diag={d}
          expanded={expanded.has(d.ticker)}
          onToggle={() => toggle(d.ticker)}
        />
      ))}
    </div>
  );
}
