/* MITS Phase 19 Stream 2 — GEX exposure by expiry bucket.
 *
 * Buckets the per-strike rows by their `expiry`/`dte` field into
 * 0DTE / 1W / 2W / 3W / 1M / >1M groups and renders side-by-side bars:
 *
 *   - Green bar = sum(call_gex) for that bucket
 *   - Red   bar = sum(put_gex)  for that bucket (absolute value)
 *   - White line overlay = sum(net_gex) per bucket (Y₂ axis)
 *
 * If the backend only returns one expiration (which is common — the
 * shipped /heatseeker only returns the current snapshot expiry by
 * default), we still render a single bucket honestly rather than
 * fabricating multiple ones.
 *
 * Props:
 *   strikes [{ strike, call_gex, put_gex, net_gex, expiry, dte, has_zero_dte }]
 *   height  number — default 220
 */
import React, { useMemo } from 'react';

const BUCKETS = [
  { key: '0DTE', label: '0DTE', test: (dte) => dte === 0 },
  { key: '1-3D', label: '1–3D', test: (dte) => dte >= 1 && dte <= 3 },
  { key: '1W',   label: '1W',   test: (dte) => dte >= 4 && dte <= 7 },
  { key: '2W',   label: '2W',   test: (dte) => dte > 7 && dte <= 14 },
  { key: '3W',   label: '3W',   test: (dte) => dte > 14 && dte <= 21 },
  { key: '1M',   label: '1M',   test: (dte) => dte > 21 && dte <= 35 },
  { key: '>1M',  label: '>1M',  test: (dte) => dte > 35 },
];

function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  const sign = n < 0 ? '-' : '';
  if (x >= 1e9) return `${sign}${(x / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${sign}${(x / 1e6).toFixed(1)}M`;
  if (x >= 1e3) return `${sign}${(x / 1e3).toFixed(1)}K`;
  return `${sign}${x.toFixed(0)}`;
}

function bucketize(rows) {
  const buckets = BUCKETS.map((b) => ({
    ...b, call: 0, put: 0, net: 0, count: 0,
  }));
  for (const r of rows) {
    const dte = Number(r.dte);
    if (!isFinite(dte)) continue;
    const b = buckets.find((b) => b.test(dte));
    if (!b) continue;
    b.call += Number(r.call_gex) || 0;
    b.put  += Number(r.put_gex)  || 0;
    b.net  += Number(r.net_gex)  || 0;
    b.count += 1;
  }
  return buckets;
}

export default function GexExpiryBars({ strikes = [], height = 220 }) {
  const buckets = useMemo(() => bucketize(strikes), [strikes]);
  const populated = useMemo(() => buckets.filter((b) => b.count > 0), [buckets]);

  if (!populated.length) {
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
        ∅ no expiration breakdown — backend snapshot only returns current expiry
      </div>
    );
  }

  const PAD = { top: 20, right: 16, bottom: 32, left: 56 };
  const VW = 700;
  const VH = height;
  const innerW = VW - PAD.left - PAD.right;
  const innerH = VH - PAD.top - PAD.bottom;

  // Use the displayed buckets for layout.
  const n = populated.length;
  const bandW = innerW / n;
  const barGap = 4;
  const barW = (bandW - barGap * 3) / 2;

  const yMax = Math.max(
    ...populated.map((b) => Math.max(Math.abs(b.call), Math.abs(b.put))),
    1,
  );
  const yScale = innerH / yMax;
  const yZero = PAD.top + innerH;

  // Net overlay polyline (white) — scaled to same y axis.
  const netPts = populated.map((b, i) => {
    const cx = PAD.left + i * bandW + bandW / 2;
    const cy = yZero - Math.max(-innerH, Math.min(innerH, b.net * yScale));
    return `${cx.toFixed(1)},${cy.toFixed(1)}`;
  }).join(' ');

  return (
    <div className="v2-gex-expirybars" style={{ width: '100%' }}>
      <svg viewBox={`0 0 ${VW} ${VH}`}
           preserveAspectRatio="none"
           width="100%" height={height}
           style={{ display: 'block' }}>

        {/* Y baseline */}
        <line x1={PAD.left} y1={yZero}
              x2={PAD.left + innerW} y2={yZero}
              stroke="var(--border-default)" />

        {/* Y-axis labels */}
        <text x={PAD.left - 6} y={PAD.top + 8}
              fill="var(--text-tertiary)" fontSize="9"
              fontFamily="var(--font-mono)" textAnchor="end">
          {fmtBig(yMax)}
        </text>
        <text x={PAD.left - 6} y={yZero + 4}
              fill="var(--text-tertiary)" fontSize="9"
              fontFamily="var(--font-mono)" textAnchor="end">
          0
        </text>

        {/* Bars */}
        {populated.map((b, i) => {
          const x0 = PAD.left + i * bandW + barGap;
          const xPut  = x0;
          const xCall = x0 + barW + barGap;
          const hPut  = Math.abs(b.put)  * yScale;
          const hCall = Math.abs(b.call) * yScale;
          return (
            <g key={b.key}>
              <rect x={xPut} y={yZero - hPut}
                    width={barW} height={hPut}
                    fill="var(--accent-red)" opacity={0.75}>
                <title>{b.label} · put GEX {fmtBig(b.put)} (n={b.count})</title>
              </rect>
              <rect x={xCall} y={yZero - hCall}
                    width={barW} height={hCall}
                    fill="var(--accent-green)" opacity={0.75}>
                <title>{b.label} · call GEX {fmtBig(b.call)} (n={b.count})</title>
              </rect>
              <text x={PAD.left + i * bandW + bandW / 2}
                    y={yZero + 14}
                    fill="var(--text-tertiary)" fontSize="10"
                    fontFamily="var(--font-mono)" textAnchor="middle">
                {b.label}
              </text>
            </g>
          );
        })}

        {/* Net polyline */}
        {populated.length > 1 && (
          <polyline points={netPts}
                    fill="none"
                    stroke="rgba(241, 245, 249, 0.85)"
                    strokeWidth={1.4} />
        )}
      </svg>

      {/* Legend */}
      <div style={{
        display: 'flex', gap: 14, padding: '6px 10px 0',
        fontFamily: 'var(--font-mono)', fontSize: 10,
        color: 'var(--text-tertiary)', flexWrap: 'wrap',
      }}>
        <span><span style={{
          display: 'inline-block', width: 12, height: 8, background: 'var(--accent-green)',
          marginRight: 4, verticalAlign: 'middle',
        }} />Call GEX</span>
        <span><span style={{
          display: 'inline-block', width: 12, height: 8, background: 'var(--accent-red)',
          marginRight: 4, verticalAlign: 'middle',
        }} />Put GEX</span>
        <span><span style={{
          display: 'inline-block', width: 14, borderTop: '2px solid rgba(241,245,249,0.85)',
          marginRight: 4, verticalAlign: 'middle',
        }} />Net GEX</span>
      </div>
    </div>
  );
}
