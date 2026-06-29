/* MITS Phase 19 Cluster D — SectorHeatmap component.
 *
 * Treemap-style block layout where each sector is a rectangle:
 *   - size = exposure (pct of portfolio)
 *   - colour = sector PnL (green positive / red negative / neutral grey)
 *
 * Source: /portfolio/risk → by_sector { "Tech": { value, pct }, … }
 *         /portfolio/context → by_sector { "Tech": 0.28, … }
 *
 * Props:
 *   sectors:    { name: { value, pct, pnl? } } OR { name: pct }
 *   pnlByTicker: optional { TICKER: pnl } so we can derive sector PnL if
 *               by_sector doesn't carry it
 *   tickerSectors: optional { TICKER: 'Tech' } map for fallback PnL agg
 *   height:     px (default 280)
 */
import React, { useMemo } from 'react';
import { EmptyState } from '../../design/Components.jsx';

function fmtPct(v) {
  if (v == null || !isFinite(v)) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}
function fmtMoney(v) {
  if (v == null || !isFinite(v)) return '—';
  const sign = v >= 0 ? '+' : '';
  return `${sign}$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 0, maximumFractionDigits: 0,
  })}`;
}

// Squarified treemap-ish row layout — simple but works well for 3-10 sectors.
function layout(items, totalW, totalH) {
  const total = items.reduce((s, x) => s + x.weight, 0) || 1;
  // Sort largest first.
  const sorted = [...items].sort((a, b) => b.weight - a.weight);
  // Pack into rows of 1-3 boxes greedily.
  const rows = [];
  let row = [];
  let rowWeight = 0;
  // Target row weight = total / ceil(N / 2.5)
  const targetRows = Math.max(1, Math.ceil(sorted.length / 2.5));
  const targetRowWeight = total / targetRows;
  for (const it of sorted) {
    row.push(it);
    rowWeight += it.weight;
    if (rowWeight >= targetRowWeight && row.length >= 1) {
      rows.push(row);
      row = [];
      rowWeight = 0;
    }
  }
  if (row.length) rows.push(row);

  const out = [];
  let y = 0;
  for (const r of rows) {
    const rw = r.reduce((s, x) => s + x.weight, 0);
    const h = (rw / total) * totalH;
    let x = 0;
    for (const it of r) {
      const w = (it.weight / rw) * totalW;
      out.push({ ...it, x, y, w, h });
      x += w;
    }
    y += h;
  }
  return out;
}

export default function SectorHeatmap({
  sectors = {},
  pnlByTicker = {},
  tickerSectors = {},
  height = 280,
}) {
  const items = useMemo(() => {
    const entries = Object.entries(sectors || {});
    if (entries.length === 0) return [];
    return entries.map(([name, raw]) => {
      let pct, value, pnl;
      if (typeof raw === 'number') {
        pct = raw;
        value = null;
      } else if (raw && typeof raw === 'object') {
        pct = raw.pct ?? raw.weight ?? null;
        value = raw.value ?? raw.market_value ?? null;
        pnl = raw.pnl ?? null;
      }
      // Fallback: aggregate PnL from per-ticker
      if (pnl == null && tickerSectors && pnlByTicker) {
        let agg = 0;
        let any = false;
        for (const [tk, sec] of Object.entries(tickerSectors)) {
          if (sec === name && pnlByTicker[tk] != null) {
            agg += Number(pnlByTicker[tk]);
            any = true;
          }
        }
        if (any) pnl = agg;
      }
      return { name, weight: Math.max(0.01, Number(pct) || 0), pct, value, pnl };
    }).filter(x => x.weight > 0);
  }, [sectors, pnlByTicker, tickerSectors]);

  if (items.length === 0) {
    return <EmptyState icon="🗺" message="No sector exposure data." />;
  }

  // Determine colour from pnl.
  const pnlsKnown = items.some(x => x.pnl != null && isFinite(x.pnl));
  const maxAbsPnl = Math.max(1, ...items.map(x => Math.abs(Number(x.pnl) || 0)));

  function cellColor(it) {
    if (!pnlsKnown || it.pnl == null) {
      // Fall back to neutral cyan tint.
      return `rgba(0, 212, 255, ${0.10 + 0.5 * it.weight})`;
    }
    const t = Math.min(1, Math.abs(Number(it.pnl)) / maxAbsPnl);
    if (it.pnl >= 0) return `rgba(0, 255, 136, ${0.10 + 0.55 * t})`;
    return `rgba(255, 51, 85, ${0.10 + 0.55 * t})`;
  }

  const W = 1000;
  const H = height;
  const laid = layout(items, W, H);

  return (
    <div className="v2-secheat" style={{ width: '100%' }}>
      <svg viewBox={`0 0 ${W} ${H}`}
           preserveAspectRatio="none"
           style={{ width: '100%', height, display: 'block' }}
           role="img"
           aria-label="Sector exposure heatmap">
        {laid.map((it, i) => (
          <g key={i}>
            <rect x={it.x + 2} y={it.y + 2}
                  width={Math.max(0, it.w - 4)}
                  height={Math.max(0, it.h - 4)}
                  fill={cellColor(it)}
                  stroke="var(--border-default)"
                  strokeWidth="1"
                  rx="6" />
            <text x={it.x + 12} y={it.y + 22}
                  fontSize="14"
                  fontWeight="700"
                  fill="var(--text-primary)"
                  fontFamily="var(--font-display)">
              {it.name}
            </text>
            <text x={it.x + 12} y={it.y + 40}
                  fontSize="12"
                  fill="var(--text-secondary)"
                  fontFamily="var(--font-mono)">
              {fmtPct(it.pct)}
            </text>
            {it.value != null && (
              <text x={it.x + 12} y={it.y + 56}
                    fontSize="11"
                    fill="var(--text-tertiary)"
                    fontFamily="var(--font-mono)">
                ${Number(it.value).toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </text>
            )}
            {it.pnl != null && isFinite(it.pnl) && (
              <text x={it.x + 12} y={it.y + 72}
                    fontSize="11"
                    fill={it.pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}
                    fontFamily="var(--font-mono)">
                {fmtMoney(it.pnl)}
              </text>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}
