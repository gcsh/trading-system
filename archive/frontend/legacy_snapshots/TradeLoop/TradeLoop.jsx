/**
 * MITS Phase 5 (P5.6) — Trade Loop page.
 *
 * Closes the corpus → trade loop visually: every EOD prediction lined up
 * against what the bot actually did that day. Operator can audit:
 *   - which setups got traded (TRADED chip)
 *   - which got skipped + why (SKIPPED chip + skip_reason)
 *   - which never qualified to enter the cycle (NOT_TRADED chip)
 *
 * Top-of-page summary cites realized P&L on closed trades and the
 * dominant skip reasons.
 */
import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  usePredictionAccuracy, usePredictionOutcomes,
} from '../hooks/usePredictionOutcomes.js';

function todayIso() {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}

function fmtPct(v, digits = 0) {
  if (v == null || isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

function fmtMoney(v) {
  if (v == null || isNaN(v)) return '—';
  const sign = v >= 0 ? '+' : '';
  return `${sign}$${Number(v).toFixed(2)}`;
}

const OUTCOME_LABELS = {
  traded_matched: { label: 'TRADED', tone: 'success' },
  traded_diverged: { label: 'TRADED (diverged)', tone: 'warning' },
  not_traded: { label: 'NOT TRADED', tone: 'muted' },
  pending: { label: 'OPEN', tone: 'info' },
  unresolved: { label: '—', tone: 'muted' },
};

function OutcomeChip({ outcome }) {
  const info = OUTCOME_LABELS[outcome] || OUTCOME_LABELS.unresolved;
  const palette = {
    success: { bg: '#1f7a3a33', color: '#5ed47f' },
    warning: { bg: '#bf8a2733', color: '#e89a4c' },
    info:    { bg: '#2c5a8e33', color: '#5fa4d3' },
    muted:   { bg: '#44444433', color: 'var(--muted)' },
  }[info.tone] || { bg: '#44444433', color: 'var(--muted)' };
  return (
    <span style={{
      padding: '2px 8px', borderRadius: 10,
      fontSize: 11, fontWeight: 700,
      background: palette.bg, color: palette.color,
      letterSpacing: '0.04em',
    }}>{info.label}</span>
  );
}


function aggregateSkipReasons(rows) {
  const counts = {};
  for (const r of rows) {
    if (r.outcome === 'not_traded' && r.skip_reason) {
      // Snip past the "catalyst_gate:" prefix for a cleaner roll-up.
      const key = r.skip_reason.split(':')[0].trim() || r.skip_reason;
      counts[key] = (counts[key] || 0) + 1;
    }
  }
  return counts;
}


function SummaryCard({ rows }) {
  const stats = useMemo(() => {
    let total = rows.length;
    let traded = 0;
    let open = 0;
    let closedWins = 0;
    let closedLosses = 0;
    let realizedPnl = 0;
    for (const r of rows) {
      if (r.outcome === 'pending') { open++; traded++; continue; }
      if (r.outcome === 'traded_matched' || r.outcome === 'traded_diverged') {
        traded++;
        if (r.actual_pnl_dollars != null) {
          realizedPnl += Number(r.actual_pnl_dollars);
          if (r.actual_pnl_dollars > 0) closedWins++;
          else if (r.actual_pnl_dollars < 0) closedLosses++;
        }
      }
    }
    return { total, traded, open, closedWins, closedLosses, realizedPnl };
  }, [rows]);

  const skipCounts = useMemo(() => aggregateSkipReasons(rows), [rows]);
  const skipBreakdown = Object.entries(skipCounts).slice(0, 4);

  const summary = `Today predicted ${stats.total} setups. Bot acted on ${stats.traded - stats.open} (${stats.open} still open). Realized ${fmtMoney(stats.realizedPnl)} across ${stats.closedWins + stats.closedLosses} closed trades. Skipped ${stats.total - stats.traded}` +
    (skipBreakdown.length
      ? ` (${skipBreakdown.map(([k, n]) => `${k}: ${n}`).join(', ')}).`
      : '.');

  return (
    <div className="panel" style={{ padding: 14, marginBottom: 12 }}>
      <div style={{ fontSize: 14, lineHeight: 1.5 }}>{summary}</div>
    </div>
  );
}


function AccuracyStrip({ accuracy }) {
  if (!accuracy) return null;
  const high = accuracy.high_conviction_total ?? 0;
  const acted = accuracy.high_conviction_traded ?? 0;
  return (
    <div className="panel" style={{ padding: 10, marginBottom: 12, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
      <div>
        <div style={{ fontSize: 11, color: 'var(--muted)' }}>High-conviction setups (last 30d)</div>
        <div style={{ fontSize: 18, fontWeight: 700 }}>
          {acted}/{high} {high ? `(${(acted / high * 100).toFixed(0)}%)` : ''}
        </div>
      </div>
      <div>
        <div style={{ fontSize: 11, color: 'var(--muted)' }}>Closed win rate</div>
        <div style={{ fontSize: 18, fontWeight: 700 }}>
          {accuracy.closed_win_rate != null
            ? fmtPct(accuracy.closed_win_rate)
            : '—'}
          <span style={{ marginLeft: 6, fontSize: 11, color: 'var(--muted)' }}>
            {(accuracy.closed_wins ?? 0)}W / {(accuracy.closed_losses ?? 0)}L
          </span>
        </div>
      </div>
      <div>
        <div style={{ fontSize: 11, color: 'var(--muted)' }}>Realized PnL (last 30d)</div>
        <div style={{ fontSize: 18, fontWeight: 700 }}>
          {fmtMoney(accuracy.realized_pnl_dollars)}
        </div>
      </div>
    </div>
  );
}


function LoopRow({ row, rank }) {
  const sa = {
    direction: row.predicted_direction,
    strike: row.predicted_strike,
    dte: row.predicted_dte,
  };
  return (
    <tr>
      <td style={{ padding: '8px 6px', color: 'var(--muted)', fontWeight: 700 }}>
        #{rank}
      </td>
      <td style={{ padding: '8px 6px', fontWeight: 700 }}>
        <Link to={`/analysis/${encodeURIComponent(row.ticker)}`} style={{ color: 'var(--text)' }}>
          {row.ticker}
        </Link>
      </td>
      <td style={{ padding: '8px 6px', fontSize: 12, color: 'var(--text-soft)' }}>
        {sa.direction || '—'}
      </td>
      <td style={{ padding: '8px 6px', fontSize: 12 }}>
        {sa.strike != null ? sa.strike : '—'}
      </td>
      <td style={{ padding: '8px 6px', fontSize: 12 }}>
        {row.posterior != null ? `${(row.posterior * 100).toFixed(0)}%` : '—'}
        <span style={{ marginLeft: 4, color: 'var(--muted)', fontSize: 10 }}>
          N={row.sample_size ?? '?'}
        </span>
      </td>
      <td style={{ padding: '8px 6px' }}>
        <OutcomeChip outcome={row.outcome} />
      </td>
      <td style={{ padding: '8px 6px', fontSize: 12 }}>
        {row.actual_pnl_dollars != null ? fmtMoney(row.actual_pnl_dollars) : '—'}
      </td>
      <td style={{ padding: '8px 6px', fontSize: 11, color: 'var(--muted)', maxWidth: 220 }}>
        {row.skip_reason || ''}
      </td>
      <td style={{ padding: '8px 6px' }}>
        <Link
          to={`/tomorrow?date=${encodeURIComponent(row.analysis_date)}`}
          className="btn small"
          style={{ textDecoration: 'none', fontSize: 11 }}
        >
          Setup
        </Link>
        {row.trade_id ? (
          <Link
            to={`/trades?id=${encodeURIComponent(row.trade_id)}`}
            className="btn small"
            style={{ textDecoration: 'none', fontSize: 11, marginLeft: 4 }}
          >
            Trade
          </Link>
        ) : null}
      </td>
    </tr>
  );
}


export default function TradeLoop() {
  const [dateStr, setDateStr] = useState(todayIso());
  const { rows, count, loading, refresh } = usePredictionOutcomes(dateStr);
  const { body: accuracy } = usePredictionAccuracy('30');
  const [reconciling, setReconciling] = useState(false);

  const triggerReconcile = async () => {
    setReconciling(true);
    try {
      await fetch(`/prediction-outcomes/reconcile?date=${encodeURIComponent(dateStr)}`, {
        method: 'POST',
      });
      // Give the DB a moment then refresh.
      setTimeout(() => { refresh(); setReconciling(false); }, 600);
    } catch (e) {
      setReconciling(false);
    }
  };

  return (
    <div>
      <div className="panel-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Trade Loop</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            EOD predictions vs what the bot actually did.
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <input
            type="date"
            value={dateStr}
            onChange={(e) => setDateStr(e.target.value)}
            style={{
              padding: '6px 8px', border: '1px solid var(--border)',
              background: 'var(--panel-2)', color: 'var(--text)',
              borderRadius: 6, fontFamily: 'inherit',
            }}
          />
          <button className="btn small" disabled={reconciling}
                    onClick={triggerReconcile}>
            {reconciling ? 'Reconciling...' : 'Reconcile'}
          </button>
        </div>
      </div>

      <AccuracyStrip accuracy={accuracy} />
      <SummaryCard rows={rows} />

      {loading ? (
        <div className="panel" style={{ padding: 24 }}>Loading…</div>
      ) : count === 0 ? (
        <div className="panel" style={{ padding: 24, textAlign: 'center' }}>
          <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 6 }}>
            No reconciled predictions for {dateStr}
          </div>
          <div style={{ color: 'var(--muted)', fontSize: 13 }}>
            The nightly reconcile runs at 17:00 ET. You can trigger it manually with the Reconcile button above.
          </div>
        </div>
      ) : (
        <div className="panel" style={{ padding: 10, overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--border)', color: 'var(--muted)' }}>
                <th style={{ padding: '6px' }}>Rank</th>
                <th style={{ padding: '6px' }}>Ticker</th>
                <th style={{ padding: '6px' }}>Direction</th>
                <th style={{ padding: '6px' }}>Strike</th>
                <th style={{ padding: '6px' }}>Posterior</th>
                <th style={{ padding: '6px' }}>Outcome</th>
                <th style={{ padding: '6px' }}>P&amp;L</th>
                <th style={{ padding: '6px' }}>Skip reason</th>
                <th style={{ padding: '6px' }}>Links</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <LoopRow key={r.id} row={r} rank={i + 1} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
