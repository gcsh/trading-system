/* MITS Phase 19 Cluster B — KnowledgeMatrix.
 *
 * Pattern × Regime heat-grid for one ticker.
 *
 * Props:
 *   cells:    array of /knowledge/cells rows (filtered: e.g. sample_split=combined, horizon=1d).
 *   patterns: ordered row labels (top-N by sample size).
 *   regimes:  ordered col labels.
 *   onCellClick(pattern, regime, cell)  — drill-in callback.
 *   selected: { pattern, regime } | null  — highlights the cell.
 *
 * Visual:
 *   - cell fill color  → posterior_win_rate (red 0.3 → yellow 0.5 → green 0.7+).
 *   - cell opacity     → sample_size (log-scale).
 *   - cell text        → posterior_wr * 100 (1dp) if cell exists, else "·".
 *   - cell title       → "pattern × regime · n=NN · WR XX% · CI [lo, hi]".
 *
 * Empty cells render as a dotted placeholder so the grid stays aligned.
 */
import React, { useMemo } from 'react';

function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

/** Diverging palette: red at 0.30, yellow at 0.50, green at 0.70+. */
function wrColor(wr, alpha) {
  if (wr == null || !isFinite(wr)) return 'transparent';
  const t = clamp((wr - 0.30) / 0.40, 0, 1);   // 0 at .30, 1 at .70
  // Interpolate red → yellow → green.
  let r, g, b;
  if (t < 0.5) {
    // red → yellow
    const k = t / 0.5;
    r = 255;
    g = Math.round(51  + (215 - 51)  * k);
    b = Math.round(85  + (0   - 85)  * k);
  } else {
    // yellow → green
    const k = (t - 0.5) / 0.5;
    r = Math.round(255 + (0   - 255) * k);
    g = Math.round(215 + (255 - 215) * k);
    b = Math.round(0   + (136 - 0)   * k);
  }
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Log-scale opacity from sample_size: 0 → 0.12, 30 → 0.55, 200+ → 0.95. */
function sizeOpacity(n) {
  if (!n || n <= 0) return 0.12;
  const k = Math.log10(n + 1) / Math.log10(201); // 0..~1 across 0..200
  return clamp(0.12 + k * 0.83, 0.12, 0.95);
}

export default function KnowledgeMatrix({
  cells = [],
  patterns = [],
  regimes = [],
  onCellClick,
  selected = null,
}) {
  // Index cells by (pattern, regime) — prefer the highest sample_size if dupes.
  const cellMap = useMemo(() => {
    const m = new Map();
    for (const c of cells) {
      const k = `${c.pattern}|${c.regime}`;
      const prev = m.get(k);
      if (!prev || (c.sample_size || 0) > (prev.sample_size || 0)) {
        m.set(k, c);
      }
    }
    return m;
  }, [cells]);

  if (!patterns.length || !regimes.length) {
    return (
      <div className="v2-km v2-km--empty">
        <span>No knowledge cells for current filters.</span>
        <style>{`
          .v2-km--empty {
            padding: 24px; text-align: center;
            color: var(--text-tertiary);
            font-size: 12px;
            border: 1px dashed var(--border-default);
            border-radius: var(--radius-md);
          }
        `}</style>
      </div>
    );
  }

  return (
    <div className="v2-km">
      <div className="v2-km__grid"
           style={{
             gridTemplateColumns: `minmax(150px, 1fr) repeat(${regimes.length}, minmax(64px, 1fr))`,
           }}>
        {/* header row */}
        <div className="v2-km__corner" />
        {regimes.map((r) => (
          <div key={r} className="v2-km__colhead" title={r}>
            {r.replaceAll('_', ' ')}
          </div>
        ))}
        {/* body rows */}
        {patterns.map((p) => (
          <React.Fragment key={p}>
            <div className="v2-km__rowhead mono" title={p}>{p}</div>
            {regimes.map((r) => {
              const c = cellMap.get(`${p}|${r}`);
              const isSel = selected && selected.pattern === p && selected.regime === r;
              if (!c) {
                return (
                  <div key={r} className="v2-km__cell v2-km__cell--empty">
                    ·
                  </div>
                );
              }
              const wr = c.posterior_win_rate;
              const n = c.sample_size;
              const lo = c.confidence_lower != null ? (c.confidence_lower * 100).toFixed(0) : '—';
              const hi = c.confidence_upper != null ? (c.confidence_upper * 100).toFixed(0) : '—';
              const bg = wrColor(wr, sizeOpacity(n));
              const title = `${p} × ${r}\nn=${n}  WR=${(wr * 100).toFixed(1)}%  CI [${lo}, ${hi}]%`;
              return (
                <button
                  key={r}
                  type="button"
                  className={`v2-km__cell ${isSel ? 'v2-km__cell--sel' : ''}`}
                  onClick={() => onCellClick && onCellClick(p, r, c)}
                  style={{ background: bg }}
                  title={title}
                >
                  <span className="mono">{(wr * 100).toFixed(0)}</span>
                  <span className="v2-km__n mono">n{n}</span>
                </button>
              );
            })}
          </React.Fragment>
        ))}
      </div>

      <div className="v2-km__legend">
        <span className="dim">posterior win-rate:</span>
        <span className="v2-km__leg-sw"
              style={{ background: wrColor(0.30, 0.85) }} />
        <span className="mono">30%</span>
        <span className="v2-km__leg-sw"
              style={{ background: wrColor(0.50, 0.85) }} />
        <span className="mono">50%</span>
        <span className="v2-km__leg-sw"
              style={{ background: wrColor(0.70, 0.85) }} />
        <span className="mono">70%+</span>
        <span className="dim" style={{ marginLeft: 12 }}>opacity = sample size</span>
      </div>

      <style>{`
        .v2-km { width: 100%; }
        .v2-km__grid {
          display: grid;
          gap: 2px;
          background: var(--border-subtle);
          padding: 2px;
          border-radius: var(--radius-md);
          overflow: auto;
          max-width: 100%;
        }
        .v2-km__corner {
          background: var(--bg-tertiary);
        }
        .v2-km__colhead {
          background: var(--bg-tertiary);
          color: var(--text-tertiary);
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          padding: 8px 6px;
          text-align: center;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .v2-km__rowhead {
          background: var(--bg-tertiary);
          color: var(--text-secondary);
          font-size: 11px;
          padding: 8px 10px;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          border-right: 1px solid var(--border-subtle);
        }
        .v2-km__cell {
          background: var(--bg-secondary);
          border: 1px solid transparent;
          color: var(--text-primary);
          font-size: 11px;
          padding: 6px 4px;
          cursor: pointer;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 1px;
          min-height: 44px;
          transition: transform var(--transition-fast), border-color var(--transition-fast);
        }
        .v2-km__cell:hover {
          transform: scale(1.06);
          border-color: var(--accent-cyan);
        }
        .v2-km__cell--sel {
          border-color: var(--accent-cyan);
          box-shadow: var(--shadow-glow-cyan);
        }
        .v2-km__cell--empty {
          color: var(--text-muted);
          cursor: default;
          background: var(--bg-secondary);
        }
        .v2-km__cell--empty:hover {
          transform: none;
          border-color: transparent;
        }
        .v2-km__n {
          font-size: 9px;
          color: rgba(0,0,0,0.7);
          opacity: 0.85;
        }
        .v2-km__legend {
          display: flex; align-items: center; gap: 6px;
          margin-top: 10px;
          font-size: 11px;
          flex-wrap: wrap;
        }
        .v2-km__leg-sw {
          display: inline-block;
          width: 14px; height: 14px;
          border-radius: 3px;
        }
        .v2-km .dim { color: var(--text-tertiary); }
        @media (max-width: 768px) {
          .v2-km__grid {
            font-size: 10px;
          }
          .v2-km__rowhead { padding: 6px 6px; font-size: 10px; }
          .v2-km__colhead { font-size: 9px; padding: 6px 3px; }
          .v2-km__cell { min-height: 36px; padding: 4px 2px; }
        }
      `}</style>
    </div>
  );
}
