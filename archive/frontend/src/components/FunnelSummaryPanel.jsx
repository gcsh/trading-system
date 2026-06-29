/**
 * Feature-Merge F1 — Decision Pipeline summary panel (original site).
 *
 * Compact 5-row mini-funnel surfaced on the Today page:
 *
 *   Evaluations  →  Brain non-HOLD  →  Policy eligible
 *                →  Consensus non-abstain  →  Submitted
 *
 * Each row shows the absolute count + the pass-rate vs the previous
 * stage. The intent is to give the operator a single glance at WHERE
 * the pipeline is leaking. The full 10-stage chart lives at
 * /decision-scorecard.
 *
 * Single canonical source: useFunnel from hooks/swr/useFunnel.js.
 * Visual style matches the ORIGINAL site (`panel`, var(--…) tokens).
 */
import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useFunnel } from '../hooks/swr/useFunnel.js';

const FIVE_STAGES = [
  { key: 'watchlist_evaluated',  label: 'Evaluations',          hint: 'Tickers the engine ranged over.' },
  { key: 'brain_non_hold',       label: 'Brain non-HOLD',       hint: 'Council emitted a non-HOLD recommendation.' },
  { key: 'policy_eligible',      label: 'Policy eligible',      hint: 'Cleared the declarative policy engine.' },
  { key: 'consensus_non_abstain',label: 'Consensus non-abstain',hint: 'Consensus stance was buy or sell.' },
  { key: 'submitted',            label: 'Submitted',            hint: 'Order sent to the broker.' },
];

function fmtN(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString();
}

function fmtPct(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return `${(Number(n) * 100).toFixed(2)}%`;
}

function colorForRate(rate) {
  if (rate == null) return 'var(--muted)';
  const x = Number(rate);
  if (x >= 0.75) return 'var(--accent)';
  if (x >= 0.30) return 'var(--accent-2)';
  if (x >= 0.05) return 'var(--warn-2)';
  return 'var(--danger-2)';
}

function MiniBar({ widthPct, color }) {
  const w = Math.max(2, Math.min(100, widthPct || 0));
  return (
    <div style={{
      flex: 1,
      minWidth: 60,
      height: 10,
      background: 'var(--panel-2)',
      borderRadius: 4,
      overflow: 'hidden',
      border: '1px solid var(--border)',
    }}>
      <div style={{
        width: `${w}%`,
        height: '100%',
        background: color,
        opacity: 0.85,
      }} />
    </div>
  );
}

export default function FunnelSummaryPanel() {
  const { row, report, submissionRate, isLoading, error } = useFunnel();

  // Map the 5 stages we care about. Prefer report.stages (canonical
  // 10-stage report); fall back to the daily row if the report block
  // is missing.
  const rows = useMemo(() => {
    const byName = {};
    if (report?.stages && Array.isArray(report.stages)) {
      for (const s of report.stages) {
        if (s && s.name) byName[s.name] = s;
      }
    }
    return FIVE_STAGES.map((cfg) => {
      const st = byName[cfg.key];
      const n = st ? Number(st.n_decisions || 0) : null;
      const rate = st ? (st.pass_rate == null ? null : Number(st.pass_rate)) : null;
      return { ...cfg, n, rate };
    });
  }, [report]);

  const baseline = useMemo(() => {
    // Use the first stage with a real count as the bar baseline.
    for (const r of rows) {
      if (r.n != null && r.n > 0) return r.n;
    }
    if (row?.n_evaluations) return Number(row.n_evaluations);
    return 0;
  }, [rows, row]);

  const headSub = useMemo(() => {
    const days = row?.window_days ?? 14;
    return `Decision Pipeline (last ${days} days)`;
  }, [row]);

  // Loading shell — keep height stable so the page doesn't jump.
  if (isLoading && !row) {
    return (
      <div className="panel" data-testid="funnel-summary-panel" style={{ marginBottom: 18 }}>
        <h3 style={{ margin: 0 }}>{headSub}</h3>
        <div style={{ color: 'var(--muted)', fontSize: 13, marginTop: 8 }}>
          Loading funnel…
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="panel" data-testid="funnel-summary-panel" style={{ marginBottom: 18 }}>
        <h3 style={{ margin: 0 }}>{headSub}</h3>
        <div style={{ color: 'var(--muted)', fontSize: 13, marginTop: 8 }}>
          Funnel snapshot unavailable — {String(error.message || error)}.
        </div>
      </div>
    );
  }

  if (!row) {
    return (
      <div className="panel" data-testid="funnel-summary-panel" style={{ marginBottom: 18 }}>
        <h3 style={{ margin: 0 }}>{headSub}</h3>
        <div style={{ color: 'var(--muted)', fontSize: 13, marginTop: 8 }}>
          Funnel snapshot not yet computed today (next nightly run at 21:55 ET).
        </div>
      </div>
    );
  }

  return (
    <div className="panel" data-testid="funnel-summary-panel" style={{ marginBottom: 18 }}>
      <div className="row" style={{
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 8,
        flexWrap: 'wrap',
        marginBottom: 8,
      }}>
        <h3 style={{ margin: 0 }}>{headSub}</h3>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          {submissionRate != null
            ? `Submission rate ${(submissionRate * 100).toFixed(3)}% · `
            : ''}
          {fmtN(row.n_submitted)} submitted of {fmtN(row.n_evaluations)} evals
        </div>
      </div>

      <div style={{ display: 'grid', gap: 6 }}>
        {rows.map((r) => {
          const widthPct = baseline > 0 && r.n != null
            ? (r.n / baseline) * 100
            : 0;
          const color = colorForRate(r.rate);
          return (
            <div
              key={r.key}
              data-testid={`funnel-row-${r.key}`}
              className="row"
              style={{
                alignItems: 'center',
                gap: 10,
                fontSize: 12.5,
              }}
              title={r.hint}
            >
              <div style={{
                minWidth: 170,
                color: 'var(--text-soft)',
                fontWeight: 500,
              }}>
                {r.label}
              </div>
              <MiniBar widthPct={widthPct} color={color} />
              <div
                data-testid={`funnel-row-${r.key}-count`}
                style={{
                  minWidth: 70,
                  textAlign: 'right',
                  fontFeatureSettings: '"tnum"',
                  color: 'var(--text)',
                  fontWeight: 600,
                }}>
                {fmtN(r.n)}
              </div>
              <div style={{
                minWidth: 62,
                textAlign: 'right',
                color: 'var(--muted)',
                fontFeatureSettings: '"tnum"',
                fontSize: 11.5,
              }}>
                {fmtPct(r.rate)}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 10, fontSize: 12 }}>
        <Link
          to="/decision-scorecard"
          data-testid="funnel-summary-open-full"
          style={{ color: 'var(--accent-2)', textDecoration: 'none' }}
        >
          → Open full funnel
        </Link>
      </div>
    </div>
  );
}
