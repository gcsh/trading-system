/**
 * Feature-Merge F1 — Throughput alert banner (original site).
 *
 * Surfaces the Phase 18-FU "smoking gun" finding directly at the top of
 * the Today page: when /learning/funnel reports submission_rate < 0.5%,
 * render a single-line plain-English warning so the operator can't miss
 * that the Brain is collapsing actionable confidence to ~0.
 *
 * Visual language matches the ORIGINAL site (var(--warn) tokens, .panel
 * class), NOT v2's deep-black/cyan-glow design. Mobile-friendly.
 *
 * Dismissible per-day via localStorage; reappears the next calendar day
 * (so the operator doesn't lose sight of an ongoing collapse).
 *
 * Single canonical source: useFunnel from hooks/swr/useFunnel.js.
 */
import React, { useState, useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useFunnel } from '../hooks/swr/useFunnel.js';

const DISMISS_KEY = 'throughputAlertDismissed';

function todayISO() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function fmtN(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString();
}

export default function ThroughputAlertBanner() {
  const { row, submissionRate, smokingGun, error } = useFunnel();
  const [dismissedToday, setDismissedToday] = useState(false);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(DISMISS_KEY);
      if (stored && stored === todayISO()) {
        setDismissedToday(true);
      }
    } catch (_) {
      // localStorage unavailable (private mode etc) — render banner.
    }
  }, []);

  const message = useMemo(() => {
    if (!row || submissionRate == null) return null;
    const subs = fmtN(row.n_submitted);
    const evals = fmtN(row.n_evaluations);
    const ratePct = (submissionRate * 100).toFixed(2);
    const windowDays = row.window_days ?? 14;
    const parts = [
      `Throughput collapse — ${subs} submissions / ${evals} evaluations (${ratePct}%) in the last ${windowDays} days.`,
    ];
    if (smokingGun && smokingGun.isAlarming) {
      const gunPct = (smokingGun.pct * 100).toFixed(1);
      parts.push(
        `Brain returns ~0 confidence on ${fmtN(smokingGun.zeroBin)} of ${fmtN(smokingGun.total)} non-HOLD votes (${gunPct}%).`,
      );
    }
    return parts.join(' ');
  }, [row, submissionRate, smokingGun]);

  if (error) return null;
  if (!row || submissionRate == null) return null;
  // Silent above 0.5% — only render on threshold cross.
  if (submissionRate >= 0.005) return null;
  if (dismissedToday) return null;

  const onDismiss = () => {
    try {
      window.localStorage.setItem(DISMISS_KEY, todayISO());
    } catch (_) {
      // best-effort
    }
    setDismissedToday(true);
  };

  return (
    <div
      data-testid="throughput-alert-banner"
      role="alert"
      className="panel"
      style={{
        padding: '10px 14px',
        marginBottom: 12,
        background: 'var(--warn-soft)',
        border: '1px solid var(--warn-border)',
        color: 'var(--warn-2)',
        fontSize: 13,
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 12,
      }}
    >
      <span aria-hidden="true" style={{ fontSize: 16, lineHeight: 1 }}>⚠</span>
      <span style={{ flex: 1, minWidth: 0, lineHeight: 1.45 }}>
        {message}
      </span>
      <Link
        to="/decision-scorecard"
        className="btn small"
        data-testid="throughput-alert-why"
        style={{ whiteSpace: 'nowrap' }}
      >
        Why? →
      </Link>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss throughput alert until tomorrow"
        data-testid="throughput-alert-dismiss"
        style={{
          background: 'transparent',
          border: 'none',
          color: 'var(--warn-2)',
          cursor: 'pointer',
          fontSize: 18,
          lineHeight: 1,
          padding: '0 4px',
        }}
      >
        ×
      </button>
    </div>
  );
}
