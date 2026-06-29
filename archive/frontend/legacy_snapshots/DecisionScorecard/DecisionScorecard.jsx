/**
 * MITS Phase 16.C — Decision Quality Scorecard page.
 *
 * Reads /decision/scorecard?window=N for the rolling distribution +
 * per-axis means + calibration bins + expectancy by bin. The page is
 * three stacked panels:
 *
 *   1. Composite KPIs strip — mean / median / stddev / N.
 *   2. Four sub-score bars (analysis, council, risk, execution).
 *   3. Calibration scatter — bin midpoint vs realized win rate.
 *   4. Expectancy table — bin / N / mean pnl pct.
 */
import React, { useEffect, useMemo, useState } from 'react';

async function api(path) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

function KPI({ label, value, hint, color }) {
  return (
    <div style={{
      padding: 14, borderRadius: 8, background: '#111827',
      border: '1px solid #1f2937', flex: 1, minWidth: 180,
    }}>
      <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 6 }}>
        {label}
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, color: color || '#e5e7eb' }}>
        {value}
      </div>
      {hint && (
        <div style={{ fontSize: 11, color: '#6b7280', marginTop: 4 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

function AxisBar({ label, mean, median }) {
  const pct = mean == null ? 0 : Math.round(mean);
  const color = pct >= 70 ? '#10b981'
               : pct >= 50 ? '#06b6d4'
               : pct >= 30 ? '#f97316'
               : '#ef4444';
  return (
    <div style={{
      flex: '1 1 180px', minWidth: 180,
      padding: 12, background: '#0a0a0a',
      borderRadius: 6, border: '1px solid #1f2937',
    }}>
      <div style={{
        fontSize: 11, color: '#9ca3af',
        textTransform: 'uppercase',
        letterSpacing: '0.05em', marginBottom: 4,
      }}>
        {label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, color }}>
        {mean == null ? '—' : `${pct}`}
      </div>
      <div style={{
        marginTop: 6, height: 6,
        background: '#1f2937', borderRadius: 3,
      }}>
        <div style={{
          width: `${pct}%`, height: '100%',
          background: color, borderRadius: 3,
        }} />
      </div>
      <div style={{ marginTop: 6, fontSize: 11, color: '#6b7280' }}>
        median: {median == null ? '—' : median.toFixed(1)}
      </div>
    </div>
  );
}

function CalibrationScatter({ bins }) {
  const w = 520;
  const h = 280;
  const pad = 40;
  const valid = (bins || []).filter((b) => b.win_rate != null && b.n > 0);
  if (!valid.length) {
    return (
      <div style={{ color: '#9ca3af', padding: 12 }}>
        No resolved trades in the window yet — scatter populates as
        trades close.
      </div>
    );
  }
  // x = bin midpoint as 0..100, y = win_rate 0..1.
  const x = (mid) => pad + (mid / 100) * (w - 2 * pad);
  const y = (wr) => h - pad - wr * (h - 2 * pad);
  const midOf = (label) => {
    const [lo, hi] = label.split('-').map(Number);
    return (lo + hi) / 2;
  };
  return (
    <svg width={w} height={h} style={{
      background: '#0a0a0a', borderRadius: 8,
    }}>
      {/* y=x diagonal — perfect calibration when WR rises with score. */}
      <line x1={x(0)} y1={y(0)} x2={x(100)} y2={y(1)}
            stroke="#374151" strokeDasharray="4 4" />
      <text x={pad} y={h - 8} fill="#9ca3af" fontSize="11">
        composite score
      </text>
      <text x={4} y={pad} fill="#9ca3af" fontSize="11">
        win rate
      </text>
      {bins.map((b, i) => {
        if (b.win_rate == null || !b.n) return null;
        const mid = midOf(b.bin);
        const r = 3 + Math.min(10, Math.sqrt(Math.max(0, b.n)));
        return (
          <g key={i}>
            <circle cx={x(mid)} cy={y(b.win_rate)}
                    r={r} fill="#60a5fa" fillOpacity={0.7} />
            <text x={x(mid) + r + 3} y={y(b.win_rate) + 3}
                  fill="#9ca3af" fontSize="10">N={b.n}</text>
          </g>
        );
      })}
    </svg>
  );
}

function ExpectancyTable({ rows }) {
  return (
    <div style={{ background: '#0a0a0a', borderRadius: 8, overflow: 'hidden' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ background: '#111827', color: '#9ca3af' }}>
            <th style={{ textAlign: 'left', padding: 10 }}>Composite bin</th>
            <th style={{ textAlign: 'right', padding: 10 }}>N</th>
            <th style={{ textAlign: 'right', padding: 10 }}>Mean P&L %</th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).map((r) => {
            const pnl = r.mean_pnl_pct;
            const color = pnl == null ? '#6b7280'
                          : pnl > 0 ? '#10b981'
                          : pnl < 0 ? '#ef4444'
                          : '#9ca3af';
            return (
              <tr key={r.bin} style={{ borderTop: '1px solid #1f2937' }}>
                <td style={{ padding: 10 }}>{r.bin}</td>
                <td style={{ padding: 10, textAlign: 'right',
                             color: '#9ca3af' }}>
                  {r.n}
                </td>
                <td style={{ padding: 10, textAlign: 'right', color }}>
                  {pnl == null ? '—' : `${pnl.toFixed(2)}%`}
                </td>
              </tr>
            );
          })}
          {(!rows || rows.length === 0) && (
            <tr>
              <td colSpan={3} style={{ padding: 16, color: '#9ca3af',
                                       textAlign: 'center' }}>
                No bins yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function DecisionScorecard() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [windowSize, setWindowSize] = useState(50);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await api(`/decision/scorecard?window=${windowSize}`);
        if (alive) setData(d);
      } catch (e) {
        if (alive) setErr(String(e));
      }
    })();
    return () => { alive = false; };
  }, [windowSize]);

  const dist = data?.composite_distribution;
  const axes = data?.by_sub_score;

  const compositeColor = useMemo(() => {
    if (!dist?.mean) return '#e5e7eb';
    if (dist.mean >= 70) return '#10b981';
    if (dist.mean >= 50) return '#06b6d4';
    if (dist.mean >= 30) return '#f97316';
    return '#ef4444';
  }, [dist]);

  if (err) {
    return (
      <div style={{ padding: 24, color: '#ef4444' }}>Error: {err}</div>
    );
  }
  if (!data) {
    return (
      <div style={{ padding: 24 }}>Loading decision scorecard…</div>
    );
  }

  return (
    <div style={{ padding: 16 }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'baseline', marginBottom: 12,
      }}>
        <h1 style={{ fontSize: 22, margin: 0 }}>Decision Quality Scorecard</h1>
        <div>
          <label style={{ fontSize: 12, color: '#9ca3af', marginRight: 6 }}>
            window
          </label>
          <select value={windowSize}
                  onChange={(e) => setWindowSize(Number(e.target.value))}>
            <option value={20}>20</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
            <option value={500}>500</option>
          </select>
        </div>
      </div>

      {/* Composite KPI strip */}
      <div style={{
        display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16,
      }}>
        <KPI label="Composite (mean)"
             value={dist?.mean == null ? '—' : dist.mean.toFixed(1)}
             color={compositeColor}
             hint={`window=${data.window} • N=${data.n_rows}`} />
        <KPI label="Composite (median)"
             value={dist?.median == null ? '—' : dist.median.toFixed(1)} />
        <KPI label="Composite stddev"
             value={dist?.stddev == null ? '—' : dist.stddev.toFixed(1)}
             hint="spread of decision quality" />
      </div>

      {/* Per-axis bars */}
      <div style={{
        background: '#111827', borderRadius: 8, padding: 12,
        border: '1px solid #1f2937', marginBottom: 16,
      }}>
        <div style={{ fontSize: 14, marginBottom: 12, color: '#e5e7eb' }}>
          Sub-scores (rolling mean)
        </div>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <AxisBar label="Analysis"
                   mean={axes?.analysis_quality?.mean}
                   median={axes?.analysis_quality?.median} />
          <AxisBar label="Council"
                   mean={axes?.council_agreement?.mean}
                   median={axes?.council_agreement?.median} />
          <AxisBar label="Risk"
                   mean={axes?.risk_quality?.mean}
                   median={axes?.risk_quality?.median} />
          <AxisBar label="Execution"
                   mean={axes?.execution_quality?.mean}
                   median={axes?.execution_quality?.median} />
        </div>
      </div>

      {/* Calibration scatter */}
      <div style={{
        background: '#111827', borderRadius: 8, padding: 12,
        border: '1px solid #1f2937', marginBottom: 16,
      }}>
        <div style={{ fontSize: 14, marginBottom: 8, color: '#e5e7eb' }}>
          Calibration — composite score (10 bins) vs realized win rate
        </div>
        <CalibrationScatter bins={data.calibration_bins} />
      </div>

      {/* Expectancy table */}
      <div style={{
        background: '#111827', borderRadius: 8, padding: 12,
        border: '1px solid #1f2937',
      }}>
        <div style={{ fontSize: 14, marginBottom: 8, color: '#e5e7eb' }}>
          Expectancy by composite bin
        </div>
        <ExpectancyTable rows={data.expectancy_by_bin} />
      </div>
    </div>
  );
}
