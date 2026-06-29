/* MITS Phase 19 Cluster C — CalibrationChart.
 *
 * Renders a calibration plot: predicted (composite-quality bin midpoint, 0-100)
 * vs realized (win_rate or mean_pnl_pct) from /decision/scorecard.
 *
 * Two modes (props.mode):
 *   "win_rate"   — calibration_bins[] from the API (predicted vs realized win %)
 *   "expectancy" — expectancy_by_bin[] (predicted vs mean realized P&L %)
 *
 * Both inputs are 10 bins (0-10, 10-20, …, 90-100). Bars are drawn against the
 * 45° identity reference line so the operator can read "above the line = under-
 * confident, below = over-confident" without doing arithmetic.
 *
 * All-empty bins render an EmptyState (most operators will see this on day 1).
 */
import React from 'react';
import { EmptyState } from '../../design/Components.jsx';

const W = 480;
const H = 280;
const PAD_L = 40;
const PAD_R = 12;
const PAD_T = 14;
const PAD_B = 32;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

function binMid(label) {
  // "60-70" → 65
  if (typeof label !== 'string') return null;
  const m = label.match(/^(\d+)-(\d+)$/);
  if (!m) return null;
  return (Number(m[1]) + Number(m[2])) / 2;
}

export default function CalibrationChart({ bins = [], mode = 'win_rate', height = H }) {
  // Accept either shape. For win_rate the field is `win_rate`; for expectancy
  // it's `mean_pnl_pct`. Strip nulls early so we can short-circuit cleanly.
  const valueKey = mode === 'expectancy' ? 'mean_pnl_pct' : 'win_rate';
  const yLabel   = mode === 'expectancy' ? 'Realized P&L %' : 'Realized Win Rate %';
  const xLabel   = 'Predicted Composite Quality (bin midpoint)';

  const points = (bins || [])
    .map(b => ({
      mid: binMid(b.bin),
      v:   b[valueKey],
      n:   b.n,
      bin: b.bin,
    }))
    .filter(p => p.mid != null && p.v != null && Number.isFinite(p.v));

  if (points.length === 0) {
    return (
      <EmptyState
        icon="∅"
        message={`No ${mode === 'expectancy' ? 'expectancy' : 'calibration'} samples yet — need closed trades in each bin.`}
      />
    );
  }

  // Y-axis: for win_rate 0..100%, for expectancy auto-fit around 0.
  let yMin, yMax;
  if (mode === 'win_rate') {
    yMin = 0; yMax = 100;
  } else {
    const vs = points.map(p => p.v);
    const lo = Math.min(...vs, 0);
    const hi = Math.max(...vs, 0);
    const pad = Math.max(1, (hi - lo) * 0.15);
    yMin = lo - pad; yMax = hi + pad;
  }
  const ySpan = yMax - yMin || 1;

  function x(mid) { return PAD_L + (mid / 100) * PLOT_W; }
  function y(v)   { return PAD_T + PLOT_H - ((v - yMin) / ySpan) * PLOT_H; }

  // Identity reference: for win_rate, y = x. For expectancy, x=0 baseline only.
  const idLine = mode === 'win_rate'
    ? `M ${x(0)} ${y(0)} L ${x(100)} ${y(100)}`
    : `M ${x(0)} ${y(0)} L ${x(100)} ${y(0)}`;

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Calibration chart">
      {/* Plot border */}
      <rect x={PAD_L} y={PAD_T} width={PLOT_W} height={PLOT_H}
            fill="var(--bg-secondary)" stroke="var(--border-subtle)" />

      {/* Y-axis labels (5 ticks) */}
      {[0, 0.25, 0.5, 0.75, 1].map((t, i) => {
        const yv = yMin + ySpan * t;
        return (
          <g key={i}>
            <line x1={PAD_L} x2={PAD_L + PLOT_W}
                  y1={y(yv)} y2={y(yv)}
                  stroke="var(--border-subtle)" strokeDasharray="2 3" opacity="0.5" />
            <text x={PAD_L - 6} y={y(yv) + 3}
                  fontSize="9" fontFamily="var(--font-mono)"
                  fill="var(--text-tertiary)" textAnchor="end">
              {mode === 'expectancy' ? `${yv.toFixed(1)}%` : `${yv.toFixed(0)}%`}
            </text>
          </g>
        );
      })}

      {/* X-axis labels (every 20 units) */}
      {[0, 20, 40, 60, 80, 100].map((v, i) => (
        <text key={i}
              x={x(v)} y={PAD_T + PLOT_H + 14}
              fontSize="9" fontFamily="var(--font-mono)"
              fill="var(--text-tertiary)" textAnchor="middle">
          {v}
        </text>
      ))}

      {/* Identity / baseline line */}
      <path d={idLine}
            stroke="var(--accent-cyan)"
            strokeDasharray="4 3"
            strokeWidth="1"
            opacity="0.55"
            fill="none" />

      {/* Data points + connecting line */}
      <path d={points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.mid)} ${y(p.v)}`).join(' ')}
            stroke="var(--accent-green)"
            strokeWidth="1.5"
            fill="none" />
      {points.map((p, i) => (
        <g key={i}>
          <circle cx={x(p.mid)} cy={y(p.v)}
                  r={Math.max(3, Math.min(8, 2 + (p.n || 0) / 10))}
                  fill="var(--accent-green)"
                  stroke="var(--bg-primary)" strokeWidth="1" />
          <title>{`bin ${p.bin}: n=${p.n}, ${mode === 'expectancy' ? `${p.v.toFixed(2)}%` : `${p.v.toFixed(1)}% win`}`}</title>
        </g>
      ))}

      {/* Axis labels */}
      <text x={PAD_L + PLOT_W / 2} y={H - 4}
            fontSize="10" fill="var(--text-secondary)" textAnchor="middle">
        {xLabel}
      </text>
      <text x={10} y={PAD_T + PLOT_H / 2}
            fontSize="10" fill="var(--text-secondary)"
            textAnchor="middle"
            transform={`rotate(-90 10 ${PAD_T + PLOT_H / 2})`}>
        {yLabel}
      </text>
    </svg>
  );
}
