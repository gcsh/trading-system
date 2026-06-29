/**
 * Stage-16 — Agent Scorecards panel. Surfaces per-agent hit-rate over
 * the most-recent closed trades that carry a persisted consensus block.
 * Lives on Mission Control alongside the per-trade Consensus panel so
 * the operator can see, in the same view, "who voted what" and
 * "who's been right historically".
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

function pillFor(hitRate) {
  if (hitRate == null) return 'pill off';
  if (hitRate >= 0.6) return 'pill on';
  if (hitRate <= 0.4) return 'pill danger';
  return 'pill info';
}

export default function AgentScorecards() {
  const [report, setReport] = useState(null);
  const [weights, setWeights] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.allSettled([
      fetchJson('/agents/scorecard'),
      fetchJson('/agents/weights'),
    ]).then(([s, w]) => {
      if (s.status === 'fulfilled') setReport(s.value);
      if (w.status === 'fulfilled') setWeights(w.value.weights || {});
      if (s.status === 'rejected') setError(s.reason.message);
    });
  }, []);

  if (error) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>🏆 Agent Scorecards</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!report) {
    return <div className="panel"><h3 style={{ marginTop: 0 }}>🏆 Agent Scorecards</h3>Loading…</div>;
  }

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>🏆 Agent Scorecards</h3>
        <span className="pill info">{report.closed_trades} closed trades scored</span>
      </div>
      {report.closed_trades === 0 ? (
        <div style={{ color: 'var(--muted)' }}>
          No closed trades with persisted consensus yet — scorecards activate after the first ~5 closed trades.
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
              <th style={{ padding: 6 }}>Agent</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Decided</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Hit-rate</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Saved (abstain)</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Missed</th>
              <th style={{ padding: 6, textAlign: 'right' }}>P&L credited</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Vote weight</th>
            </tr>
          </thead>
          <tbody>
            {report.agents.map((a) => {
              const w = weights?.[a.agent];
              return (
                <tr key={a.agent} style={{ borderTop: '1px solid var(--border)' }}>
                  <td style={{ padding: 6, fontWeight: 600 }}>{a.role}</td>
                  <td style={{ padding: 6, textAlign: 'right' }}>{a.decided_trades}</td>
                  <td style={{ padding: 6, textAlign: 'right' }}>
                    <span className={pillFor(a.hit_rate)}>
                      {a.hit_rate != null ? `${Math.round(a.hit_rate * 100)}%` : '—'}
                    </span>
                  </td>
                  <td style={{ padding: 6, textAlign: 'right', color: 'var(--accent)' }}>{a.avoided_losers}</td>
                  <td style={{ padding: 6, textAlign: 'right', color: 'var(--danger)' }}>{a.missed_winners}</td>
                  <td style={{ padding: 6, textAlign: 'right',
                                  color: a.pnl_attributed >= 0 ? 'var(--accent)' : 'var(--danger)',
                                  fontWeight: 600 }}>
                    {a.pnl_attributed >= 0 ? '+' : ''}${a.pnl_attributed.toFixed(2)}
                  </td>
                  <td style={{ padding: 6, textAlign: 'right' }}>
                    {w != null ? (
                      <span className={w > 1 ? 'pill on' : w < 1 ? 'pill danger' : 'pill off'}>
                        ×{w.toFixed(2)}
                      </span>
                    ) : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
