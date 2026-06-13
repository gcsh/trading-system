/**
 * MITS Phase 14.D — Brain Scorecard page.
 *
 * Reads /brain/scorecard for the headline numbers + calibration bins
 * and /brain/predictions for the recent predictions table.
 *
 * Three panels:
 *   1. Headline KPIs (predicted WR, realized WR, calibration gap pp).
 *   2. Calibration plot — 10 bins, predicted_midpoint vs realized WR.
 *   3. Invalidation hit-rate gauge.
 *   4. Recent predictions table with outcome chips.
 */
import React, { useEffect, useMemo, useState } from 'react';

async function api(path) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

const OUTCOME_STYLES = {
  win:        { bg: '#10b98122', fg: '#10b981', border: '1px solid #10b981' },
  loss:       { bg: '#ef444422', fg: '#ef4444', border: '1px solid #ef4444' },
  scratch:    { bg: '#a3a3a322', fg: '#a3a3a3', border: '1px solid #a3a3a3' },
  not_traded: { bg: '#37415122', fg: '#9ca3af', border: '1px solid #37415133' },
  pending:    { bg: '#06b6d422', fg: '#06b6d4', border: '1px solid #06b6d4' },
};

function OutcomeChip({ outcome }) {
  const s = OUTCOME_STYLES[outcome] || OUTCOME_STYLES.pending;
  return (
    <span style={{
      background: s.bg, color: s.fg, border: s.border,
      padding: '3px 9px', borderRadius: 12, fontSize: 11,
      fontWeight: 700, letterSpacing: '0.04em', textTransform: 'uppercase',
    }}>
      {outcome}
    </span>
  );
}

function KPI({ label, value, hint, color }) {
  return (
    <div style={{
      padding: 14, borderRadius: 8, background: '#111827',
      border: '1px solid #1f2937', flex: 1, minWidth: 180,
    }}>
      <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, color: color || '#e5e7eb' }}>
        {value}
      </div>
      {hint && (
        <div style={{ fontSize: 11, color: '#6b7280', marginTop: 4 }}>{hint}</div>
      )}
    </div>
  );
}

function CalibrationPlot({ bins }) {
  const w = 480;
  const h = 280;
  const pad = 36;
  if (!bins || !bins.length) {
    return <div style={{ color: '#9ca3af' }}>No calibration data yet.</div>;
  }
  const xs = bins.map((b) => b.bin_midpoint);
  const ys = bins.map((b) => b.realized_win_rate);
  const x = (v) => pad + v * (w - 2 * pad);
  const y = (v) => h - pad - v * (h - 2 * pad);
  return (
    <svg width={w} height={h} style={{ background: '#0a0a0a', borderRadius: 8 }}>
      {/* Diagonal reference (perfect calibration). */}
      <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)}
            stroke="#374151" strokeDasharray="4 4" />
      {/* Axis labels. */}
      <text x={pad} y={h - 8} fill="#9ca3af" fontSize="11">predicted</text>
      <text x={4} y={pad} fill="#9ca3af" fontSize="11">realized</text>
      {/* Bin dots — area scales with N. */}
      {bins.map((b, i) => {
        if (b.realized_win_rate == null) return null;
        const r = 3 + Math.min(10, Math.sqrt(Math.max(0, b.n)));
        return (
          <g key={i}>
            <circle cx={x(b.predicted_mean)} cy={y(b.realized_win_rate)}
                    r={r} fill="#60a5fa" fillOpacity={0.7} />
            <text x={x(b.predicted_mean) + r + 3} y={y(b.realized_win_rate) + 3}
                  fill="#9ca3af" fontSize="10">N={b.n}</text>
          </g>
        );
      })}
    </svg>
  );
}

