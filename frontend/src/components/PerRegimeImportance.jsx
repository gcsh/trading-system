/**
 * Stage-16 — Per-regime feature importance widget.
 *
 * Surfaces /explain/importance/by-regime: each regime trend gets its own
 * importance report so the operator can see "in bull tape, rsi_14
 * matters; in choppy, iv_rank does". Lives on AI Signals next to the
 * global FeatureImportance widget.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const TREND_COLOR = {
  bullish: 'var(--accent)',
  bearish: 'var(--danger)',
  choppy: 'var(--text)',
  unknown: 'var(--muted)',
};

export default function PerRegimeImportance({ topK = 8 }) {
  const [body, setBody] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchJson(`/explain/importance/by-regime?top_k=${topK}`)
      .then(setBody)
      .catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topK]);

  if (error) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>🧬 Importance by regime</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!body) {
    return <div className="panel col-12"><h3 style={{ marginTop: 0 }}>🧬 Importance by regime</h3>Loading…</div>;
  }

  // Pick regimes in canonical order so the columns are stable.
  const ORDER = ['bullish', 'bearish', 'choppy', 'unknown'];
  const regimes = ORDER.filter((r) => r in body).concat(
    Object.keys(body).filter((r) => !ORDER.includes(r))
  );

  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>🧬 Importance by regime</h3>
        <span className="pill info">{regimes.length} regimes</span>
      </div>
      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
        {regimes.map((regime) => {
          const rpt = body[regime];
          if (!rpt) return null;
          const isPermutation = rpt.method === 'permutation';
          const max = Math.max(
            ...(rpt.importances || []).map((i) => Math.abs(i.importance)),
            0.001,
          );
          return (
            <div key={regime} style={{
              background: 'var(--panel-2)',
              borderRadius: 6,
              padding: 10,
              border: `1px solid var(--border)`,
              borderTop: `3px solid ${TREND_COLOR[regime] || 'var(--border-strong)'}`,
            }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <span style={{ fontWeight: 600, textTransform: 'capitalize' }}>{regime}</span>
                <span className={`pill ${isPermutation ? 'on' : 'off'}`}>
                  {rpt.method} · {rpt.sample_size || 0}
                </span>
              </div>
              {(rpt.importances || []).slice(0, topK).map((fi) => (
                <div key={fi.feature} className="row" style={{ alignItems: 'center', gap: 6, marginBottom: 3 }}>
                  <span style={{ fontSize: 11, minWidth: 90, color: 'var(--muted)' }}>{fi.feature}</span>
                  <div style={{ flex: 1, background: 'var(--bg-elev)', borderRadius: 3, height: 6, overflow: 'hidden' }}>
                    <div style={{
                      width: `${Math.max(2, (Math.abs(fi.importance) / max) * 100)}%`,
                      height: '100%',
                      background: TREND_COLOR[regime] || 'var(--accent)',
                    }} />
                  </div>
                  <span style={{ fontSize: 10, color: 'var(--muted)', minWidth: 40, textAlign: 'right' }}>
                    {(fi.importance * 100).toFixed(2)}%
                  </span>
                </div>
              ))}
              {(rpt.warnings || []).map((w, i) => (
                <div key={i} style={{ color: 'var(--muted)', fontSize: 11, marginTop: 4, fontStyle: 'italic' }}>
                  {w}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
