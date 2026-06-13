/* MITS Phase 19 Cluster B — FlowDepthChart.
 *
 * Aggregated premium-by-strike horizontal bar chart for a single ticker.
 * Takes a list of flow ticks (from /flow/{ticker}) and groups them by
 * (strike, option_type), then draws a divergent depth view:
 *
 *     calls (right, green)  ◄─┤ STRIKE 195 ├─►  puts (left, red)
 *
 * Props:
 *   flows:    array of flow ticks with shape:
 *             { strike, option_type ('call'|'put'), premium, sentiment, ... }
 *   topN:     how many strikes to show (default 12, sorted by total premium).
 *   spot:     optional spot price — drawn as a horizontal line marker.
 *
 * Renders an EmptyState-style block if no flows are passed.
 */
import React, { useMemo } from 'react';

function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  if (x >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (x >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${Number(n).toFixed(0)}`;
}

export default function FlowDepthChart({ flows = [], topN = 12, spot = null }) {
  const groups = useMemo(() => {
    if (!Array.isArray(flows) || flows.length === 0) return [];
    const acc = new Map();   // strike -> { call: prem, put: prem, callN, putN }
    for (const f of flows) {
      const k = Number(f.strike);
      if (!isFinite(k) || k <= 0) continue;
      const type = (f.option_type || '').toLowerCase();
      const prem = Number(f.premium) || 0;
      if (!acc.has(k)) acc.set(k, { strike: k, call: 0, put: 0, callN: 0, putN: 0 });
      const row = acc.get(k);
      if (type === 'call') { row.call += prem; row.callN += 1; }
      else if (type === 'put') { row.put += prem; row.putN += 1; }
    }
    const arr = Array.from(acc.values());
    arr.sort((a, b) => (b.call + b.put) - (a.call + a.put));
    return arr.slice(0, topN).sort((a, b) => b.strike - a.strike);
  }, [flows, topN]);

  if (!groups.length) {
    return (
      <div className="v2-fdc v2-fdc--empty">
        <div className="v2-fdc__icon">∅</div>
        <div className="v2-fdc__msg">No flow ticks for this ticker.</div>
        <style>{`
          .v2-fdc--empty {
            padding: 32px 12px;
            text-align: center;
            background: var(--bg-tertiary);
            border: 1px dashed var(--border-default);
            border-radius: var(--radius-md);
          }
          .v2-fdc__icon {
            font-size: 24px;
            color: var(--text-muted);
            margin-bottom: 4px;
          }
          .v2-fdc__msg {
            font-size: 12px;
            color: var(--text-tertiary);
          }
        `}</style>
      </div>
    );
  }

  // Scale: max bar width relative to the largest single side across all rows.
  const maxSide = Math.max(
    ...groups.flatMap((g) => [g.call, g.put]),
    1,
  );

  // Determine where spot lands in the strike list (for the gold separator).
  let spotInsertIdx = -1;
  if (spot != null && isFinite(spot) && groups.length > 1) {
    for (let i = 0; i < groups.length - 1; i++) {
      const a = groups[i].strike, b = groups[i + 1].strike;
      if (Math.min(a, b) <= spot && spot <= Math.max(a, b)) {
        spotInsertIdx = i;
        break;
      }
    }
  }

  return (
    <div className="v2-fdc">
      <div className="v2-fdc__head">
        <span className="v2-fdc__h-side v2-fdc__h-side--put">PUTS Σ</span>
        <span className="v2-fdc__h-strike">strike</span>
        <span className="v2-fdc__h-side v2-fdc__h-side--call">CALLS Σ</span>
      </div>
      <div className="v2-fdc__rows">
        {groups.map((g, i) => {
          const putW = (g.put / maxSide) * 100;
          const callW = (g.call / maxSide) * 100;
          const isPutHeavy = g.put > g.call;
          return (
            <React.Fragment key={g.strike}>
              <div className="v2-fdc__row"
                   title={`Strike ${g.strike}: calls $${g.call.toFixed(0)} (n=${g.callN}), puts $${g.put.toFixed(0)} (n=${g.putN})`}>
                <div className="v2-fdc__cell v2-fdc__cell--put">
                  <span className="mono v2-fdc__amt">{fmtBig(g.put)}</span>
                  <div className="v2-fdc__bar v2-fdc__bar--put"
                       style={{ width: `${putW}%` }} />
                </div>
                <div className={`v2-fdc__strike mono ${isPutHeavy ? 'v2-fdc__strike--put' : 'v2-fdc__strike--call'}`}>
                  {g.strike.toFixed(g.strike % 1 === 0 ? 0 : 2)}
                </div>
                <div className="v2-fdc__cell v2-fdc__cell--call">
                  <div className="v2-fdc__bar v2-fdc__bar--call"
                       style={{ width: `${callW}%` }} />
                  <span className="mono v2-fdc__amt">{fmtBig(g.call)}</span>
                </div>
              </div>
              {spotInsertIdx === i && (
                <div className="v2-fdc__spot mono"
                     title={`Spot ≈ ${spot.toFixed(2)}`}>
                  ◄ spot {spot.toFixed(2)} ►
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>
      <style>{`
        .v2-fdc { width: 100%; }
        .v2-fdc__head {
          display: grid;
          grid-template-columns: 1fr auto 1fr;
          gap: 8px;
          padding: 4px 8px 8px;
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: var(--text-tertiary);
          border-bottom: 1px solid var(--border-subtle);
        }
        .v2-fdc__h-side { text-align: center; }
        .v2-fdc__h-side--put  { color: var(--accent-red); text-align: left; }
        .v2-fdc__h-side--call { color: var(--accent-green); text-align: right; }
        .v2-fdc__h-strike {
          text-align: center;
          min-width: 64px;
        }
        .v2-fdc__rows {
          display: flex; flex-direction: column;
          margin-top: 4px;
        }
        .v2-fdc__row {
          display: grid;
          grid-template-columns: 1fr auto 1fr;
          gap: 8px;
          align-items: center;
          padding: 2px 8px;
          min-height: 22px;
        }
        .v2-fdc__row:hover {
          background: var(--bg-elevated);
        }
        .v2-fdc__cell {
          display: flex; align-items: center; gap: 6px;
        }
        .v2-fdc__cell--put {
          justify-content: flex-end;
          flex-direction: row;
        }
        .v2-fdc__cell--call {
          justify-content: flex-start;
          flex-direction: row;
        }
        .v2-fdc__bar {
          height: 14px;
          border-radius: 2px;
          opacity: 0.85;
        }
        .v2-fdc__bar--put  { background: var(--accent-red); }
        .v2-fdc__bar--call { background: var(--accent-green); }
        .v2-fdc__amt {
          font-size: 10px;
          color: var(--text-secondary);
          min-width: 36px;
        }
        .v2-fdc__strike {
          min-width: 64px;
          text-align: center;
          font-size: 12px;
          font-weight: 700;
          padding: 2px 6px;
          border-radius: var(--radius-sm);
          background: var(--bg-tertiary);
        }
        .v2-fdc__strike--put  { color: var(--accent-red); }
        .v2-fdc__strike--call { color: var(--accent-green); }
        .v2-fdc__spot {
          text-align: center;
          font-size: 10px;
          color: var(--accent-yellow);
          padding: 4px 0;
          background: rgba(255, 215, 0, 0.06);
          border-top: 1px dashed var(--accent-yellow);
          border-bottom: 1px dashed var(--accent-yellow);
          margin: 2px 0;
        }
      `}</style>
    </div>
  );
}
