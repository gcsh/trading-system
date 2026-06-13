/* MITS Phase 19 Stream 1 — TheoryOverlay builder.
 *
 * Translates the /theories/{theory}/{ticker} response shape (or the
 * multi-theory endpoint /theories/multi/{ticker}) into the
 * primitive-friendly object OHLCChart consumes:
 *
 *   {
 *     trendLines: [{x1, x2, y1, y2, color, style, lineWidth}],
 *     priceLines: [{price, color, lineStyle, title, lineWidth}],
 *     markers:    [{t, position, color, shape, text}],
 *     entryZone:  {y1, y2, color, opacity}  // first zone in primary theory
 *   }
 *
 * The TheoryOverlay component itself is a tiny renderer wrapper that
 * also draws a chip legend showing which theories are active + a CTA
 * to deep-link into the legacy Theory Studio for full control.
 *
 * Why not delegate to the existing TheoryChart? TheoryChart pulls
 * its own bars + owns a chart instance. The v2 Stock Detail page
 * needs to render a single chart with theory overlays AND entry-zone
 * boxes from the current open position — so we keep the chart in
 * OHLCChart and feed it a normalized overlay dict instead.
 */
import React from 'react';

const PALETTE = {
  price_action: '#a855f7',
  bollinger:    '#00d4ff',
  ma_ribbon:    '#fbbf24',
  pivots:       '#94a3b8',
  fibonacci:    '#f472b6',
  donchian:     '#34d399',
  keltner:      '#fb923c',
  ichimoku:     '#60a5fa',
  default:      '#cbd5e1',
};

function colorFor(theory) {
  return PALETTE[theory] || PALETTE.default;
}

/**
 * Build the OHLCChart-friendly overlay dict from a /theories response.
 * The single-theory response has top-level: lines[], zones[], markers[],
 * signals[]. The multi-theory response has annotations: { theory: ... }.
 *
 * We also support a custom "primary" overlay (entry/target/stop) passed
 * in by the parent (e.g. from the current open trade or the latest
 * recommendation).
 */
export function buildOverlays(payload, opts = {}) {
  const out = {
    trendLines: [],
    priceLines: [],
    markers:    [],
    entryZone:  null,
  };

  // Normalize: convert multi-theory shape into a flat per-theory list.
  const annotationsByTheory = {};
  if (payload?.annotations && typeof payload.annotations === 'object') {
    Object.assign(annotationsByTheory, payload.annotations);
  } else if (payload && (payload.lines || payload.zones || payload.markers || payload.signals)) {
    annotationsByTheory[payload.theory || 'theory'] = payload;
  }

  for (const [theoryName, ann] of Object.entries(annotationsByTheory)) {
    if (!ann) continue;
    const c = colorFor(theoryName);

    // horizontal levels → priceLines
    for (const ln of (ann.lines || [])) {
      if (ln.kind === 'horizontal' && ln.start?.price != null) {
        out.priceLines.push({
          price:     Number(ln.start.price),
          color:     ln.color || c,
          lineWidth: Math.max(1, ln.width || 1),
          lineStyle: ln.style || 'dashed',
          title:     ln.label || '',
        });
      }
      // trendlines + rays
      if (ln.kind === 'trendline' && ln.start && ln.end) {
        out.trendLines.push({
          x1: ln.start.ts, y1: Number(ln.start.price),
          x2: ln.end.ts,   y2: Number(ln.end.price),
          color: ln.color || c,
          lineWidth: Math.max(1, ln.width || 1),
          style: ln.style || 'solid',
        });
      }
    }
    // markers → BOS / CHoCH labels
    for (const m of (ann.markers || [])) {
      out.markers.push({
        t:        m.ts,
        position: m.shape === 'arrow_down' ? 'aboveBar'
                   : (m.shape === 'arrow_up' ? 'belowBar' : 'aboveBar'),
        color:    m.color || c,
        shape:    m.shape === 'arrow_down' ? 'arrowDown'
                   : (m.shape === 'arrow_up' ? 'arrowUp' : 'circle'),
        text:     m.label || '',
      });
    }
    // First zone in the primary theory wins as the entry-zone display.
    if (!out.entryZone && Array.isArray(ann.zones) && ann.zones.length) {
      const z = ann.zones[0];
      out.entryZone = {
        y1: Number(z.y1), y2: Number(z.y2),
        color:   z.color || '#8b5e3c',
        opacity: z.opacity != null ? z.opacity : 0.18,
      };
    }
  }

  // Operator-provided entry / target / stop wins over theory-derived ones.
  if (opts.entry != null && !Number.isNaN(Number(opts.entry))) {
    out.priceLines.push({
      price: Number(opts.entry), color: '#00d4ff',
      lineWidth: 2, lineStyle: 'solid', title: 'ENTRY',
    });
  }
  if (opts.target != null && !Number.isNaN(Number(opts.target))) {
    out.priceLines.push({
      price: Number(opts.target), color: '#00ff88',
      lineWidth: 2, lineStyle: 'dashed', title: 'TARGET',
    });
  }
  if (opts.stop != null && !Number.isNaN(Number(opts.stop))) {
    out.priceLines.push({
      price: Number(opts.stop), color: '#ff3355',
      lineWidth: 2, lineStyle: 'dashed', title: 'STOP',
    });
  }
  if (opts.entryZone) {
    out.entryZone = {
      y1: Number(opts.entryZone.y1),
      y2: Number(opts.entryZone.y2),
      color: opts.entryZone.color || '#8b5e3c',
      opacity: opts.entryZone.opacity != null ? opts.entryZone.opacity : 0.2,
    };
  }
  return out;
}

