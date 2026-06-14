/* MITS Phase 19 Stream 2 — GEX-by-strike bidirectional bar chart.
 *
 * Horizontal layout (replicates the operator reference):
 *   - Y axis = strike prices, ascending
 *   - X axis = GEX in USD, centered at 0
 *   - Call GEX bars extend RIGHT (green)
 *   - Put  GEX bars extend LEFT  (red)
 *   - White polyline overlay = Net GEX per strike (lerp to bar midpoints)
 *   - Dotted yellow horizontal line at spot price (label)
 *   - Markers: GEX MAX (CALL), GEX MAX (PUT), GEX WALL
 *
 * Pure SVG so we can place precise labels + dotted spot line without
 * fighting recharts. Honors the dark/neon palette via CSS vars.
 *
 * Props:
 *   strikes          [{ strike, call_gex, put_gex, net_gex }]
 *   spotPrice        number — current underlying price (dotted line)
 *   callWall         number | null — annotate as "GEX WALL"
 *   putWall          number | null — annotate as "PUT WALL"
 *   gammaFlip        number | null — annotate as "FLIP"
 *   maxGammaStrike   number | null — annotate as "GEX MAX"
 *   height           number — chart height in px (default 560)
 *   strikeWindowPct  number — clip strikes to ±N% of spot to keep the
 *                              chart readable (default 0.08 = 8%)
 */
import React, { useMemo } from 'react';

function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  const sign = n < 0 ? '-' : '';
  if (x >= 1e9) return `${sign}${(x / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${sign}${(x / 1e6).toFixed(1)}M`;
  if (x >= 1e3) return `${sign}${(x / 1e3).toFixed(1)}K`;
  return `${sign}${x.toFixed(0)}`;
}

