/* MITS Phase 19 Cluster B — TheorySelector.
 *
 * Multi-select chip row for picking which technical theories to overlay
 * on a chart. Pulled from /theories which returns:
 *   { theories: [{name, label}, ...], windows: [...] }
 *
 * Props:
 *   theories:   [{name, label}]      — full list (from /theories).
 *   selected:   string[]             — currently active theory names.
 *   onChange(next: string[])         — replaces the selected array.
 *   max:        number               — soft cap; default 5 (perf safety).
 *   palette:    {theoryName -> hex}  — color overrides for chip dot.
 */
import React from 'react';

const DEFAULT_PALETTE = {
  price_action:       '#a855f7',
  bollinger:          '#00d4ff',
  ma_ribbon:          '#fbbf24',
  pivots:             '#94a3b8',
  fibonacci:          '#f472b6',
  donchian:           '#34d399',
  keltner:            '#fb923c',
  ichimoku:           '#60a5fa',
  gann:               '#facc15',
  rsi_divergence:     '#22d3ee',
  macd_signal:        '#fb7185',
  stochastic:         '#a3e635',
  atr_bands:          '#f97316',
  murrey_math:        '#8b5cf6',
  andrews_pitchfork:  '#06b6d4',
  square_of_9:        '#eab308',
  volume_profile:     '#10b981',
  harmonic_patterns:  '#ec4899',
  elliott_wave:       '#3b82f6',
  wyckoff_phases:     '#f59e0b',
  smc_order_blocks:   '#84cc16',
  fair_value_gaps:    '#14b8a6',
  avwap:              '#d946ef',
};

function colorFor(name, palette) {
  return (palette && palette[name]) || DEFAULT_PALETTE[name] || '#cbd5e1';
}

export default function TheorySelector({
  theories = [],
  selected = [],
  onChange,
  max = 5,
  palette,
}) {
  const sel = new Set(selected);

  function toggle(name) {
    if (sel.has(name)) {
      sel.delete(name);
    } else {
      if (sel.size >= max) return;   // soft cap
      sel.add(name);
    }
    onChange && onChange(Array.from(sel));
  }

  function clearAll() {
    onChange && onChange([]);
  }

  if (!theories.length) {
    return (
      <div className="v2-ts v2-ts--empty">
        <span className="dim">No theories available (waiting for /theories).</span>
        <style>{`
          .v2-ts--empty {
            padding: 12px;
            font-size: 12px;
            color: var(--text-tertiary);
            background: var(--bg-tertiary);
            border: 1px dashed var(--border-default);
            border-radius: var(--radius-md);
          }
        `}</style>
      </div>
    );
  }

  return (
    <div className="v2-ts">
      <div className="v2-ts__head">
        <span className="v2-ts__label">overlay theories</span>
        <span className="v2-ts__count mono">
          {selected.length}/{max}
        </span>
        {selected.length > 0 && (
          <button type="button"
                  className="v2-ts__clear"
                  onClick={clearAll}>
            clear
          </button>
        )}
      </div>
      <div className="v2-ts__chips">
        {theories.map((t) => {
          const isOn = sel.has(t.name);
          const c = colorFor(t.name, palette);
          const disabled = !isOn && sel.size >= max;
          return (
            <button
              key={t.name}
              type="button"
              className={`v2-ts__chip ${isOn ? 'v2-ts__chip--on' : ''} ${disabled ? 'v2-ts__chip--off' : ''}`}
              onClick={() => toggle(t.name)}
              disabled={disabled}
              title={disabled ? `Limit reached (${max}) — clear a chip first.` : t.label}
              style={isOn ? {
                borderColor: c,
                color: c,
                boxShadow: `0 0 8px ${c}55`,
              } : undefined}
            >
              <span className="v2-ts__dot"
                    style={{ background: isOn ? c : 'transparent',
                             borderColor: c }} />
              {t.label}
            </button>
          );
        })}
      </div>
      <style>{`
        .v2-ts { width: 100%; }
        .v2-ts__head {
          display: flex; align-items: center; gap: 10px;
          margin-bottom: 8px;
        }
        .v2-ts__label {
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 10px;
        }
        .v2-ts__count {
          font-size: 11px;
          color: var(--text-secondary);
        }
        .v2-ts__clear {
          margin-left: auto;
          background: transparent;
          border: 1px solid var(--border-default);
          color: var(--text-tertiary);
          font-size: 10px;
          padding: 2px 8px;
          border-radius: var(--radius-sm);
          cursor: pointer;
        }
        .v2-ts__clear:hover {
          color: var(--accent-red);
          border-color: var(--accent-red);
        }
        .v2-ts__chips {
          display: flex; flex-wrap: wrap; gap: 6px;
        }
        .v2-ts__chip {
          display: inline-flex; align-items: center; gap: 6px;
          padding: 4px 10px;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-secondary);
          font-family: var(--font-mono);
          font-size: 11px;
          border-radius: 999px;
          cursor: pointer;
          transition: all var(--transition-fast);
        }
        .v2-ts__chip:hover:not(:disabled) {
          border-color: var(--accent-cyan);
          color: var(--accent-cyan);
        }
        .v2-ts__chip--on {
          background: rgba(0, 212, 255, 0.06);
          font-weight: 600;
        }
        .v2-ts__chip--off {
          opacity: 0.45;
          cursor: not-allowed;
        }
        .v2-ts__dot {
          width: 8px; height: 8px;
          border-radius: 50%;
          border: 1px solid;
        }
      `}</style>
    </div>
  );
}
