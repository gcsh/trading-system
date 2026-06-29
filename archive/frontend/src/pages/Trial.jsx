/**
 * Stage-17 — Promotion Readiness page (the "30-Day Trial" view).
 *
 * The right success metric after the paper trial isn't account P&L —
 * it's whether the system has enough sample size, is well-calibrated,
 * and has positive expectancy. This page surfaces those three axes
 * with progress bars + an overall verdict.
 *
 * Pulls from /trial/readiness which composes /metrics/summary + /gates/status.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const STATUS_PILL = {
  ready: 'pill on',
  ready_with_caveats: 'pill info',
  need_more_data: 'pill purple',
  need_calibration: 'pill danger',
  need_edge: 'pill danger',
};

function Progress({ pct, color = 'var(--accent)', height = 10 }) {
  return (
    <div style={{ width: '100%', background: 'var(--panel-2)', borderRadius: 4, height, overflow: 'hidden' }}>
      <div style={{ width: `${Math.max(1, Math.min(100, pct * 100))}%`, height: '100%', background: color }} />
    </div>
  );
}

function Stat({ label, value, target, fmt = (v) => v, tone, hint }) {
  const ok = tone === 'good';
  const bad = tone === 'bad';
  return (
    <div style={{ minWidth: 180 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>
        {label}
      </div>
      <div style={{
        fontSize: 22, fontWeight: 700, marginTop: 2,
        color: ok ? 'var(--accent)' : bad ? 'var(--danger)' : 'var(--text)',
      }}>
        {value == null ? '—' : fmt(value)}
      </div>
      {target != null && (
        <div style={{ fontSize: 11, color: 'var(--muted)' }}>
          target {fmt(target)}
        </div>
      )}
      {hint && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function compareTone(value, target, direction) {
  if (value == null) return 'neutral';
  return direction === 'lte'
    ? (value <= target ? 'good' : 'bad')
    : (value >= target ? 'good' : 'bad');
}

const fmtPct = (v) => `${(v * 100).toFixed(0)}%`;
const fmt2 = (v) => Number(v).toFixed(2);
const fmt3 = (v) => Number(v).toFixed(3);
const fmtMoney = (v) => `$${Number(v).toFixed(2)}`;

export default function Trial() {
  const [body, setBody] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchJson('/trial/readiness?min_trades=100&target_trades=200')
      .then(setBody)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="panel">
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!body) return <div className="panel">Loading readiness…</div>;

  const { trial, verdict, progress, gates, gates_summary } = body;
  const { sample_size, calibration, edge } = progress;

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      {/* Verdict */}
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>
              Trial verdict
            </div>
            <h2 style={{ margin: '4px 0 0' }}>{verdict.headline}</h2>
          </div>
          <div className="row" style={{ gap: 8, alignItems: 'center' }}>
            <span className={STATUS_PILL[verdict.status] || 'pill info'}>{verdict.status.replace(/_/g, ' ')}</span>
            <span className="pill info">day {trial.days_in} of {trial.days_total}</span>
            <span className={`pill ${gates_summary.overall === 'pass' ? 'on' : gates_summary.overall === 'fail' ? 'danger' : 'off'}`}>
              gates: {gates_summary.pass} pass / {gates_summary.fail} fail / {gates_summary.insufficient} insufficient
            </span>
          </div>
        </div>
        {verdict.blockers?.length > 0 && (
          <div style={{ marginTop: 10, color: 'var(--muted)', fontSize: 13 }}>
            <strong>Blocking gates:</strong> {verdict.blockers.join(', ')}
          </div>
        )}
      </div>

      {/* Sample size */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>📊 Sample size</h3>
        <p style={{ color: 'var(--muted)', marginTop: 0, fontSize: 13 }}>
          Calibration gates can't be trusted without enough closed trades. Minimum to publish: {sample_size.minimum}. Preferred: {sample_size.target}.
        </p>
        <div className="row" style={{ gap: 16, alignItems: 'center', marginTop: 8 }}>
          <Stat label="Closed trades" value={sample_size.current} target={sample_size.minimum}
                  tone={sample_size.current >= sample_size.minimum ? 'good' : 'neutral'} />
          <div style={{ flex: 1, minWidth: 240 }}>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
              progress to minimum
            </div>
            <Progress pct={sample_size.min_pct} color={sample_size.min_pct >= 1 ? 'var(--accent)' : 'var(--accent-2)'} />
            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8, marginBottom: 4 }}>
              progress to target
            </div>
            <Progress pct={sample_size.target_pct} />
          </div>
        </div>
      </div>

      {/* Calibration */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>🎯 Calibration stability</h3>
        <p style={{ color: 'var(--muted)', marginTop: 0, fontSize: 13 }}>
          A calibrated 70% prediction that wins 70% of the time is more valuable than a model winning 80% one week and 20% the next.
        </p>
        <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
          <Stat label="Brier" value={calibration.brier} target={calibration.brier_target} fmt={fmt3}
                  tone={compareTone(calibration.brier, calibration.brier_target, 'lte')} />
          <Stat label="ECE" value={calibration.ece} target={calibration.ece_target} fmt={fmt3}
                  tone={compareTone(calibration.ece, calibration.ece_target, 'lte')} />
          <Stat label="Brier stability σ" value={calibration.brier_stability_std} target={calibration.brier_stability_target} fmt={fmt3}
                  tone={compareTone(calibration.brier_stability_std, calibration.brier_stability_target, 'lte')}
                  hint="std-dev across rolling windows" />
          <Stat label="ECE stability σ" value={calibration.ece_stability_std} target={calibration.ece_stability_target} fmt={fmt3}
                  tone={compareTone(calibration.ece_stability_std, calibration.ece_stability_target, 'lte')}
                  hint="std-dev across rolling windows" />
        </div>
      </div>

      {/* Edge */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>💪 Edge (expectancy, not raw accuracy)</h3>
        <p style={{ color: 'var(--muted)', marginTop: 0, fontSize: 13 }}>
          Win rate 42% with profit factor 2.4 beats win rate 80% with profit factor 0.9. We optimize for positive expectancy.
        </p>
        <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
          <Stat label="Expectancy" value={edge.expectancy} target={edge.expectancy_target} fmt={fmtMoney}
                  tone={compareTone(edge.expectancy, edge.expectancy_target, 'gte')} />
          <Stat label="Profit factor" value={edge.profit_factor} target={edge.profit_factor_target} fmt={fmt2}
                  tone={compareTone(edge.profit_factor, edge.profit_factor_target, 'gte')} />
          <Stat label="Win rate" value={edge.win_rate} target={edge.win_rate_target} fmt={fmtPct}
                  tone={compareTone(edge.win_rate, edge.win_rate_target, 'gte')} />
          <Stat label="Sharpe" value={edge.sharpe} target={edge.sharpe_target} fmt={fmt2}
                  tone={compareTone(edge.sharpe, edge.sharpe_target, 'gte')} />
          <Stat label="Max drawdown" value={edge.max_drawdown_pct} target={edge.max_drawdown_target} fmt={fmtPct}
                  tone={compareTone(edge.max_drawdown_pct, edge.max_drawdown_target, 'lte')} />
        </div>
      </div>

      {/* Full gate roster */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>⛩️ Stage-1.5 promotion contract — 9 gates</h3>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
              <th style={{ padding: 6 }}>Gate</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Value</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Threshold</th>
              <th style={{ padding: 6, textAlign: 'right' }}>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {gates.map((g) => (
              <tr key={g.name} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: 6, fontWeight: 600 }}>{g.name}</td>
                <td style={{ padding: 6, textAlign: 'right' }}>
                  {g.value != null ? Number(g.value).toFixed(g.value < 1 ? 3 : 2) : '—'}
                </td>
                <td style={{ padding: 6, textAlign: 'right', color: 'var(--muted)' }}>
                  {g.direction === 'lte' ? '≤' : '≥'} {Number(g.threshold).toFixed(g.threshold < 1 ? 3 : 2)}
                </td>
                <td style={{ padding: 6, textAlign: 'right' }}>
                  <span className={`pill ${g.verdict === 'pass' ? 'on' : g.verdict === 'fail' ? 'danger' : 'off'}`}>
                    {g.verdict}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
