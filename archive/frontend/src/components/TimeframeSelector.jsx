/**
 * Timeframe selector — single compact dropdown.
 *
 * Replaces the previous 10-button row. Same `value` / `onChange` API
 * so callers (StockAnalysis, TheoryStudio) don't need to change.
 */
import React from 'react';
import { TIMEFRAMES } from '../hooks/useChartTimeframe.js';

const LABELS = {
  '1D':  '1 Day',
  '1W':  '1 Week',
  '1M':  '1 Month',
  '3M':  '3 Months',
  '6M':  '6 Months',
  'YTD': 'Year to Date',
  '1Y':  '1 Year',
  '3Y':  '3 Years',
  '5Y':  '5 Years',
  'MAX': 'Max history',
};

export default function TimeframeSelector({
  value, onChange, compact = false, style = {},
}) {
  return (
    <label
      className="tb-timeframe-selector"
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
        Timeframe
      </span>
      <select
        data-testid="timeframe-select"
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
          minWidth: compact ? 72 : 96,
          cursor: 'pointer',
        }}
      >
        {TIMEFRAMES.map((tf) => (
          <option key={tf} value={tf}>
            {tf} · {LABELS[tf] || tf}
          </option>
        ))}
      </select>
    </label>
  );
}
