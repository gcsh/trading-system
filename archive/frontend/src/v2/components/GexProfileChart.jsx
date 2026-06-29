/* MITS Phase 19 Stream 2 — Aggregated GEX Profile area chart.
 *
 * Horizontal layout (replicates the operator reference bottom panel):
 *   - X axis = strike prices (left to right)
 *   - Y axis = cumulative Net GEX in USD
 *   - Area above 0 → bullish (green fill)
 *   - Area below 0 → bearish (red fill)
 *   - Dotted vertical line at current spot price (labeled SPOT)
 *   - Optional vertical markers at call_wall / put_wall / gamma_flip
 *   - Annotations: BULLISH ZONE on right side, BEARISH ZONE on left
 *
 * Shapes are SVG with `preserveAspectRatio="none"` so bars/zones
 * stretch to fill the container. Every text label is rendered as an
 * absolutely-positioned HTML <div> on top of the SVG, otherwise the
 * SVG stretch warps the glyphs into wide letter-spacing ("7 9 4"
 * instead of "794"). Same approach as GexByStrikeChart.jsx.
 *
 * Props:
 *   strikes      [{ strike, net_gex }] — at minimum
 *   spotPrice    number | null
 *   callWall     number | null
 *   putWall      number | null
 *   gammaFlip    number | null
 *   height       number — default 240
 */
import React, { useMemo } from 'react';

function fmtStrike(s) {
  if (s == null || !isFinite(s)) return '—';
  return Number(s).toLocaleString(undefined, { maximumFractionDigits: 0 });
}
function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  const sign = n < 0 ? '-' : '';
  if (x >= 1e9) return `${sign}${(x / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${sign}${(x / 1e6).toFixed(1)}M`;
  if (x >= 1e3) return `${sign}${(x / 1e3).toFixed(1)}K`;
  return `${sign}${x.toFixed(0)}`;
}

function aggregateByStrike(rows) {
  if (!Array.isArray(rows) || !rows.length) return [];
  const map = new Map();
  for (const r of rows) {
    const k = Number(r.strike);
    if (!isFinite(k)) continue;
    const cur = map.get(k) || { strike: k, net_gex: 0 };
    cur.net_gex += Number(r.net_gex) || 0;
    map.set(k, cur);
  }
  return Array.from(map.values()).sort((a, b) => a.strike - b.strike);
}

