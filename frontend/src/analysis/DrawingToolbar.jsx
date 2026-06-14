/**
 * Phase C.3 — chart drawing toolbar (vertical strip on chart left edge).
 *
 * Visual TradingView-style icon column. The Cursor tool is wired (it's
 * the default no-op selection state). The actual freehand drawing
 * primitives (trendline, horizontal line, fib retrace, rectangle, text)
 * scaffold here but route through ``onSelect`` so a follow-up commit
 * can hook them into a shared canvas overlay on top of TheoryChart.
 *
 * Why ship the strip ahead of the drawing engine: the chart wrapper
 * now has a dedicated left rail that signals "drawings live here" to
 * the operator, the tool state machine is in place, and the next
 * commit only needs to add the pointer handlers + persistence layer.
 *
 * Persistence: per-ticker, keyed ``tb.analysis.drawings.<TICKER>``
 * once the engine lands. The toolbar itself doesn't write — it just
 * picks the active tool and lets the parent decide what to do.
 */
import React from 'react';

export const DRAWING_TOOLS = [
  { id: 'cursor',     glyph: '✥',  label: 'Cursor',          status: 'ready' },
  { id: 'trendline',  glyph: '⟋',  label: 'Trend line',      status: 'soon' },
  { id: 'horizontal', glyph: '─',  label: 'Horizontal line', status: 'soon' },
  { id: 'fib',        glyph: '𝐅',  label: 'Fib retracement', status: 'soon' },
  { id: 'rect',       glyph: '▭',  label: 'Rectangle',       status: 'soon' },
  { id: 'text',       glyph: 'T',  label: 'Text note',       status: 'soon' },
];

export default function DrawingToolbar({ active = 'cursor', onSelect }) {
  return (
    <div
      role="toolbar"
      aria-label="Chart drawing tools"
      data-testid="analysis-drawing-toolbar"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        padding: 4,
        background: 'var(--bg-secondary, #0d111f)',
        border: '1px solid var(--border-subtle, #2a3349)',
        borderRadius: 6,
        width: 36,
        flexShrink: 0,
      }}
    >
      {DRAWING_TOOLS.map((tool) => {
        const isActive = active === tool.id;
        const enabled = tool.status === 'ready';
        return (
          <button
            key={tool.id}
            type="button"
            data-testid={`drawing-tool-${tool.id}`}
            aria-pressed={isActive}
            disabled={!enabled}
            onClick={() => onSelect && enabled && onSelect(tool.id)}
            title={enabled ? tool.label : `${tool.label} — coming next`}
            style={{
              width: 28, height: 28,
              padding: 0,
              borderRadius: 4,
              border: '1px solid ' + (isActive
                ? 'var(--accent, #5fc9ce)'
                : 'transparent'),
              background: isActive
                ? 'rgba(95, 201, 206, 0.12)'
                : 'transparent',
              color: enabled
                ? (isActive ? 'var(--accent, #5fc9ce)' : 'var(--text-primary, #e6edf3)')
                : 'var(--muted, #5b6985)',
              cursor: enabled ? 'pointer' : 'not-allowed',
              fontSize: 14,
              lineHeight: 1,
              opacity: enabled ? 1 : 0.55,
            }}
          >
            {tool.glyph}
          </button>
        );
      })}
    </div>
  );
}
