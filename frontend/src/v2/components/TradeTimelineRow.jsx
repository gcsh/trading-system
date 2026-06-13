/* MITS Phase 19 Cluster A — Trade Journal timeline row.
 *
 * Bloomberg-density row for the Trade Journal table: time + instrument
 * + action + qty + price + P&L + status + source + drill-in icon. Every
 * field is plain-English-safe (renders "—" on null) and the entire row
 * is clickable to push the operator to the decision cockpit for that
 * trade. Background tints by status (open=cyan, win=green, loss=red,
 * closed_by_reset=muted).
 *
 * Props:
 *   trade        — full /trades/list row
 *   selected     — boolean (when right-rail detail drawer is showing it)
 *   onSelect()   — toggle right-rail detail
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { Pill } from '../../design/Components.jsx';

function fmtMoney(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x < 0 ? '-' : '';
  return `${sign}$${Math.abs(x).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}
function fmtPnL(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x > 0 ? '+' : x < 0 ? '-' : '';
  return `${sign}$${Math.abs(x).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}
function fmtQty(v) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 4 });
}
function fmtTs(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString('en-US', {
      month: 'short', day: '2-digit', year: '2-digit',
    });
    const time = d.toLocaleTimeString('en-US', {
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
    return `${date} ${time}`;
  } catch (e) {
    return iso;
  }
}

function actionTone(action) {
  if (!action) return 'neutral';
  const a = String(action).toUpperCase();
  if (a.startsWith('BUY')) return 'success';
  if (a.startsWith('SELL')) return 'error';
  if (a === 'HOLD') return 'neutral';
  return 'info';
}
function statusTone(status) {
  if (!status) return 'neutral';
  const s = String(status).toLowerCase();
  if (s === 'open' || s === 'submitted') return 'info';
  if (s === 'closed' || s === 'filled') return 'success';
  if (s === 'failed' || s === 'rejected') return 'error';
  if (s.includes('reset')) return 'neutral';
  return 'neutral';
}
function pnlClass(pnl) {
  if (pnl == null) return '';
  if (Number(pnl) > 0) return 'v2-stat__delta--pos';
  if (Number(pnl) < 0) return 'v2-stat__delta--neg';
  return 'v2-stat__delta--flat';
}

function instrumentLabel(t) {
  if (!t) return '—';
  if (t.instrument === 'option') {
    const k = (t.option_type || '').toUpperCase().charAt(0); // C / P
    const strike = t.strike != null ? Number(t.strike).toFixed(2) : '?';
    const exp = t.expiration || '?';
    return `${t.ticker} ${k}${strike} ${exp}`;
  }
  if (t.instrument === 'spread') {
    return `${t.ticker} SPREAD`;
  }
  return t.ticker || '—';
}

export default function TradeTimelineRow({ trade, selected, onSelect }) {
  if (!trade) return null;
  const rowCls = [
    'v2-trj-row',
    selected ? 'v2-trj-row--selected' : '',
    (trade.status || '').includes('reset') ? 'v2-trj-row--muted' : '',
  ].filter(Boolean).join(' ');

  return (
    <tr className={rowCls}
        onClick={onSelect}
        style={{ cursor: 'pointer' }}>
      <td className="mono" style={{ whiteSpace: 'nowrap' }}>
        {fmtTs(trade.timestamp)}
      </td>
      <td>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <span className="mono" style={{ fontWeight: 600 }}>
            {instrumentLabel(trade)}
          </span>
          <span style={{
            fontSize: 'var(--font-size-xs)',
            color: 'var(--text-tertiary)',
          }}>
            {trade.instrument || 'stock'}
          </span>
        </div>
      </td>
      <td>
        <Pill tone={actionTone(trade.action)} size="sm">
          {trade.action || '—'}
        </Pill>
      </td>
      <td className="mono" style={{ textAlign: 'right' }}>
        {fmtQty(trade.quantity)}
      </td>
      <td className="mono" style={{ textAlign: 'right' }}>
        {fmtMoney(trade.price)}
      </td>
      <td className={`mono ${pnlClass(trade.pnl)}`}
          style={{ textAlign: 'right', fontWeight: 600 }}>
        {fmtPnL(trade.pnl)}
      </td>
      <td>
        <Pill tone={statusTone(trade.status)} size="sm">
          {trade.status || 'open'}
        </Pill>
      </td>
      <td>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <span style={{ fontSize: 'var(--font-size-xs)' }}>
            {trade.signal_source || '—'}
          </span>
          {trade.source_kind && trade.source_kind !== 'live' && (
            <Pill tone="warning" size="sm">{trade.source_kind}</Pill>
          )}
          {trade.opportunistic && (
            <Pill tone="info" size="sm">opp</Pill>
          )}
        </div>
      </td>
      <td style={{ textAlign: 'center' }}>
        <Link
          to={`/v2/decision/cockpit/${trade.id}`}
          onClick={(e) => e.stopPropagation()}
          title="Open Decision Cockpit"
          style={{
            color: 'var(--accent-cyan)',
            fontSize: 16,
            textDecoration: 'none',
          }}
        >
          →
        </Link>
      </td>
    </tr>
  );
}