export default function GexProfileChart({
  strikes = [],
  spotPrice = null,
  callWall = null,
  putWall = null,
  gammaFlip = null,
  height = 240,
  strikeWindowPct = 0.10,
}) {
  const agg = useMemo(() => aggregateByStrike(strikes), [strikes]);

  // Filter to readable window around spot.
  const clipped = useMemo(() => {
    if (!agg.length) return [];
    if (!spotPrice || !isFinite(spotPrice)) return agg;
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
        ∅ no aggregate GEX profile data
      </div>
    );
  }

  const PAD = { top: 24, right: 24, bottom: 34, left: 70 };
  const VW = 1200;
  const VH = height;
  const innerW = VW - PAD.left - PAD.right;
  const innerH = VH - PAD.top - PAD.bottom;

  const minS = clipped[0].strike;
  const maxS = clipped[clipped.length - 1].strike;
  const spanS = (maxS - minS) || 1;

  const yMax = Math.max(...clipped.map((r) => Math.abs(r.net_gex)), 1);

  const xFor = (strike) => PAD.left + ((strike - minS) / spanS) * innerW;
  const yZero = PAD.top + innerH / 2;
  const yFor = (v) => yZero - (v / yMax) * (innerH / 2);
  const pctX = (x) => `${(x / VW) * 100}%`;

  // Build two overlapping area paths — one clipped to positive half,
  // one to negative half.
  const baselineY = yZero;
  const pointsTopHalf = clipped.map((r) => {
    const v = Math.max(0, r.net_gex);
    return [xFor(r.strike), yFor(v)];
  });
  const pointsBotHalf = clipped.map((r) => {
    const v = Math.min(0, r.net_gex);
    return [xFor(r.strike), yFor(v)];
  });
  const areaPath = (pts) => {
    if (!pts.length) return '';
    let p = `M ${pts[0][0].toFixed(1)} ${baselineY.toFixed(1)} L `;
    p += pts.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L ');
    p += ` L ${pts[pts.length - 1][0].toFixed(1)} ${baselineY.toFixed(1)} Z`;
    return p;
  };
  const linePath = (pts) => {
    if (!pts.length) return '';
    return pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
  };

  const netLinePts = clipped.map((r) => [xFor(r.strike), yFor(r.net_gex)]);

  // X tick labels — ~8 ticks.
  const tickCount = 8;
  const xTicks = Array.from({ length: tickCount }, (_, i) =>
    minS + (i * spanS) / (tickCount - 1));

  // Vertical markers (put_wall, gamma_flip, call_wall) — filtered to
  // ones inside the window. Used for both SVG indicator lines and
  // HTML labels.
  const markers = [
    { v: putWall,   color: 'var(--accent-red)',    label: 'PUT WALL' },
    { v: gammaFlip, color: 'var(--accent-purple)', label: 'γ FLIP' },
    { v: callWall,  color: 'var(--accent-green)',  label: 'CALL WALL' },
  ].filter((m) => m.v && m.v >= minS && m.v <= maxS);

  // Horizontal collision avoidance for marker labels at the top of
  // the chart. Top labels at ~16px width each; if two are within
  // ~7% of the inner width (~80px @ 1200vw → noticeable mash), stack
  // the latter one higher.
  const labelGap = 0.07 * innerW;
  const markerLabels = markers
    .map((m) => ({ ...m, x: xFor(m.v), row: 0 }))
    .sort((a, b) => a.x - b.x);
  for (let i = 1; i < markerLabels.length; i++) {
    if (markerLabels[i].x - markerLabels[i - 1].x < labelGap) {
      markerLabels[i].row = markerLabels[i - 1].row + 1;
    }
  }

  return (
    <div className="v2-gex-profile"
         style={{ width: '100%', position: 'relative', height }}>
      {/* Shapes only — bars, zones, polyline, marker lines, spot
          line. ALL TEXT labels live in HTML overlays below the SVG
          to avoid `preserveAspectRatio="none"` horizontal letter
          stretching. */}
      <svg viewBox={`0 0 ${VW} ${VH}`}
           preserveAspectRatio="none"
           width="100%" height={height}
           style={{ display: 'block', position: 'absolute', inset: 0 }}>

        {/* Zone background tints */}
        {spotPrice != null && spotPrice >= minS && spotPrice <= maxS && (
          <>
            <rect x={xFor(spotPrice)} y={PAD.top}
                  width={PAD.left + innerW - xFor(spotPrice)} height={innerH}
                  fill="var(--accent-green)" opacity={0.04} />
            <rect x={PAD.left} y={PAD.top}
                  width={xFor(spotPrice) - PAD.left} height={innerH}
                  fill="var(--accent-red)" opacity={0.04} />
          </>
        )}

        {/* Zero line */}
        <line x1={PAD.left} y1={yZero}
              x2={PAD.left + innerW} y2={yZero}
              stroke="var(--border-default)" strokeWidth={1} opacity={0.7} />

        {/* Area fills */}
        <path d={areaPath(pointsTopHalf)}
              fill="var(--accent-green)" opacity={0.35} />
        <path d={areaPath(pointsBotHalf)}
              fill="var(--accent-red)" opacity={0.35} />

        {/* Net GEX outline */}
        <path d={linePath(netLinePts)}
              fill="none"
              stroke="rgba(241, 245, 249, 0.95)"
              strokeWidth={1.4} />

        {/* X-axis tick marks (lines only — labels handled by HTML). */}
        {xTicks.map((t, i) => (
          <line key={`xt-${i}`}
                x1={xFor(t)} y1={PAD.top + innerH}
                x2={xFor(t)} y2={PAD.top + innerH + 4}
                stroke="var(--border-default)" />
        ))}

        {/* Vertical marker indicator lines. Labels handled by HTML. */}
        {markers.map((m, i) => (
          <line key={`mk-line-${i}`}
                x1={xFor(m.v)} y1={PAD.top}
                x2={xFor(m.v)} y2={PAD.top + innerH}
                stroke={m.color}
                strokeWidth={1}
                strokeDasharray="3 3"
                opacity={0.6} />
        ))}

        {/* Spot dotted line — labeled badge handled by HTML. */}
        {spotPrice != null && spotPrice >= minS && spotPrice <= maxS && (
          <line x1={xFor(spotPrice)} y1={PAD.top}
                x2={xFor(spotPrice)} y2={PAD.top + innerH}
                stroke="var(--accent-yellow)"
                strokeWidth={1.6}
                strokeDasharray="6 4" />
        )}
      </svg>

      {/* ─── HTML LABEL OVERLAYS (no font stretching) ─── */}

      {/* Y-axis amplitude labels */}
      <div className="v2-gex-profile__ylabel v2-gex-profile__ylabel--top"
           style={{ left: 0, width: pctX(PAD.left - 6),
                    top: PAD.top - 3 }}>
        +{fmtBig(yMax)}
      </div>
      <div className="v2-gex-profile__ylabel v2-gex-profile__ylabel--zero"
           style={{ left: 0, width: pctX(PAD.left - 6),
                    top: yZero - 5 }}>
        0
      </div>
      <div className="v2-gex-profile__ylabel v2-gex-profile__ylabel--bot"
           style={{ left: 0, width: pctX(PAD.left - 6),
                    top: PAD.top + innerH - 8 }}>
        −{fmtBig(yMax)}
      </div>

      {/* Zone annotations */}
      <div className="v2-gex-profile__zone v2-gex-profile__zone--bearish"
           style={{ left: pctX(PAD.left + 8), top: PAD.top + 4 }}>
        BEARISH ZONE
      </div>
      <div className="v2-gex-profile__zone v2-gex-profile__zone--bullish"
           style={{ left: pctX(PAD.left + innerW - 8), top: PAD.top + 4 }}>
        BULLISH ZONE
      </div>

      {/* X-axis tick labels */}
      {xTicks.map((t, i) => (
        <div key={`xt-lbl-${i}`}
             className="v2-gex-profile__xlabel"
             style={{ left: pctX(xFor(t)), top: PAD.top + innerH + 8 }}>
          {fmtStrike(t)}
        </div>
      ))}

      {/* Marker labels (PUT WALL, γ FLIP, CALL WALL) with vertical
          stacking when labels collide horizontally. */}
      {markerLabels.map((m, i) => (
        <div key={`mk-lbl-${i}`}
             className="v2-gex-profile__marker"
             style={{
               left: pctX(m.x),
               top: PAD.top - 16 - (m.row * 12),
               color: m.color,
             }}>
          {m.label}
        </div>
      ))}

      {/* Spot badge — yellow, below the chart. */}
      {spotPrice != null && spotPrice >= minS && spotPrice <= maxS && (
        <div className="v2-gex-profile__spot"
             style={{ left: pctX(xFor(spotPrice)),
                      top: PAD.top + innerH + 6 }}>
          SPOT {fmtStrike(spotPrice)}
        </div>
      )}

      <style>{`
        .v2-gex-profile { font-family: var(--font-mono); }
        .v2-gex-profile > div {
          position: absolute;
          pointer-events: none;
          white-space: nowrap;
          font-family: var(--font-mono);
          letter-spacing: 0;
          line-height: 1;
        }
        .v2-gex-profile__ylabel {
          font-size: 10px;
          text-align: right;
          padding-right: 4px;
          font-weight: 700;
        }
        .v2-gex-profile__ylabel--top { color: var(--accent-green); }
        .v2-gex-profile__ylabel--zero { color: var(--text-tertiary); font-weight: 400; }
        .v2-gex-profile__ylabel--bot { color: var(--accent-red); }

        .v2-gex-profile__zone {
          font-family: var(--font-display);
          font-size: 11px;
          font-weight: 800;
          letter-spacing: 0.1em;
          opacity: 0.65;
        }
        .v2-gex-profile__zone--bearish { color: var(--accent-red); }
        .v2-gex-profile__zone--bullish {
          color: var(--accent-green);
          transform: translateX(-100%);
        }

        .v2-gex-profile__xlabel {
          font-size: 10px;
          color: var(--text-tertiary);
          transform: translateX(-50%);
        }
        .v2-gex-profile__marker {
          font-size: 9px;
          font-weight: 700;
          transform: translateX(-50%);
        }
        .v2-gex-profile__spot {
          font-size: 10px;
          font-weight: 800;
          color: var(--bg-primary);
          background: var(--accent-yellow);
          padding: 2px 6px;
          border-radius: 3px;
          transform: translateX(-50%);
          line-height: 14px;
        }
      `}</style>
    </div>
  );
}