function PerAxisCalibration({ data }) {
  // MITS Phase 15.E — five small bars showing the per-component
  // predicted_correct_rate for the regime, technical, options, analog,
  // and strategy axes, with sample size as a footer chip.
  const AXES = ['regime', 'technical', 'options', 'analog', 'strategy'];
  const entries = AXES.map((axis) => ({
    axis,
    rate: data?.[axis]?.predicted_correct_rate ?? null,
    n: data?.[axis]?.n ?? 0,
  }));
  const hasAny = entries.some((e) => e.n > 0);
  return (
    <div style={{
      background: '#111827', borderRadius: 8, padding: 12,
      border: '1px solid #1f2937', marginBottom: 16,
    }}>
      <div style={{ fontSize: 14, marginBottom: 12, color: '#e5e7eb' }}>
        Per-Axis Calibration (component-level correctness)
      </div>
      {!hasAny && (
        <div style={{ color: '#9ca3af', fontSize: 12 }}>
          No per-axis data yet — populates as the nightly linker resolves
          predictions stamped with regime/strategy snapshots.
        </div>
      )}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {entries.map(({ axis, rate, n }) => {
          const pct = rate == null ? 0 : Math.round(rate * 100);
          const color = n === 0 ? '#374151'
                       : pct >= 60 ? '#10b981'
                       : pct >= 40 ? '#06b6d4'
                       : '#ef4444';
          return (
            <div key={axis} style={{
              flex: '1 1 140px', minWidth: 140,
              padding: 10, background: '#0a0a0a',
              borderRadius: 6, border: '1px solid #1f2937',
            }}>
              <div style={{ fontSize: 11, color: '#9ca3af',
                            textTransform: 'uppercase',
                            letterSpacing: '0.05em', marginBottom: 4 }}>
                {axis}
              </div>
              <div style={{ fontSize: 22, fontWeight: 700, color }}>
                {n === 0 ? '—' : `${pct}%`}
              </div>
              <div style={{ marginTop: 6, height: 6,
                            background: '#1f2937', borderRadius: 3 }}>
                <div style={{ width: `${pct}%`, height: '100%',
                              background: color, borderRadius: 3 }} />
              </div>
              <div style={{ marginTop: 6, fontSize: 11, color: '#6b7280' }}>
                N={n}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


function InvalidationGauge({ rate, savings }) {
  const pct = Math.round((rate || 0) * 100);
  const savedPct = Math.round((savings || 0) * 100);
  const color = pct >= 50 ? '#10b981' : pct >= 25 ? '#06b6d4' : '#a3a3a3';
  return (
    <div style={{
      padding: 14, borderRadius: 8, background: '#111827',
      border: '1px solid #1f2937', minWidth: 200,
    }}>
      <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 6 }}>
        Invalidation Hit Rate
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, color }}>{pct}%</div>
      <div style={{ marginTop: 8, height: 8, background: '#1f2937', borderRadius: 4 }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 4 }} />
      </div>
      <div style={{ fontSize: 11, color: '#6b7280', marginTop: 6 }}>
        {savedPct}% of hits would have saved capital
      </div>
    </div>
  );
}

export default function BrainScorecard() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [windowSize] = useState(50);
  const [surface, setSurface] = useState('');

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const qs = surface
          ? `?window=${windowSize}&surface=${encodeURIComponent(surface)}`
          : `?window=${windowSize}`;
        const d = await api(`/brain/scorecard${qs}`);
        if (alive) setData(d);
      } catch (e) {
        if (alive) setErr(String(e));
      }
    })();
    return () => { alive = false; };
  }, [windowSize, surface]);

  const card = data?.scorecard;
  const recent = data?.recent_predictions || [];

  const gapColor = useMemo(() => {
    if (!card) return '#e5e7eb';
    const gap = card.calibration_gap_pp;
    if (gap >= 10) return '#ef4444';
    if (gap >= 5) return '#f97316';
    if (gap <= -5) return '#06b6d4';
    return '#10b981';
  }, [card]);

  if (err) return <div style={{ padding: 24, color: '#ef4444' }}>Error: {err}</div>;
  if (!data) return <div style={{ padding: 24 }}>Loading brain scorecard…</div>;

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                    alignItems: 'baseline', marginBottom: 12 }}>
        <h1 style={{ fontSize: 22, margin: 0 }}>Brain Calibration Scorecard</h1>
        <div style={{ display: 'flex', gap: 8 }}>
          <select value={surface} onChange={(e) => setSurface(e.target.value)}>
            <option value="">all surfaces</option>
            <option value="analysis">analysis</option>
            <option value="eod_analysis">eod_analysis</option>
            <option value="opportunity_brain">opportunity_brain</option>
          </select>
        </div>
      </div>

      {/* KPI strip */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <KPI label="Resolved predictions"
             value={card.window_trades}
             hint={`window=${data.window}`} />
        <KPI label="Predicted win rate"
             value={`${(card.predicted_win_rate * 100).toFixed(1)}%`} />
        <KPI label="Realized win rate"
             value={`${(card.realized_win_rate * 100).toFixed(1)}%`} />
        <KPI label="Calibration gap"
             value={`${card.calibration_gap_pp > 0 ? '+' : ''}${card.calibration_gap_pp.toFixed(1)} pp`}
             color={gapColor}
             hint="predicted − realized (positive = over-confident)" />
        <InvalidationGauge rate={card.invalidation_hit_rate}
                           savings={card.invalidation_saved_capital_rate} />
      </div>

      {/* Calibration plot */}
      <div style={{ background: '#111827', borderRadius: 8,
                    padding: 12, marginBottom: 16,
                    border: '1px solid #1f2937' }}>
        <div style={{ fontSize: 14, marginBottom: 8, color: '#e5e7eb' }}>
          Reliability plot (10 bins)
        </div>
        <CalibrationPlot bins={card.calibration_bins} />
      </div>

      {/* Per-axis calibration (15.E) */}
      <PerAxisCalibration data={card.per_axis_calibration} />

      {/* Recent predictions table */}
      <div style={{ background: '#0a0a0a', borderRadius: 8, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#111827', color: '#9ca3af' }}>
              <th style={{ textAlign: 'left', padding: 10 }}>When</th>
              <th style={{ textAlign: 'left', padding: 10 }}>Ticker</th>
              <th style={{ textAlign: 'left', padding: 10 }}>Surface</th>
              <th style={{ textAlign: 'left', padding: 10 }}>Pattern</th>
              <th style={{ textAlign: 'left', padding: 10 }}>Action</th>
              <th style={{ textAlign: 'right', padding: 10 }}>Posterior</th>
              <th style={{ textAlign: 'right', padding: 10 }}>Realized %</th>
              <th style={{ textAlign: 'center', padding: 10 }}>Invalidation</th>
              <th style={{ textAlign: 'center', padding: 10 }}>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((r) => {
              const t = r.created_at ? r.created_at.replace('T', ' ').slice(0, 16) : '—';
              const post = r.posterior_at_decision != null
                ? `${(r.posterior_at_decision * 100).toFixed(0)}%` : '—';
              const realized = r.actual_pnl_pct != null
                ? `${(r.actual_pnl_pct * 100).toFixed(2)}%` : '—';
              const invMark = r.invalidation_hit === true ? 'hit'
                            : r.invalidation_hit === false ? 'no'
                            : '—';
              return (
                <tr key={r.id} style={{ borderTop: '1px solid #1f2937' }}>
                  <td style={{ padding: 10, color: '#9ca3af' }}>{t}</td>
                  <td style={{ padding: 10, fontWeight: 700 }}>{r.ticker}</td>
                  <td style={{ padding: 10, color: '#9ca3af' }}>{r.surface}</td>
                  <td style={{ padding: 10 }}>{r.pattern || '—'}</td>
                  <td style={{ padding: 10 }}>
                    {r.suggested_action || '—'}
                    {r.suggested_strike != null && (
                      <span style={{ color: '#6b7280', marginLeft: 6 }}>
                        ${r.suggested_strike}
                      </span>
                    )}
                  </td>
                  <td style={{ padding: 10, textAlign: 'right' }}>{post}</td>
                  <td style={{ padding: 10, textAlign: 'right' }}>{realized}</td>
                  <td style={{ padding: 10, textAlign: 'center', color: '#9ca3af' }}>
                    {invMark}
                  </td>
                  <td style={{ padding: 10, textAlign: 'center' }}>
                    <OutcomeChip outcome={r.outcome} />
                  </td>
                </tr>
              );
            })}
            {recent.length === 0 && (
              <tr>
                <td colSpan={9} style={{ padding: 16, color: '#9ca3af', textAlign: 'center' }}>
                  No predictions yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
