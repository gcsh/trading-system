/**
 * Candle interval selector — compact dropdown.
 *
 * Same `value` / `onChange` API as before.
 */
import React from 'react';
import { INTERVALS } from '../hooks/useChartInterval.js';

const LABELS = {
  auto: 'Auto (chart default)',
  '1m':  '1 minute',
  '5m':  '5 minutes',
  '15m': '15 minutes',
  '30m': '30 minutes',
  '1h':  '1 hour',
  '4h':  '4 hours',
  '1d':  'Daily',
  '1w':  'Weekly',
};

export default function IntervalSelector({
  value, onChange, compact = false, style = {},
}) {
  return (
    <label
      className="tb-interval-selector"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: compact ? 10 : 11,
        ...style,
      }}
    >
      <span style={{
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
        color: 'var(--muted, #8593b0)',
        fontWeight: 700,
        fontSize: compact ? 9 : 10,
      }}>
        Candles
      </span>
      <select
        data-testid="interval-select"
        value={value}
        onChange={(e) => onChange && onChange(e.target.value)}
        style={{
          background: 'var(--bg-secondary, #0d111f)',
          border: '1px solid var(--border-subtle, #2a3349)',
          color: 'var(--text-primary, #e6edf3)',
          padding: compact ? '2px 6px' : '4px 8px',
          fontSize: compact ? 11 : 12,
          fontWeight: 600,
          borderRadius: 6,
          minWidth: compact ? 80 : 110,
          cursor: 'pointer',
        }}
      >
        {INTERVALS.map((iv) => (
          <option key={iv.id} value={iv.id}>
            {iv.label} · {LABELS[iv.id] || iv.label}
          </option>
        ))}
      </select>
    </label>
  );
}
