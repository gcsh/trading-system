/**
 * Calibration — reliability diagram for win-probability predictions.
 *
 * Reads /metrics/calibration and renders:
 *   - Brier + ECE headline numbers
 *   - Reliability diagram (predicted vs actual per bin)
 *   - Per-bin sample-size strip so user knows where the model has evidence
 */
import React, { useEffect, useMemo, useState } from 'react';

function fmtPct(v, fallback = '—') {
  if (v == null || isNaN(v)) return fallback;
  return `${(v * 100).toFixed(1)}%`;
}
function fmtNum(v, digits = 4, fallback = '—') {
  if (v == null || isNaN(v)) return fallback;
  return Number(v).toFixed(digits);
}


function ReliabilityChart({ rows }) {
  // 360x360 SVG, [0,1]×[0,1] with the identity diagonal as reference.
  const W = 360, H = 360, PAD = 32;
  const x = (v) => PAD + v * (W - 2 * PAD);
  const y = (v) => H - PAD - v * (H - 2 * PAD);
  const maxCount = Math.max(1, ...rows.map((r) => r.count));

  return (
    <svg width={W} height={H} style={{ background: 'var(--panel-2)', borderRadius: 8 }}>
      {/* Grid */}
      {[0.2, 0.4, 0.6, 0.8].map((g) => (
        <g key={g}>
          <line x1={x(0)} x2={x(1)} y1={y(g)} y2={y(g)}
                stroke="var(--border)" strokeWidth="1" />
          <line y1={y(0)} y2={y(1)} x1={x(g)} x2={x(g)}
                stroke="var(--border)" strokeWidth="1" />
          <text x={PAD - 6} y={y(g)} textAnchor="end" fontSize="9"
                fill="var(--muted)" dominantBaseline="middle">{g}</text>
          <text y={H - PAD + 12} x={x(g)} textAnchor="middle" fontSize="9"
                fill="var(--muted)">{g}</text>
        </g>
      ))}
      {/* Identity line (perfect calibration). */}
      <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)}
            stroke="var(--muted)" strokeDasharray="4 4" strokeWidth="1.5" />
      {/* Reliability curve. */}
      {rows.length >= 2 && (
        <polyline
          fill="none" stroke="var(--accent)" strokeWidth="2"
          points={rows.map((r) => `${x(r.predicted)},${y(r.actual)}`).join(' ')}
        />
      )}
      {/* Point markers, size = log(count). */}
      {rows.map((r) => {
        const radius = 3 + 5 * (r.count / maxCount);
        return (
          <circle key={r.bin} cx={x(r.predicted)} cy={y(r.actual)} r={radius}
                  fill="var(--accent)" opacity="0.75" />
        );
      })}
      {/* Axis labels */}
      <text x={W / 2} y={H - 4} textAnchor="middle" fontSize="10" fill="var(--muted)">
        Predicted P(win)
      </text>
      <text x={10} y={H / 2} textAnchor="middle" fontSize="10" fill="var(--muted)"
            transform={`rotate(-90 10 ${H / 2})`}>
        Actual hit rate
      </text>
    </svg>
  );
}


function SampleStrip({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <div>
      <div style={{ fontSize: 10, color: 'var(--muted)', marginBottom: 4 }}>
        Sample size per bin (where the model has evidence)
      </div>
      <div className="row" style={{ gap: 2, alignItems: 'flex-end', height: 36 }}>
        {rows.map((r) => (
          <div key={r.bin} title={`${(r.lower * 100).toFixed(0)}–${(r.upper * 100).toFixed(0)}% bin · n=${r.count}`}
               style={{
                 flex: 1, height: `${10 + 80 * (r.count / max)}%`,
                 background: 'var(--accent)', opacity: 0.5,
                 borderRadius: '2px 2px 0 0', minWidth: 6,
               }} />
        ))}
      </div>
    </div>
  );
}


