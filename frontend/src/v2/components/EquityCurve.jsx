/* MITS Phase 19 Cluster D — EquityCurve component.
 *
 * Renders a single-series line chart of portfolio equity over time with a
 * shaded drawdown band underneath. Pure SVG, no chart library — matches
 * the design-system aesthetic.
 *
 * Props:
 *   data:        array of { timestamp, portfolio_value } from /portfolio/equity
 *   height:      svg height (default 280)
 *   stroke:      line colour (default var(--accent-cyan))
 *   showDrawdown:overlay drawdown shading (default true)
 *
 * Renders an EmptyState message if data is missing or < 2 points.
 */
import React, { useMemo } from 'react';
import { EmptyState } from '../../design/Components.jsx';

function fmtMoney(v) {
  if (v == null || !isFinite(v)) return '—';
  return `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 0, maximumFractionDigits: 0,
  })}`;
}
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export default function EquityCurve({
  data = [],
  height = 280,
  stroke = 'var(--accent-cyan)',
  showDrawdown = true,
}) {
  const points = useMemo(() => {
    if (!Array.isArray(data) || data.length < 2) return [];
    return data
      .map(d => ({
        t: d.timestamp || d.t || d.date,
        v: Number(d.portfolio_value ?? d.equity ?? d.value),
      }))
      .filter(p => p.t && isFinite(p.v))
      .sort((a, b) => Date.parse(a.t) - Date.parse(b.t));
  }, [data]);

  if (points.length < 2) {
    return (
      <EmptyState
        icon="📈"
        message="Not enough equity snapshots yet — need at least 2 points."
      />
    );
  }

  // Compute drawdown series (peak-to-trough %).
  const ddSeries = [];
  let peak = points[0].v;
  for (const p of points) {
    if (p.v > peak) peak = p.v;
    const dd = peak > 0 ? (p.v - peak) / peak : 0;
    ddSeries.push(dd);
  }
  const maxDD = Math.min(...ddSeries);

  const W = 1000; // viewBox width (scales)
  const H = height;
  const padT = 16, padB = 36, padL = 64, padR = 16;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const vMin = Math.min(...points.map(p => p.v));
  const vMax = Math.max(...points.map(p => p.v));
  const vRange = vMax - vMin || 1;

  const xs = points.map((_, i) => padL + (i / (points.length - 1)) * innerW);
  const ys = points.map(p => padT + (1 - (p.v - vMin) / vRange) * innerH);

  const linePath = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xs[i].toFixed(1)} ${ys[i].toFixed(1)}`)
    .join(' ');
  const fillPath = `${linePath} L ${xs[xs.length - 1].toFixed(1)} ${padT + innerH} L ${xs[0].toFixed(1)} ${padT + innerH} Z`;

  // Drawdown overlay — shows depth of underwater periods.
  const ddYs = ddSeries.map(dd => {
    // dd is <= 0; map [maxDD..0] → [innerH..0] in a sub-band at the bottom
    const ddBand = innerH * 0.35;
    if (maxDD === 0) return padT + innerH;
    const t = dd / maxDD; // 0..1, 1 = deepest
    return padT + innerH - ddBand + t * ddBand;
  });
  const ddPath = points
    .map((_, i) => `${i === 0 ? 'M' : 'L'} ${xs[i].toFixed(1)} ${ddYs[i].toFixed(1)}`)
    .join(' ');
  const ddFill = `${ddPath} L ${xs[xs.length - 1].toFixed(1)} ${padT + innerH} L ${xs[0].toFixed(1)} ${padT + innerH} Z`;

  // 4 horizontal gridlines + value labels.
  const gridY = [0, 0.25, 0.5, 0.75, 1.0];

  return (
    <div className="v2-eq" style={{ width: '100%' }}>
      <svg viewBox={`0 0 ${W} ${H}`}
           preserveAspectRatio="none"
           style={{ width: '100%', height, display: 'block' }}
           role="img"
           aria-label="Equity curve over time">
        {/* gridlines */}
        {gridY.map((t, i) => {
          const y = padT + t * innerH;
          const v = vMax - t * vRange;
          return (
            <g key={i}>
              <line x1={padL} x2={W - padR} y1={y} y2={y}
                    stroke="var(--border-subtle)" strokeDasharray="2 3" />
              <text x={padL - 6} y={y + 4}
                    fontSize="11"
                    textAnchor="end"
                    fill="var(--text-tertiary)"
                    fontFamily="var(--font-mono)">
                {fmtMoney(v)}
              </text>
            </g>
          );
        })}
        {/* drawdown shading */}
        {showDrawdown && (
          <path d={ddFill} fill="var(--accent-red)" opacity="0.12" />
        )}
        {/* equity fill */}
        <path d={fillPath} fill={stroke} opacity="0.10" />
        {/* equity line */}
        <path d={linePath} fill="none" stroke={stroke} strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" />
        {/* x-axis labels — first / mid / last */}
        {[0, Math.floor(points.length / 2), points.length - 1].map((i, k) => (
          <text key={k}
                x={xs[i]}
                y={H - 10}
                fontSize="11"
                textAnchor="middle"
                fill="var(--text-tertiary)"
                fontFamily="var(--font-mono)">
            {fmtDate(points[i].t)}
          </text>
        ))}
      </svg>
      <div className="v2-eq__legend">
        <span className="v2-eq__legend-item">
          <span className="v2-eq__legend-swatch" style={{ background: stroke }} />
          Equity
        </span>
        {showDrawdown && (
          <span className="v2-eq__legend-item">
            <span className="v2-eq__legend-swatch"
                  style={{ background: 'var(--accent-red)', opacity: 0.5 }} />
            Drawdown {(maxDD * 100).toFixed(1)}%
          </span>
        )}
      </div>
      <style>{`
        .v2-eq__legend {
          display: flex; gap: 16px;
          padding: 6px 0 0;
          font-size: var(--font-size-xs);
          color: var(--text-tertiary);
          font-family: var(--font-mono);
        }
        .v2-eq__legend-item {
          display: inline-flex; align-items: center; gap: 6px;
        }
        .v2-eq__legend-swatch {
          width: 10px; height: 10px; border-radius: 2px;
          display: inline-block;
        }
      `}</style>
    </div>
  );
}
