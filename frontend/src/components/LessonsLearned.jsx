/**
 * Stage-17 — Lessons Learned panel.
 *
 * Surfaces /journal/lessons. Each lesson is an *institutional rule*
 * mined from closed trades — pattern (e.g. "ORB in choppy regime"),
 * sample size, win rate vs baseline, expectancy, suggested size
 * multiplier. NOT trade analogues. Lives on the Cockpit so the
 * operator sees the system's accumulated wisdom at a glance.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const ACTION_PILL = {
  abstain: 'pill danger',
  reduce_size_50: 'pill danger',
  reduce_size_25: 'pill purple',
  unchanged: 'pill off',
  increase_size_25: 'pill on',
  increase_size_50: 'pill on',
};

const SEV_BORDER = {
  alert: 'var(--danger)',
  warn: 'var(--accent-2)',
  info: 'var(--border-strong)',
};

function formatAction(action) {
  return ({
    abstain: 'ABSTAIN',
    reduce_size_50: 'SIZE × 0.5',
    reduce_size_25: 'SIZE × 0.75',
    unchanged: 'NO ADJUSTMENT',
    increase_size_25: 'SIZE × 1.25',
    increase_size_50: 'SIZE × 1.5',
  })[action] || action;
}

export default function LessonsLearned() {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchJson('/journal/lessons?top_k=10')
      .then(setReport)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>📚 Lessons Learned</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!report) {
    return <div className="panel col-12"><h3 style={{ marginTop: 0 }}>📚 Lessons Learned</h3>Loading…</div>;
  }

  const { lessons = [], baseline_win_rate, baseline_expectancy, total_closed_trades } = report;

  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>📚 Lessons Learned</h3>
        <div className="row" style={{ gap: 8 }}>
          <span className="pill info">{total_closed_trades} closed trades mined</span>
          {baseline_win_rate != null && (
            <span className="pill purple">baseline WR {(baseline_win_rate * 100).toFixed(0)}%</span>
          )}
          {baseline_expectancy != null && (
            <span className={`pill ${baseline_expectancy >= 0 ? 'on' : 'danger'}`}>
              baseline E[${baseline_expectancy.toFixed(2)}]
            </span>
          )}
        </div>
      </div>
      {lessons.length === 0 ? (
        <div style={{ color: 'var(--muted)' }}>
          Not enough closed trades to mine lessons yet — patterns surface once each (strategy × condition) bucket has ≥8 trades.
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 6 }}>
          {lessons.map((l, i) => (
            <div key={i} style={{
              padding: '8px 12px',
              background: 'var(--panel-2)',
              border: '1px solid var(--border)',
              borderLeft: `3px solid ${SEV_BORDER[l.severity] || 'var(--border-strong)'}`,
              borderRadius: 6,
            }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{l.pattern}</div>
                <div className="row" style={{ gap: 6, alignItems: 'center' }}>
                  <span className={ACTION_PILL[l.suggested_action] || 'pill info'}>
                    {formatAction(l.suggested_action)}
                  </span>
                  <span className="pill info">n = {l.sample_size}</span>
                </div>
              </div>
              <div className="row" style={{ gap: 12, marginTop: 4, color: 'var(--muted)', fontSize: 12, flexWrap: 'wrap' }}>
                <span>WR <strong style={{
                  color: l.delta_pp < 0 ? 'var(--danger)' : 'var(--accent)',
                }}>{Math.round(l.win_rate * 100)}%</strong>
                  {' '}vs baseline {Math.round(l.baseline_win_rate * 100)}%
                  {' '}({l.delta_pp > 0 ? '+' : ''}{(l.delta_pp * 100).toFixed(1)} pp)</span>
                <span>E[<strong style={{
                  color: l.expectancy < 0 ? 'var(--danger)' : 'var(--accent)',
                }}>${l.expectancy.toFixed(2)}</strong>]</span>
                {l.expectancy_r != null && (
                  <span>{l.expectancy_r > 0 ? '+' : ''}{l.expectancy_r.toFixed(2)}R</span>
                )}
                {l.profit_factor != null && (
                  <span>PF {l.profit_factor.toFixed(2)}</span>
                )}
                <span style={{ fontStyle: 'italic' }}>
                  CI [{(l.confidence_bound_lo * 100).toFixed(0)}, {(l.confidence_bound_hi * 100).toFixed(0)}]%
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
