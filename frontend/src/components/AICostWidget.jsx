/**
 * Stage-16 — AI Cost vs Alpha widget.
 *
 * Surfaces /ai-cost/summary and /ai-cost/alpha-ratio: total spend, per-surface
 * breakdown, alpha-per-dollar ratio. Lives on the AI Signals page so the
 * operator can see whether the Claude surfaces are paying for themselves.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const SURFACE_LABEL = {
  memo: 'Trade Memo',
  narrative: 'Narrative',
  meta_ai: 'Meta-AI',
  brain: 'AI Brain',
  chat: 'Chat',
  agents: 'Agents enrich',
  other: 'Other',
};

export default function AICostWidget() {
  const [summary, setSummary] = useState(null);
  const [alpha, setAlpha] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([
      fetchJson('/ai-cost/summary'),
      fetchJson('/ai-cost/alpha-ratio'),
    ]).then(([s, a]) => {
      setSummary(s);
      setAlpha(a);
    }).catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>💵 AI Cost vs Alpha</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!summary || !alpha) {
    return <div className="panel col-12"><h3 style={{ marginTop: 0 }}>💵 AI Cost vs Alpha</h3>Loading…</div>;
  }

  const totals = summary.totals || {};
  const bySurface = summary.by_surface || {};
  const surfaces = Object.entries(bySurface).sort((a, b) => b[1].cost_usd - a[1].cost_usd);

  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>💵 AI Cost vs Alpha</h3>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: 22, fontWeight: 600 }}>${(totals.cost_usd || 0).toFixed(4)}</span>
          <span className="pill info">{totals.calls || 0} calls</span>
          {alpha.alpha_per_dollar != null && (
            <span className={`pill ${alpha.alpha_per_dollar > 1 ? 'on' : 'danger'}`}>
              alpha-per-$: {alpha.alpha_per_dollar.toFixed(2)}×
            </span>
          )}
        </div>
      </div>

      {totals.calls === 0 ? (
        <div style={{ color: 'var(--muted)' }}>
          No AI calls recorded yet. Once narrative / memo / meta_ai run, spend will accumulate here.
        </div>
      ) : (
        <>
          <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 6 }}>By surface</div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                <th style={{ padding: 6 }}>Surface</th>
                <th style={{ padding: 6, textAlign: 'right' }}>Calls</th>
                <th style={{ padding: 6, textAlign: 'right' }}>Tokens in</th>
                <th style={{ padding: 6, textAlign: 'right' }}>Tokens out</th>
                <th style={{ padding: 6, textAlign: 'right' }}>Cost</th>
              </tr>
            </thead>
            <tbody>
              {surfaces.map(([name, s]) => (
                <tr key={name} style={{ borderTop: '1px solid var(--border)' }}>
                  <td style={{ padding: 6, fontWeight: 600 }}>{SURFACE_LABEL[name] || name}</td>
                  <td style={{ padding: 6, textAlign: 'right' }}>{s.calls}</td>
                  <td style={{ padding: 6, textAlign: 'right', color: 'var(--muted)' }}>{s.tokens_in.toLocaleString()}</td>
                  <td style={{ padding: 6, textAlign: 'right', color: 'var(--muted)' }}>{s.tokens_out.toLocaleString()}</td>
                  <td style={{ padding: 6, textAlign: 'right', fontWeight: 600 }}>${s.cost_usd.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {alpha.attributed_pnl_usd !== 0 && (
            <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 10 }}>
              {alpha.attributed_pnl_usd >= 0 ? 'Earned' : 'Lost'} ${Math.abs(alpha.attributed_pnl_usd).toFixed(2)} on
              trades attributed to ${alpha.attributed_cost_usd.toFixed(4)} of AI spend.
            </div>
          )}
        </>
      )}
    </div>
  );
}
