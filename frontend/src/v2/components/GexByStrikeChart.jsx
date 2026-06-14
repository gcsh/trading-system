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

  // Geometry → CSS-percent helpers. The SVG stretches horizontally
  // (`preserveAspectRatio="none"`) so any x-coord in viewBox units
  // maps to (x / VW) of the container's rendered width. Y is 1:1 in px
  // because VH equals the container's height prop.
  const pctX = (x) => `${(x / VW) * 100}%`;

  return (
    <div className="v2-gex-strikechart"
         style={{ width: '100%', position: 'relative', height }}>
      {/* SHAPES ONLY in SVG — bars, grid, polyline, markers'
          indicator lines, spot dot. ALL TEXT is rendered as HTML
          overlays below. This is required because the SVG
          stretches horizontally (`preserveAspectRatio="none"`); any
          <text> inside would get its glyphs and inter-letter spacing
          stretched too, producing the "7 9 4" wide-letter look the
          operator flagged. HTML labels stay typographically correct
          regardless of how wide the container is. */}
      <svg viewBox={`0 0 ${VW} ${VH}`}
           preserveAspectRatio="none"
           width="100%" height={height}
           style={{ display: 'block', position: 'absolute', inset: 0 }}>
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

        {/* Markers — indicator lines only. Badge + text overlay below. */}
        {markers.map((m, i) => (
          <line key={`mk-line-${i}`}
                x1={PAD.left} y1={m.y}
                x2={PAD.left + innerW} y2={m.y}
                stroke={m.color} strokeWidth={0.7}
                strokeDasharray="3 3" opacity={0.5} />
        ))}

        {/* Spot-price dotted line (yellow). Badge overlay handles the label. */}
        {spotY != null && (
          <line x1={PAD.left} y1={spotY}
                x2={PAD.left + innerW} y2={spotY}
                stroke="var(--accent-yellow)"
                strokeWidth={1.5}
                strokeDasharray="6 4" />
        )}

        {/* GEX MAX dot on the largest net_gex strike. */}
        {maxAbsRow && (
          <circle cx={xFor(maxAbsRow.net_gex)} cy={yFor(maxAbsRow.strike)}
                  r={4}
                  fill="var(--accent-cyan)"
                  stroke="var(--bg-primary)" strokeWidth={1.5} />
        )}
      </svg>

      {/* ─── HTML LABEL OVERLAYS (no font stretching) ─── */}

      {/* Y-axis strike labels (left margin). */}
      {labelStrikes.map((r) => (
        <div key={`yl-${r.strike}`}
             className="v2-gex-strikechart__ylabel"
             style={{
               top: `${yFor(r.strike) - 7}px`,
               width: pctX(PAD.left - 6),
             }}>
          {fmtStrike(r.strike)}
        </div>
      ))}

      {/* Center axis "0" at top. */}
      <div className="v2-gex-strikechart__zero"
           style={{ left: pctX(x0), top: PAD.top - 18 }}>
        0
      </div>

      {/* X-axis numeric ticks (bottom). */}
      {xTicks.map((t, i) => (
        t === 0 ? null : (
          <div key={`xt-${i}`}
               className="v2-gex-strikechart__xlabel"
               style={{ left: pctX(xFor(t)), top: PAD.top + innerH + 8 }}>
            {fmtBig(t)}
          </div>
        )
      ))}
      {/* PUT ← / → CALL hint labels at bottom corners. */}
      <div className="v2-gex-strikechart__hint v2-gex-strikechart__hint--left"
           style={{ left: pctX(PAD.left + 4), top: PAD.top + innerH + 8 }}>
        PUT ←
      </div>
      <div className="v2-gex-strikechart__hint v2-gex-strikechart__hint--right"
           style={{ left: pctX(PAD.left + innerW - 4), top: PAD.top + innerH + 8 }}>
        → CALL
      </div>

      {/* Marker badges (CALL WALL, PUT WALL, γ FLIP, γ MAX). */}
      {markers.map((m, i) => (
        <div key={`mk-badge-${i}`}
             className="v2-gex-strikechart__marker"
             style={{
               left: pctX(VW - PAD.right + 4),
               top: m.y - 9,
               width: pctX(PAD.right - 8),
               color: m.color,
               borderColor: m.color,
             }}>
          {m.label} {fmtStrike(m.strike)}
        </div>
      ))}

      {/* SPOT badge (yellow). */}
      {spotY != null && (
        <div className="v2-gex-strikechart__spot"
             style={{
               left: pctX(PAD.left + innerW + 4),
               top: spotY - 10,
               width: pctX(PAD.right - 8),
             }}>
          SPOT {fmtStrike(spotPrice)}
        </div>
      )}

      {/* Legend strip beneath the chart. */}
      <div className="v2-gex-strikechart__legend">
        <span><span className="v2-gex-sw" style={{ background: 'var(--accent-green)' }} /> Call GEX</span>
        <span><span className="v2-gex-sw" style={{ background: 'var(--accent-red)' }} /> Put GEX</span>
        <span><span className="v2-gex-sw" style={{ background: 'rgba(241,245,249,0.85)', height: 2 }} /> Net GEX</span>
        <span><span className="v2-gex-sw v2-gex-sw--dashed" style={{ borderColor: 'var(--accent-yellow)' }} /> Spot</span>
      </div>

      <style>{`
        .v2-gex-strikechart { font-family: var(--font-mono); }

        /* All overlays inherit the same base styling — small mono, no
           wrap, pointer-events:none so they don't intercept hovers. */
        .v2-gex-strikechart > .v2-gex-strikechart__ylabel,
        .v2-gex-strikechart > .v2-gex-strikechart__xlabel,
        .v2-gex-strikechart > .v2-gex-strikechart__zero,
        .v2-gex-strikechart > .v2-gex-strikechart__hint,
        .v2-gex-strikechart > .v2-gex-strikechart__marker,
        .v2-gex-strikechart > .v2-gex-strikechart__spot {
          position: absolute;
          pointer-events: none;
          white-space: nowrap;
          font-family: var(--font-mono);
          letter-spacing: 0;
          line-height: 1;
        }

        .v2-gex-strikechart__ylabel {
          font-size: 10px;
          color: var(--text-tertiary);
          text-align: right;
          padding-right: 4px;
          left: 0;
        }
        .v2-gex-strikechart__xlabel {
          font-size: 9px;
          color: var(--text-tertiary);
          transform: translateX(-50%);
        }
        .v2-gex-strikechart__zero {
          font-size: 10px;
          color: var(--text-tertiary);
          transform: translateX(-50%);
        }
        .v2-gex-strikechart__hint--left,
        .v2-gex-strikechart__hint--right {
          font-size: 9px;
          color: var(--text-tertiary);
        }
        .v2-gex-strikechart__hint--right { transform: translateX(-100%); }

        .v2-gex-strikechart__marker {
          font-size: 9px;
          font-weight: 700;
          background: var(--bg-elevated);
          border: 1px solid currentColor;
          border-radius: 3px;
          padding: 2px 4px;
          text-align: center;
          height: 18px;
          line-height: 14px;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .v2-gex-strikechart__spot {
          font-size: 10px;
          font-weight: 800;
          color: var(--bg-primary);
          background: var(--accent-yellow);
          border-radius: 3px;
          padding: 2px 4px;
          text-align: center;
          height: 20px;
          line-height: 16px;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        .v2-gex-strikechart__legend {
          position: absolute;
          left: 0; right: 0;
          bottom: 0;
          display: flex;
          gap: 16px;
          padding: 4px 12px;
          font-size: 11px;
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
