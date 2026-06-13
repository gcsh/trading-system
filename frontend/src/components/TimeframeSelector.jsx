/**
 * Feature-Merge F4 — 10-button timeframe selector.
 *
 * Renders a horizontal row of 10 timeframe buttons:
 *   1D / 1W / 1M / 3M / 6M / YTD / 1Y / 3Y / 5Y / MAX
 *
 * Driven by the canonical useChartTimeframe hook so:
 *   - Same hook = same persisted state across pages.
 *   - 1W / 3Y / 5Y are served by 5d-or-all backend windows + a client
 *     trim; the wrapper above does NOT need to know that.
 *
 * Styling deliberately matches the existing in-app `btn small / primary`
 * pattern so the row blends into both StockAnalysis and TheoryStudio
 * without design changes (no v2/* imports — hard rule of this merge).
 */
import React from 'react';
import { TIMEFRAMES } from '../hooks/useChartTimeframe.js';

export default function TimeframeSelector({
  value, onChange, compact = false, style = {},
}) {
  return (
    <div
      className="tb-timeframe-selector row"
      role="tablist"
      aria-label="Chart timeframe"
      style={{
        display: 'flex',
        gap: 4,
        flexWrap: 'wrap',
        ...style,
      }}
    >
      {TIMEFRAMES.map((tf) => {
        const active = value === tf;
        return (
          <button
            key={tf}
            type="button"
            role="tab"
            aria-selected={active}
            data-tf={tf}
            data-testid={`tf-${tf}`}
            className={`btn small ${active ? 'primary' : ''}`}
            onClick={() => onChange && onChange(tf)}
            style={{
              padding: compact ? '2px 6px' : '3px 9px',
              fontSize: compact ? 10 : 11,
              minWidth: compact ? 26 : 32,
              fontWeight: active ? 700 : 500,
            }}
          >
            {tf}
          </button>
        );
      })}
    </div>
  );
}
