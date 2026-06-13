import React, { useState } from 'react';

const SAMPLE = `//@version=5
strategy("My MACD+RSI", overlay=true)
[macdLine, signalLine, _] = ta.macd(close, 12, 26, 9)
longCond = ta.crossover(macdLine, signalLine) and ta.rsi(close, 14) < 40
shortCond = ta.crossunder(macdLine, signalLine) or ta.rsi(close, 14) > 70
if longCond
    strategy.entry("L", strategy.long)
if shortCond
    strategy.close("L")`;

export default function PineImport({ onApplied }) {
  const [src, setSrc] = useState('');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async (apply) => {
    setBusy(true);
    try {
      const r = await fetch('/strategies/import-pine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: src, apply }),
      });
      const body = await r.json();
      setResult(body);
      if (body.applied && onApplied) onApplied();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel col-12">
      <div className="panel-head">
        <h2>Import a Pine Script strategy</h2>
        <button className="btn small ghost" onClick={() => setSrc(SAMPLE)}>Load example</button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
        Paste TradingView Pine below. This is a <strong>best-effort translator</strong> — it can't run Pine (only
        TradingView can), but it extracts the common patterns (MACD crosses, RSI thresholds, price vs MA) and turns
        them into custom rules you can run here. Anything it doesn't recognize is listed so you know what was dropped.
      </div>
      <textarea
        value={src}
        onChange={(e) => setSrc(e.target.value)}
        placeholder="Paste Pine Script here…"
        style={{ minHeight: 160 }}
      />
      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn small" disabled={busy || !src.trim()} onClick={() => run(false)}>Translate</button>
        <button className="btn small primary" disabled={busy || !src.trim()} onClick={() => run(true)}>
          Translate &amp; apply as custom strategy
        </button>
      </div>

      {result && (
        <div style={{ marginTop: 12 }}>
          {result.rules.length > 0 ? (
            <>
              <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>Translated rules</div>
              <pre style={{ background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, fontSize: 12, color: 'var(--text)' }}>
                {result.rules_text}
              </pre>
              <div className="row" style={{ gap: 6, marginBottom: 6 }}>
                {result.recognized.map((r, i) => <span key={i} className="pill on">{r}</span>)}
              </div>
              {result.applied && <span className="pill info">applied · active strategy is now "custom"</span>}
            </>
          ) : (
            <div style={{ color: 'var(--warn)', fontSize: 13 }}>
              Couldn't recognize any supported patterns. Supported: MACD crossover/crossunder, RSI &lt;/&gt; thresholds, price vs SMA.
            </div>
          )}
          {result.skipped?.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>Not translated ({result.skipped.length} lines):</div>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18, fontSize: 11, color: 'var(--muted)' }}>
                {result.skipped.slice(0, 6).map((s, i) => <li key={i}><code>{s}</code></li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