/**
 * Tiny legend strip the StockDetail page renders below the chart so
 * the operator can see which theories are active + jump out to the
 * legacy Theory Studio for deep editing.
 */
export default function TheoryOverlay({
  activeTheories = [],
  ticker,
  onTheoryToggle,
  empty = false,
  emptyMessage = 'No theory overlays for this window.',
}) {
  if (empty || !activeTheories.length) {
    return (
      <div className="v2-theory-legend v2-theory-legend--empty">
        <span className="dim">{emptyMessage}</span>
        {ticker && (
          <a className="v2-theory-legend__link"
             href={`/v1/analysis/${encodeURIComponent(ticker)}`}>
            Open in Theory Studio →
          </a>
        )}
        <style>{`
          .v2-theory-legend--empty {
            display: flex; align-items: center; gap: 12px;
            padding: 8px 12px;
            background: var(--bg-tertiary);
            border: 1px dashed var(--border-default);
            border-radius: var(--radius-md);
            font-size: 12px;
          }
          .v2-theory-legend--empty .dim { color: var(--text-tertiary); }
        `}</style>
      </div>
    );
  }
  return (
    <div className="v2-theory-legend">
      <span className="v2-theory-legend__label">overlays:</span>
      {activeTheories.map((t) => (
        <button
          key={t}
          type="button"
          className="v2-theory-chip"
          onClick={onTheoryToggle ? () => onTheoryToggle(t) : undefined}
          style={{ borderColor: colorFor(t), color: colorFor(t) }}
          title={`Toggle ${t.replaceAll('_', ' ')}`}
        >
          <span className="v2-theory-chip__dot"
                style={{ background: colorFor(t) }} />
          {t.replaceAll('_', ' ')}
        </button>
      ))}
      {ticker && (
        <a className="v2-theory-legend__link"
           href={`/v1/analysis/${encodeURIComponent(ticker)}`}>
          edit in Theory Studio →
        </a>
      )}
      <style>{`
        .v2-theory-legend {
          display: flex; flex-wrap: wrap; align-items: center;
          gap: 6px; padding: 6px 10px;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-md);
          font-size: 11px;
        }
        .v2-theory-legend__label {
          color: var(--text-tertiary);
          text-transform: uppercase; letter-spacing: 0.06em;
          margin-right: 4px;
        }
        .v2-theory-chip {
          display: inline-flex; align-items: center; gap: 6px;
          padding: 2px 8px;
          background: rgba(0,0,0,0.25);
          border: 1px solid var(--border-default);
          border-radius: 999px;
          font-size: 11px;
          font-family: 'JetBrains Mono', monospace;
          cursor: pointer;
        }
        .v2-theory-chip__dot {
          width: 6px; height: 6px; border-radius: 50%;
        }
        .v2-theory-legend__link {
          margin-left: auto;
          color: var(--accent-cyan);
          font-size: 11px;
          text-decoration: none;
        }
        .v2-theory-legend__link:hover { text-decoration: underline; }
      `}</style>
    </div>
  );
}
