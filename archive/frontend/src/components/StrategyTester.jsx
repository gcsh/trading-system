import React, { useState } from 'react';
import { num } from '../lib/format.js';

function ActionPill({ action }) {
  if (!action) return <span className="pill off">—</span>;
  if (action === 'HOLD') return <span className="pill off">HOLD</span>;
  if (action === 'ERROR') return <span className="pill danger">ERROR</span>;
  const isBuy = action.startsWith('BUY');
  return <span className={`pill ${isBuy ? 'on' : 'danger'}`}>{action.replace(/_/g, ' ')}</span>;
}

export default function StrategyTester({ strategy, onApply, tickers }) {
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const test = async () => {
    if (!strategy) return;
    setBusy(true);
    try {
      const qs = tickers && tickers.length ? `?tickers=${encodeURIComponent(tickers.join(','))}` : '';
      const r = await fetch(`/diagnostics/strategy/${encodeURIComponent(strategy)}${qs}`);
      if (!r.ok) {
        const msg = await r.json().catch(() => ({}));
        throw new Error(msg.detail || r.status);
      }
      setResult(await r.json());
    } catch (e) {
      setResult({ error: e.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel col-12">
      <div className="panel-head">
        <h2>
          Test strategy: <span style={{ textTransform: 'capitalize' }}>{(strategy || '').replace(/_/g, ' ')}</span>
        </h2>
        <div className="row">
          <button className="btn small primary" disabled={busy || !strategy} onClick={test}>
            {busy ? 'Running…' : 'Run test'}
          </button>
          {onApply && strategy && (
            <button className="btn small" onClick={() => onApply(strategy)}>
              Apply as active strategy
            </button>
          )}
        </div>
      </div>

      <div style={{ fontSize: 12, color: 'var(--text-soft)', background: 'var(--info-soft)', border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px', marginBottom: 10, lineHeight: 1.5 }}>
        ℹ️ This is a <strong>right-now snapshot of this one strategy</strong> — "HOLD" just means it sees no fresh entry this second. It does <strong>not</strong> mean the bot can't already own the stock: the bot may have bought it earlier via a different strategy, and holds it until its stop/target. Intraday/options setups (like opening-range breakout) often read HOLD on a daily snapshot because we don't have live pre-market/options data — use the annotated chart above to see how the strategy actually performed.
      </div>

      {!result && (
        <div className="empty">
          <div className="title">Pick a strategy below and click "Run test"</div>
          <div className="hint">
            Runs the strategy on every configured ticker right now. No orders are placed.
          </div>
        </div>
      )}

      {result?.error && <div style={{ color: 'var(--danger)' }}>{result.error}</div>}

      {result?.results && (
        <>
          <div className="row" style={{ marginBottom: 10 }}>
            <span className={`pill ${result.actionable_count > 0 ? 'on' : 'off'}`}>
              {result.actionable_count} of {result.tickers_scanned} actionable
            </span>
            <span className="pill info">min conf {(num(result.min_confidence) * 100).toFixed(0)}%</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Action</th>
                <th className="num">Confidence</th>
                <th className="num">Price</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {result.results
                .slice()
                .sort((a, b) => b.confidence - a.confidence)
                .map((r) => (
                  <tr key={r.ticker}>
                    <td><strong>{r.ticker}</strong></td>
                    <td><ActionPill action={r.action} /></td>
                    <td className="num">{(r.confidence * 100).toFixed(0)}%</td>
                    <td className="num">
                      {r.snapshot_price != null ? `$${num(r.snapshot_price).toFixed(2)}` : '—'}
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: 12, maxWidth: 360 }}>{r.reason}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