export default function Calibration() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [nBins, setNBins] = useState(10);

  useEffect(() => {
    fetch(`/metrics/calibration?n_bins=${nBins}&limit=10000`)
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, [nBins]);

  if (err) return <div className="empty">calibration endpoint error: {err}</div>;
  if (!data) return <div className="empty">Loading calibration data…</div>;

  const rows = data.data || [];
  const isEmpty = rows.length === 0;

  return (
    <div>
      <div className="row" style={{ marginBottom: 16, gap: 12, flexWrap: 'wrap' }}>
        <Headline label="Brier" value={fmtNum(data.brier, 4)}
                  hint="Lower = better. 0 = perfect, 0.25 = always-50%-blind." />
        <Headline label="ECE" value={fmtPct(data.ece)}
                  hint="Expected calibration error. Population-weighted |predicted − actual|." />
        <Headline label="Samples" value={data.sample_size?.toLocaleString() ?? '—'}
                  hint="Decisions with both win_probability and outcome." />
        <Headline label="Labels graded" value={fmtPct(data.label_quality?.fraction_graded)}
                  hint="Share of decisions with outcome attached." />
      </div>

      {isEmpty ? (
        <div className="empty">
          No (probability, outcome) pairs yet. Calibration needs at least one closed trade
          with a stored win_probability.
        </div>
      ) : (
        <div className="row" style={{ gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6,
                                textTransform: 'uppercase', letterSpacing: '0.05em',
                                fontWeight: 600 }}>
              Reliability Diagram
            </div>
            <ReliabilityChart rows={rows} />
            <div style={{ marginTop: 6, fontSize: 10, color: 'var(--muted-2)' }}>
              Diagonal = perfect. Points above diagonal: model is under-confident (predicts
              less than actually happens). Below: over-confident.
            </div>
          </div>

          <div style={{ flex: 1, minWidth: 280 }}>
            <SampleStrip rows={rows} />

            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 10, color: 'var(--muted)', marginBottom: 4,
                                  textTransform: 'uppercase', letterSpacing: '0.05em',
                                  fontWeight: 600 }}>Per-bin breakdown</div>
              <table style={{ width: '100%', fontSize: 11,
                                  fontFeatureSettings: '"tnum"',
                                  borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ color: 'var(--muted)' }}>
                    <th style={{ textAlign: 'left', padding: '4px 6px' }}>Range</th>
                    <th style={{ textAlign: 'right', padding: '4px 6px' }}>n</th>
                    <th style={{ textAlign: 'right', padding: '4px 6px' }}>Pred</th>
                    <th style={{ textAlign: 'right', padding: '4px 6px' }}>Actual</th>
                    <th style={{ textAlign: 'right', padding: '4px 6px' }}>Δ</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const delta = r.actual - r.predicted;
                    return (
                      <tr key={r.bin} style={{ borderTop: '1px solid var(--border)' }}>
                        <td style={{ padding: '4px 6px' }}>
                          {fmtPct(r.lower)}–{fmtPct(r.upper)}
                        </td>
                        <td style={{ padding: '4px 6px', textAlign: 'right' }}>{r.count}</td>
                        <td style={{ padding: '4px 6px', textAlign: 'right' }}>{fmtPct(r.predicted)}</td>
                        <td style={{ padding: '4px 6px', textAlign: 'right' }}>{fmtPct(r.actual)}</td>
                        <td style={{
                          padding: '4px 6px', textAlign: 'right',
                          color: Math.abs(delta) > 0.1 ? 'var(--danger)' : 'var(--text-soft)',
                        }}>
                          {delta >= 0 ? '+' : ''}{(delta * 100).toFixed(1)}%
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="row" style={{ marginTop: 12, alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 11, color: 'var(--muted)' }}>Bins</span>
              {[5, 10, 15, 20].map((n) => (
                <button key={n} className={`btn small ${n === nBins ? 'primary' : ''}`}
                        onClick={() => setNBins(n)}>{n}</button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


function Headline({ label, value, hint }) {
  return (
    <div className="panel" style={{ padding: '10px 14px', minWidth: 150, flex: 1 }}>
      <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase',
                       letterSpacing: '0.05em', fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2,
                       fontFeatureSettings: '"tnum"' }}>{value}</div>
      {hint && <div style={{ fontSize: 10, color: 'var(--muted-2)', marginTop: 4 }}>{hint}</div>}
    </div>
  );
}
