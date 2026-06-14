/**
 * Candle interval selector — sibling of TimeframeSelector.
 *
 * 8 explicit intervals + Auto:
 *   Auto / 1m / 5m / 15m / 30m / 1h / 4h / D / W
 *
 * Styling mirrors TimeframeSelector so the two rows read as one widget.
 */
import React from 'react';
import { INTERVALS } from '../hooks/useChartInterval.js';

export default function IntervalSelector({
  value, onChange, compact = false, style = {},
}) {
  return (
    <div
      className="tb-interval-selector row"
      role="tablist"
      aria-label="Candle interval"
      style={{
        display: 'flex',
        gap: 4,
        flexWrap: 'wrap',
        ...style,
      }}
    >
      {INTERVALS.map((iv) => {
        const active = value === iv.id;
        return (
          <button
            key={iv.id}
            type="button"
            role="tab"
            aria-selected={active}
            data-iv={iv.id}
            data-testid={`iv-${iv.id}`}
            className={`btn small ${active ? 'primary' : ''}`}
            onClick={() => onChange && onChange(iv.id)}
            style={{
              padding: compact ? '2px 6px' : '3px 9px',
              fontSize: compact ? 10 : 11,
              minWidth: compact ? 26 : 30,
              fontWeight: active ? 700 : 500,
            }}
            title={iv.id === 'auto'
              ? 'Use the default candle size for the current timeframe.'
              : `Force ${iv.label} candles.`}
          >
            {iv.label}
          </button>
        );
      })}
    </div>
  );
}
