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
 * Pure SVG; relies on the recharts dependency only via DOM-level area
 * primitives would over-complicate the dotted spot line + zone fills.
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

  return (
    <div className="v2-gex-profile" style={{ width: '100%' }}>
      <svg viewBox={`0 0 ${VW} ${VH}`}
           preserveAspectRatio="none"
           width="100%" height={height}
           style={{ display: 'block' }}>

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

        {/* X-axis ticks */}
        {xTicks.map((t, i) => (
          <g key={`xt-${i}`}>
            <line x1={xFor(t)} y1={PAD.top + innerH}
                  x2={xFor(t)} y2={PAD.top + innerH + 4}
                  stroke="var(--border-default)" />
            <text x={xFor(t)} y={PAD.top + innerH + 16}
                  fill="var(--text-tertiary)" fontSize="10"
                  fontFamily="var(--font-mono)" textAnchor="middle">
              {fmtStrike(t)}
            </text>
          </g>
        ))}

        {/* Y-axis labels */}
        <text x={PAD.left - 8} y={PAD.top + 10}
              fill="var(--accent-green)" fontSize="10"
              fontFamily="var(--font-mono)" textAnchor="end" fontWeight="700">
          +{fmtBig(yMax)}
        </text>
        <text x={PAD.left - 8} y={yZero + 4}
              fill="var(--text-tertiary)" fontSize="10"
              fontFamily="var(--font-mono)" textAnchor="end">
          0
        </text>
        <text x={PAD.left - 8} y={PAD.top + innerH - 2}
              fill="var(--accent-red)" fontSize="10"
              fontFamily="var(--font-mono)" textAnchor="end" fontWeight="700">
          -{fmtBig(yMax)}
        </text>

        {/* Zone annotations */}
        <text x={PAD.left + 8} y={PAD.top + 16}
              fill="var(--accent-red)" opacity={0.65}
              fontSize="11" fontWeight="800"
              letterSpacing="0.1em"
              fontFamily="var(--font-display)">
          BEARISH ZONE
        </text>
        <text x={PAD.left + innerW - 8} y={PAD.top + 16}
              fill="var(--accent-green)" opacity={0.65}
              fontSize="11" fontWeight="800"
              letterSpacing="0.1em"
              textAnchor="end"
              fontFamily="var(--font-display)">
          BULLISH ZONE
        </text>

        {/* Vertical markers */}
        {[
          { v: putWall,   color: 'var(--accent-red)',    label: 'PUT WALL' },
          { v: gammaFlip, color: 'var(--accent-purple)', label: 'γ FLIP' },
          { v: callWall,  color: 'var(--accent-green)',  label: 'CALL WALL' },
        ].filter((m) => m.v && m.v >= minS && m.v <= maxS).map((m, i) => (
          <g key={`m-${i}`}>
            <line x1={xFor(m.v)} y1={PAD.top}
                  x2={xFor(m.v)} y2={PAD.top + innerH}
                  stroke={m.color}
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  opacity={0.6} />
            <text x={xFor(m.v)} y={PAD.top - 8}
                  fill={m.color} fontSize="9"
                  fontFamily="var(--font-mono)" fontWeight="700"
                  textAnchor="middle">
              {m.label}
            </text>
          </g>
        ))}

        {/* Spot dotted line (last, on top). */}
        {spotPrice != null && spotPrice >= minS && spotPrice <= maxS && (
          <g>
            <line x1={xFor(spotPrice)} y1={PAD.top}
                  x2={xFor(spotPrice)} y2={PAD.top + innerH}
                  stroke="var(--accent-yellow)"
                  strokeWidth={1.6}
                  strokeDasharray="6 4" />
            <rect x={xFor(spotPrice) - 30} y={PAD.top + innerH + 4}
                  width={60} height={16}
                  rx={2} ry={2}
                  fill="var(--accent-yellow)" />
            <text x={xFor(spotPrice)} y={PAD.top + innerH + 16}
                  fill="var(--bg-primary)" fontSize="10"
                  fontFamily="var(--font-mono)" fontWeight="800"
                  textAnchor="middle">
              SPOT {fmtStrike(spotPrice)}
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}