function fmtStrike(s) {
  if (s == null || !isFinite(s)) return '—';
  return Number(s).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/**
 * Aggregate per-expiry rows down to one row per strike (sum gex).
 */
function aggregateByStrike(rows) {
  if (!Array.isArray(rows) || !rows.length) return [];
  const map = new Map();
  for (const r of rows) {
    const k = Number(r.strike);
    if (!isFinite(k)) continue;
    const cur = map.get(k) || {
      strike: k, call_gex: 0, put_gex: 0, net_gex: 0,
      call_oi: 0, put_oi: 0, total_oi: 0,
    };
    cur.call_gex += Number(r.call_gex) || 0;
    cur.put_gex  += Number(r.put_gex)  || 0;
    cur.net_gex  += Number(r.net_gex)  || 0;
    cur.call_oi  += Number(r.call_oi)  || 0;
    cur.put_oi   += Number(r.put_oi)   || 0;
    cur.total_oi += Number(r.total_oi) || 0;
    map.set(k, cur);
  }
  return Array.from(map.values()).sort((a, b) => a.strike - b.strike);
}

export default function GexByStrikeChart({
  strikes = [],
  spotPrice = null,
  callWall = null,
  putWall = null,
  gammaFlip = null,
  maxGammaStrike = null,
  height = 560,
  strikeWindowPct = 0.08,
}) {
  const agg = useMemo(() => aggregateByStrike(strikes), [strikes]);

  // Restrict to ±strikeWindowPct around spot so we don't squash bars
  // into a 1-pixel wide stripe (398 strikes is too dense to plot raw).
  const clipped = useMemo(() => {
    if (!agg.length) return [];
    if (!spotPrice || !isFinite(spotPrice)) {
      // No spot — keep all strikes that actually have data.
      return agg.filter((r) => Math.abs(r.call_gex) > 0 || Math.abs(r.put_gex) > 0);
    }
    const lo = spotPrice * (1 - strikeWindowPct);
    const hi = spotPrice * (1 + strikeWindowPct);
    return agg.filter((r) => r.strike >= lo && r.strike <= hi);
  }, [agg, spotPrice, strikeWindowPct]);

  if (!clipped.length) {
    return (
      <div style={{
        background: 'var(--bg-secondary)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--radius-md)',
        padding: 'var(--space-6)',
        textAlign: 'center',
        color: 'var(--text-tertiary)',
        height,
      }}>
        ∅ no strike-level GEX data in current view
      </div>
    );
  }

  // Layout.
  const PAD = { top: 28, right: 90, bottom: 32, left: 90 };
  const VW = 1000;          // viewBox width (responsive via preserveAspectRatio)
  const VH = height;
  const innerW = VW - PAD.left - PAD.right;
  const innerH = VH - PAD.top - PAD.bottom;

  // X scale: max abs GEX (call or put).
  const xMax = Math.max(
    ...clipped.map((r) => Math.max(Math.abs(r.call_gex), Math.abs(r.put_gex))),
    1,
  );
  const xCenter = PAD.left + innerW / 2;
  const xScale = innerW / 2 / xMax;
  const x0 = xCenter;
  const xFor = (v) => x0 + v * xScale;

  // Y scale: strikes mapped top→bottom but with low strike at bottom.
  const minStrike = clipped[0].strike;
  const maxStrike = clipped[clipped.length - 1].strike;
  const strikeSpan = (maxStrike - minStrike) || 1;
  // Each strike gets one band; equal-spaced rows for readability.
  const bandH = innerH / clipped.length;
  const yFor = (strike) => {
    const idx = clipped.findIndex((r) => r.strike === strike);
    if (idx < 0) return PAD.top + innerH / 2;
    // Render lowest strike at BOTTOM (idx 0 → bottom).
    return PAD.top + innerH - (idx + 0.5) * bandH;
  };

  // Reasonable Y-axis label density (every Nth strike).
  const labelEvery = Math.max(1, Math.ceil(clipped.length / 14));
  const labelStrikes = clipped.filter((_, i) => i % labelEvery === 0);

  // Net-GEX polyline points.
  const netLinePoints = clipped.map((r) => `${xFor(r.net_gex).toFixed(1)},${yFor(r.strike).toFixed(1)}`).join(' ');

  // Spot price → Y line (interpolated between bands).
  const spotY = (() => {
    if (!spotPrice || !isFinite(spotPrice)) return null;
    if (spotPrice <= minStrike) return PAD.top + innerH;
    if (spotPrice >= maxStrike) return PAD.top;
    // Interpolate based on strike position (use real strike value rather
    // than band index for visual accuracy).
    const frac = (spotPrice - minStrike) / strikeSpan;
    return PAD.top + innerH - frac * innerH;
  })();

  // X-axis ticks at -xMax, -xMax/2, 0, +xMax/2, +xMax.
  const xTicks = [-xMax, -xMax / 2, 0, xMax / 2, xMax];

  // Bar geometry: thin bars centered on each strike band.
  const barH = Math.max(2, bandH * 0.6);

  // Identify GEX MAX (largest |net_gex| among rows).
  const maxAbsRow = clipped.reduce((acc, r) => (
    Math.abs(r.net_gex) > Math.abs(acc?.net_gex || 0) ? r : acc
  ), null);

  // Marker rows: call wall, put wall, gamma flip, max gamma strike.
  const markers = [];
  if (callWall && callWall >= minStrike && callWall <= maxStrike) {
    markers.push({ y: PAD.top + innerH - ((callWall - minStrike) / strikeSpan) * innerH,
      label: 'CALL WALL', color: 'var(--accent-green)', strike: callWall });
  }
  if (putWall && putWall >= minStrike && putWall <= maxStrike) {
    markers.push({ y: PAD.top + innerH - ((putWall - minStrike) / strikeSpan) * innerH,
      label: 'PUT WALL', color: 'var(--accent-red)', strike: putWall });
  }
  if (gammaFlip && gammaFlip >= minStrike && gammaFlip <= maxStrike) {
    markers.push({ y: PAD.top + innerH - ((gammaFlip - minStrike) / strikeSpan) * innerH,
      label: 'γ FLIP', color: 'var(--accent-purple)', strike: gammaFlip });
  }
  if (maxGammaStrike && maxGammaStrike >= minStrike && maxGammaStrike <= maxStrike) {
    markers.push({ y: PAD.top + innerH - ((maxGammaStrike - minStrike) / strikeSpan) * innerH,
      label: 'γ MAX', color: 'var(--accent-yellow)', strike: maxGammaStrike });
  }

  return (
    <div className="v2-gex-strikechart" style={{ width: '100%' }}>
      {/* This chart is fundamentally a horizontal-bar layout — bars
          extend left/right from a center axis. Stretching the SVG
          horizontally (`preserveAspectRatio="none"`) makes the bars
          longer, which is correct: bar length encodes GEX magnitude.
          The previous attempt to use `xMidYMid meet` shrunk the bars
          into a narrow center band with huge empty side margins. */}
      <svg viewBox={`0 0 ${VW} ${VH}`}
           preserveAspectRatio="none"
           width="100%" height={height}
           style={{ display: 'block' }}>
        {/* Backdrop grid lines (vertical, at each x-tick). */}
        {xTicks.map((t, i) => (
          <line key={`vg-${i}`}
                x1={xFor(t)} y1={PAD.top}
                x2={xFor(t)} y2={PAD.top + innerH}
                stroke="var(--border-subtle)"
                strokeWidth={t === 0 ? 1.4 : 0.6}
                strokeDasharray={t === 0 ? null : '2 4'}
                opacity={t === 0 ? 0.7 : 0.5} />
        ))}

        {/* Center axis label */}
        <text x={x0} y={PAD.top - 10}
              fill="var(--text-tertiary)" fontSize="10"
              fontFamily="var(--font-mono)" textAnchor="middle">
          0
        </text>

        {/* X-axis numeric ticks (top) */}
        {xTicks.map((t, i) => (
          t === 0 ? null : (
            <text key={`xt-${i}`}
                  x={xFor(t)} y={PAD.top + innerH + 18}
                  fill="var(--text-tertiary)" fontSize="9"
                  fontFamily="var(--font-mono)" textAnchor="middle">
              {fmtBig(t)}
            </text>
          )
        ))}
        <text x={PAD.left + 4} y={PAD.top + innerH + 18}
              fill="var(--text-tertiary)" fontSize="9"
              fontFamily="var(--font-mono)" textAnchor="start">
          PUT ←
        </text>
        <text x={PAD.left + innerW - 4} y={PAD.top + innerH + 18}
              fill="var(--text-tertiary)" fontSize="9"
              fontFamily="var(--font-mono)" textAnchor="end">
          → CALL
        </text>

        {/* Put GEX bars (extend LEFT from center → red) */}
        {clipped.map((r) => {
          const yC = yFor(r.strike);
          const w = Math.abs(r.put_gex) * xScale;
          if (w < 0.5) return null;
          return (
            <rect key={`p-${r.strike}`}
                  x={x0 - w} y={yC - barH / 2}
                  width={w} height={barH}
                  fill="var(--accent-red)" opacity={0.7}>
              <title>strike {r.strike} · put GEX {fmtBig(r.put_gex)}</title>
            </rect>
          );
        })}

        {/* Call GEX bars (extend RIGHT from center → green) */}
        {clipped.map((r) => {
          const yC = yFor(r.strike);
          const w = Math.abs(r.call_gex) * xScale;
          if (w < 0.5) return null;
          return (
            <rect key={`c-${r.strike}`}
                  x={x0} y={yC - barH / 2}
                  width={w} height={barH}
                  fill="var(--accent-green)" opacity={0.7}>
              <title>strike {r.strike} · call GEX {fmtBig(r.call_gex)}</title>
            </rect>
          );
        })}

        {/* Net GEX polyline overlay (white). */}
        {clipped.length > 1 && (
          <polyline points={netLinePoints}
                    fill="none"
                    stroke="rgba(241, 245, 249, 0.85)"
                    strokeWidth={1.4}
                    strokeLinejoin="round"
                    strokeLinecap="round" />
        )}

        {/* Y-axis strike labels (left side). */}
        {labelStrikes.map((r) => (
          <text key={`yl-${r.strike}`}
                x={PAD.left - 8} y={yFor(r.strike) + 3}
                fill="var(--text-tertiary)" fontSize="10"
                fontFamily="var(--font-mono)" textAnchor="end">
            {fmtStrike(r.strike)}
          </text>
        ))}

        {/* Markers — horizontal lines + label badges on the right. */}
        {markers.map((m, i) => (
          <g key={`mk-${i}`}>
            <line x1={PAD.left} y1={m.y}
                  x2={PAD.left + innerW} y2={m.y}
                  stroke={m.color} strokeWidth={0.7}
                  strokeDasharray="3 3" opacity={0.5} />
            <rect x={VW - PAD.right + 4} y={m.y - 7}
                  width={PAD.right - 8} height={14}
                  rx={2} ry={2}
                  fill="var(--bg-elevated)"
                  stroke={m.color} strokeWidth={0.7} />
            <text x={VW - PAD.right / 2} y={m.y + 3}
                  fill={m.color} fontSize="8"
                  fontFamily="var(--font-mono)"
                  fontWeight="700" textAnchor="middle">
              {m.label} {fmtStrike(m.strike)}
            </text>
          </g>
        ))}

        {/* Spot-price dotted line (yellow). */}
        {spotY != null && (
          <g>
            <line x1={PAD.left} y1={spotY}
                  x2={PAD.left + innerW} y2={spotY}
                  stroke="var(--accent-yellow)"
                  strokeWidth={1.5}
                  strokeDasharray="6 4" />
            <rect x={PAD.left + innerW + 4} y={spotY - 8}
                  width={PAD.right - 8} height={16}
                  rx={2} ry={2}
                  fill="var(--accent-yellow)" opacity={0.95} />
            <text x={VW - PAD.right / 2} y={spotY + 3}
                  fill="var(--bg-primary)" fontSize="9"
                  fontWeight="800" fontFamily="var(--font-mono)"
                  textAnchor="middle">
              SPOT {fmtStrike(spotPrice)}
            </text>
          </g>
        )}

        {/* GEX MAX badge on the largest net_gex strike. */}
        {maxAbsRow && (
          <g>
            <circle cx={xFor(maxAbsRow.net_gex)} cy={yFor(maxAbsRow.strike)}
                    r={4}
                    fill="var(--accent-cyan)"
                    stroke="var(--bg-primary)" strokeWidth={1.5} />
          </g>
        )}
      </svg>

      {/* Legend strip beneath the chart. */}
      <div className="v2-gex-strikechart__legend">
        <span><span className="v2-gex-sw" style={{ background: 'var(--accent-green)' }} /> Call GEX</span>
        <span><span className="v2-gex-sw" style={{ background: 'var(--accent-red)' }} /> Put GEX</span>
        <span><span className="v2-gex-sw" style={{ background: 'rgba(241,245,249,0.85)', height: 2 }} /> Net GEX</span>
        <span><span className="v2-gex-sw v2-gex-sw--dashed" style={{ borderColor: 'var(--accent-yellow)' }} /> Spot</span>
      </div>

      <style>{`
        .v2-gex-strikechart__legend {
          display: flex;
          gap: 16px;
          padding: 8px 12px 4px;
          font-size: 11px;
          font-family: var(--font-mono);
          color: var(--text-tertiary);
          flex-wrap: wrap;
        }
        .v2-gex-strikechart__legend span {
          display: inline-flex; align-items: center; gap: 6px;
        }
        .v2-gex-sw {
          display: inline-block;
          width: 14px; height: 8px;
          border-radius: 1px;
        }
        .v2-gex-sw--dashed {
          height: 0;
          border-top: 2px dashed;
          width: 14px;
        }
      `}</style>
    </div>
  );
}
