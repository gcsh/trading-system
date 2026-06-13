import React, { useEffect, useState } from 'react';
import { pct } from '../lib/format.js';

function Stage({ n, active, done, title, children }) {
  return (
    <div style={{ display: 'flex', gap: 12, opacity: active ? 1 : 0.25, transition: 'opacity .4s', alignItems: 'flex-start' }}>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <div style={{
          width: 26, height: 26, borderRadius: '50%', display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 700,
          background: done ? 'var(--accent)' : active ? 'var(--info)' : 'var(--panel-2)',
          color: done || active ? '#fff' : 'var(--muted)', border: '1px solid var(--border)', flexShrink: 0,
        }}>{done ? '✓' : n}</div>
        {n < 4 && <div style={{ width: 2, flex: 1, minHeight: 18, background: 'var(--border)', marginTop: 2 }} />}
      </div>
      <div style={{ flex: 1, paddingBottom: 14 }}>
        <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>{title}</div>
        {active && children}
      </div>
    </div>
  );
}

export default function StrategyScout({ ticker }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState(0);
  const [applied, setApplied] = useState(null);
  const [err, setErr] = useState(null);

  const scout = async () => {
    setBusy(true); setErr(null); setData(null); setStage(0); setApplied(null);
    try {
      const r = await fetch(`/copilot/recommend?ticker=${encodeURIComponent(ticker || 'SPY')}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setData(d);
      [1, 2, 3, 4].forEach((s, i) => setTimeout(() => setStage(s), 450 * (i + 1)));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const approve = async () => {
    if (!data?.best) return;
    await fetch('/copilot/apply-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ strategy: data.best.strategy }) });
    setApplied(data.best.strategy);
  };

  // Optional deep-link auto-run (?scout=1) — handy for sharing/demoing.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('scout') === '1') scout();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const best = data?.best;
  const niceBest = best ? best.strategy.replace(/_/g, ' ') : '';

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2 style={{ margin: 0 }}>🧭 Strategy Scout</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>Find a proven strategy for today's market — tested on real stocks before you trust it.</div>
        </div>
        <button className="btn small primary" onClick={scout} disabled={busy}>{busy ? 'Scanning the market…' : data ? '↻ Scan again' : `🔍 Find a strategy for now`}</button>
      </div>

      {err && <div className="hint" style={{ color: 'var(--danger)' }}>{err}</div>}
      {!data && !err && (
        <div className="empty" style={{ padding: '22px 12px' }}>
          <div className="title">Let the bot pick the right play</div>
          <div className="hint">It reads the market, matches a textbook-proven strategy, backtests it on sample stocks, and shows you the results before anything goes live.</div>
        </div>
      )}

      {data && (
        <div style={{ marginTop: 4 }}>
          <Stage n={1} active={stage >= 1} done={stage > 1} title="📡 Reading the market">
            <div style={{ fontSize: 13 }}><strong>{data.regime.label}</strong></div>
            <div style={{ fontSize: 12.5, color: 'var(--text-soft)', lineHeight: 1.5 }}>{data.regime.description}</div>
          </Stage>

          <Stage n={2} active={stage >= 2} done={stage > 2} title="🎯 Matching proven strategies">
            <div style={{ fontSize: 12.5, color: 'var(--text-soft)' }}>
              For this market, the textbook plays are:{' '}
              {(data.candidates || []).map((c, i) => (
                <span key={c.strategy} style={{ fontWeight: 600 }}>{c.strategy.replace(/_/g, ' ')}{i < data.candidates.length - 1 ? ', ' : ''}</span>
              ))}.
            </div>
          </Stage>

          <Stage n={3} active={stage >= 3} done={stage > 3} title={`🧪 Backtesting on ${data.samples?.length || 0} sample stocks`}>
            <table style={{ fontSize: 12 }}>
              <thead><tr><th>Strategy</th><th className="num">Avg return</th><th className="num">vs hold</th><th className="num">Beat hold</th><th className="num">Trades</th></tr></thead>
              <tbody>
                {(data.candidates || []).map((c) => (
                  <tr key={c.strategy} style={{ background: best && c.strategy === best.strategy ? 'var(--accent-soft)' : undefined }}>
                    <td style={{ textTransform: 'capitalize', fontWeight: best && c.strategy === best.strategy ? 700 : 500 }}>{c.strategy.replace(/_/g, ' ')}</td>
                    <td className={`num ${c.avg_return_pct >= 0 ? 'pos' : 'neg'}`}>{pct(c.avg_return_pct, 1, { showSign: true })}</td>
                    <td className={`num ${c.avg_alpha_pct >= 0 ? 'pos' : 'neg'}`}>{pct(c.avg_alpha_pct, 1, { showSign: true })}</td>
                    <td className="num">{c.beat_bh_count}/{c.samples_tested}</td>
                    <td className="num">{c.total_trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Stage>

          <Stage n={4} active={stage >= 4} done={!!applied} title="✅ Verdict">
            <div style={{ fontWeight: 700, fontSize: 14, color: data.verdict.good ? 'var(--accent)' : 'var(--warn)' }}>{data.verdict.headline}</div>
            <div style={{ fontSize: 12.5, color: 'var(--text-soft)', lineHeight: 1.5, marginTop: 3 }}>{data.verdict.detail}</div>
            {best && best.total_trades > 0 && (
              <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 3 }}>Confidence it beats just holding: {data.verdict.confidence}%</div>
                <div className="gauge" style={{ margin: 0 }}><div className="gauge-track"><div className="gauge-fill" style={{ width: `${data.verdict.confidence}%`, background: data.verdict.good ? 'var(--accent)' : 'var(--warn)' }} /></div></div>
              </div>
            )}
            {best && (
              <div className="row" style={{ marginTop: 12 }}>
                {applied ? (
                  <span className="pill on">✓ {applied.replace(/_/g, ' ')} is now the active strategy</span>
                ) : (
                  <>
                    <button className="btn small primary" onClick={approve} disabled={!data.verdict.good && best.total_trades === 0}>
                      ✓ Approve &amp; use {niceBest}
                    </button>
                    <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>It'll trade on paper money first — your $5,000 trial, no real funds.</span>
                  </>
                )}
              </div>
            )}
          </Stage>
        </div>
      )}
    </div>
  );
}
