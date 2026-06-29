/**
 * MITS Phase 7.6 — Live regime banner.
 *
 * One-line top-of-page strip showing the current intraday regime,
 * which decision layer is active (statistical vs opportunistic), and
 * the headline tape numbers that triggered it. Polls
 * `/regime/intraday` every 30 s via `useIntradayRegime`.
 *
 * No emojis; color-only chip per operator rule. Plain-English text.
 */
import React from 'react';
import { useIntradayRegime } from '../hooks/useIntradayRegime.js';

const REGIME_COLORS = {
  panic: { bg: '#3a0d0d', fg: '#ffb4b4', label: 'PANIC' },
  capitulation: { bg: '#3a0d0d', fg: '#ffb4b4', label: 'CAPITULATION' },
  squeeze: { bg: '#0f3a1c', fg: '#9deebc', label: 'SQUEEZE' },
  trending_up: { bg: '#3a290a', fg: '#ffd587', label: 'TRENDING UP' },
  trending_down: { bg: '#3a290a', fg: '#ffd587', label: 'TRENDING DOWN' },
  chop: { bg: '#1a1a1f', fg: '#9a9aab', label: 'CHOP' },
  normal: { bg: '#1a1a1f', fg: '#9a9aab', label: 'NORMAL' },
};

function _fmtSince(iso) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    const hh = d.getHours().toString().padStart(2, '0');
    const mm = d.getMinutes().toString().padStart(2, '0');
    return `${hh}:${mm} ET`;
  } catch {
    return null;
  }
}

function _fmtPct(value, digits = 1) {
  if (value === null || value === undefined) return '—';
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  return `${num >= 0 ? '+' : ''}${num.toFixed(digits)}%`;
}

function _fmtNumber(value, digits = 1) {
  if (value === null || value === undefined) return '—';
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  return num.toFixed(digits);
}

export default function RegimeBanner() {
  const data = useIntradayRegime(30_000);
  if (!data) {
    return (
      <div
        style={{
          padding: '10px 14px',
          marginBottom: 12,
          background: '#1a1a1f',
          border: '1px solid #2c2c33',
          borderRadius: 8,
          color: '#7a7a8a',
          fontSize: 13,
        }}
      >
        Loading intraday regime…
      </div>
    );
  }

  const state = (data.state || 'normal').toLowerCase();
  const colors = REGIME_COLORS[state] || REGIME_COLORS.normal;
  const mode = (data.mode || 'statistical').toLowerCase();
  const modeLabel = mode === 'opportunistic' ? 'OPPORTUNISTIC' : 'STATISTICAL';
  const since = _fmtSince(data.since);
  const lastScan = _fmtSince(data.last_scan_at);
  const hypothesis = data.current_hypothesis || null;

  const vix = _fmtNumber(data.vix, 1);
  const vixChg = _fmtPct(data.vix_change_pct, 0);
  const breadth = _fmtNumber(data.breadth, 2);
  const pcr = _fmtNumber(data.put_call, 2);

  return (
    <div
      data-testid="regime-banner"
      style={{
        padding: '10px 14px',
        marginBottom: 12,
        background: colors.bg,
        border: `1px solid ${colors.fg}33`,
        borderRadius: 8,
        color: colors.fg,
        fontSize: 13,
        display: 'flex',
        flexWrap: 'wrap',
        gap: 18,
        alignItems: 'center',
      }}
    >
      <span style={{ fontWeight: 700, letterSpacing: 0.5 }}>
        Regime: {colors.label}
        {since ? ` (since ${since})` : ''}
      </span>
      <span>VIX {vix} ({vixChg})</span>
      <span>Breadth {breadth}</span>
      <span>P/C {pcr}</span>
      <span style={{ opacity: 0.85 }}>
        Bot in {modeLabel} mode.
      </span>
      {lastScan ? (
        <span style={{ opacity: 0.65 }}>
          Last scan {lastScan}
        </span>
      ) : null}
      {hypothesis && hypothesis.thesis ? (
        <span style={{ flexBasis: '100%', opacity: 0.92, fontSize: 12, marginTop: 4 }}>
          Hypothesis ({hypothesis.ticker} {hypothesis.direction}, {hypothesis.dte_bucket},
          conviction {Number(hypothesis.conviction || 0).toFixed(2)}):{' '}
          {String(hypothesis.thesis).slice(0, 220)}
        </span>
      ) : null}
    </div>
  );
}
