import React from 'react';
import { money, num, pct } from '../lib/format.js';

function Row({ label, value, tone }) {
  return (
    <tr>
      <td style={{ color: 'var(--muted)' }}>{label}</td>
      <td className={`num ${tone || ''}`}>{value}</td>
    </tr>
  );
}

export default function PerformancePanel({ performance }) {
  const p = performance || {};
  const total = num(p.total_pnl);
  const today = num(p.pnl_today);
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Performance</h2>
        <span className="panel-sub">all-time</span>
      </div>
      <table>
        <tbody>
          <Row label="Total P&L" value={money(total, { showSign: true })} tone={total >= 0 ? 'pos' : 'neg'} />
          <Row label="Today's P&L" value={money(today, { showSign: true })} tone={today >= 0 ? 'pos' : 'neg'} />
          <Row label="Trades" value={`${p.trade_count ?? 0} (${p.closed_count ?? 0} closed)`} />
          <Row label="Win rate" value={pct(num(p.win_rate) * 100, 1)} />
          <Row label="Avg gain" value={money(p.avg_gain)} tone="pos" />
          <Row label="Avg loss" value={money(p.avg_loss)} tone="neg" />
          <Row label="Profit factor" value={num(p.profit_factor).toFixed(2)} />
          <Row label="Sharpe (ann.)" value={num(p.sharpe).toFixed(2)} />
          <Row label="Max drawdown" value={pct(p.max_drawdown_pct, 2)} tone="neg" />
          <Row label="Equity change" value={pct(p.equity_change_pct, 2, { showSign: true })} tone={num(p.equity_change_pct) >= 0 ? 'pos' : 'neg'} />
        </tbody>
      </table>
    </div>
  );
}
