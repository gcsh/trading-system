/**
 * CumulativeGexPanel — running sum of net_gex from top strike downward.
 *
 * Where the cumulative line crosses zero is the gamma flip — the level
 * at which dealer hedging regime flips between stabilizing and
 * amplifying. Sits to the right of the per-strike heatmap (Item #14).
 *
 * The data already includes gex_by_strike with net_gex per strike;
 * we cumsum on the client to keep backend untouched.
 */
import React, { useMemo } from 'react';
import { money } from '../lib/format.js';

function fmtCompact(n) {
  if (n == null || isNaN(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return n.toFixed(0);
}

export default function CumulativeGexPanel({ rows, spotStrike, flip }) {
  // rows are already sorted high→low strike (like a chain ladder).
  // Cumsum from top → bottom mirrors the "running exposure as we walk
  // down the chain" interpretation: where running sum crosses zero is
  // where dealer regime flips.
  const cumRows = useMemo(() => {
    if (!rows?.length) return [];
    let acc = 0;
    return rows.map((r) => {
      acc += r.net_gex || 0;
      return { ...r, cum_gex: acc };
    });
  }, [rows]);

  const maxAbs = useMemo(() => {
    if (!cumRows.length) return 1;
    return Math.max(...cumRows.map((r) => Math.abs(r.cum_gex))) || 1;
  }, [cumRows]);

  // Mark the strike row whose cumulative is closest to zero AND straddles
  // the sign change (visual flip indicator on this panel).
  const flipIdx = useMemo(() => {
    if (!cumRows.length) return -1;
    let best = -1, bestAbs = Infinity;
    for (let i = 0; i < cumRows.length; i++) {
      const a = Math.abs(cumRows[i].cum_gex);
      if (a < bestAbs) { bestAbs = a; best = i; }
    }
    return best;
  }, [cumRows]);

  if (!cumRows.length) {
    return <div className="empty">No cumulative data.</div>;
  }

  return (
    <div>
      <div style={{
        fontSize: 11, color: 'var(--muted)', marginBottom: 6,
        textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600,
      }}>
        Cumulative GEX (top → down)
      </div>
      <div style={{ display: 'grid', gap: 1 }}>
        {cumRows.map((r, i) => {
          const positive = r.cum_gex >= 0;
          const pct = Math.min(100, (Math.abs(r.cum_gex) / maxAbs) * 100);
          const isSpot = spotStrike != null && Math.abs(r.strike - spotStrike) < 1e-6;
          const isFlip = i === flipIdx;
          const bg = positive
            ? `linear-gradient(90deg, transparent ${100 - pct}%, rgba(80,200,140,0.45) 100%)`
            : `linear-gradient(90deg, rgba(220,70,90,0.45) 0%, transparent ${pct}%)`;
          return (
            <div
              key={r.strike}
              style={{
                position: 'relative',
                padding: '4px 8px',
                fontSize: 11,
                background: bg,
                borderLeft: isSpot ? '2px solid var(--info)' : 'none',
                outline: isFlip ? '1px dashed var(--warn)' : 'none',
              }}
            >
              <div className="row" style={{ gap: 8 }}>
                <span style={{
                  minWidth: 56, color: isSpot ? 'var(--info)' : 'var(--text-2)',
                  fontWeight: isSpot ? 700 : 400,
                }}>
                  {Number(r.strike).toFixed(1)}
                </span>
                <span style={{
                  marginLeft: 'auto',
                  color: positive ? 'var(--accent)' : 'var(--danger)',
                  fontWeight: isFlip ? 700 : 500,
                }}>
                  {fmtCompact(r.cum_gex)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ fontSize: 10, color: 'var(--muted-2)', marginTop: 6 }}>
        Dashed outline = strike where running sum is closest to zero (~gamma flip on this view).
        Spot row in info blue.
      </div>
    </div>
  );
}
