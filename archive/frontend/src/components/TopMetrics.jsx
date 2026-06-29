import React from 'react';
import { money, pct, num } from '../lib/format.js';

function Card({ label, value, sub, tone }) {
  return (
    <div className={`metric-card ${tone || ''}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub != null && <div className="delta">{sub}</div>}
    </div>
  );
}

export default function TopMetrics({ performance, status }) {
  const p = performance || {};
  const pnl = num(p.total_pnl);
  const pnlToday = num(p.pnl_today);
  const equity = num(p.equity_end);
  const equityChange = num(p.equity_change_pct);
  const winRate = num(p.win_rate) * 100;
  const sharpe = num(p.sharpe);
  const dd = num(p.max_drawdown_pct);

  const tone = (v) => (v > 0 ? 'positive' : v < 0 ? 'negative' : '');

  return (
    <div className="metric-strip">
      <Card
        label="Equity"
        value={money(equity)}
        sub={`${pct(equityChange, 2, { showSign: true })} since start`}
        tone={tone(equityChange)}
      />
      <Card
        label="Total P&amp;L"
        value={money(pnl, { showSign: true })}
        sub={`unrealized ${money(num(p.unrealized_pnl), { showSign: true })} · ${p.closed_count ?? 0} closed`}
        tone={tone(pnl)}
      />
      <Card
        label="Today's P&amp;L"
        value={money(pnlToday, { showSign: true })}
        sub={`${p.trades_today ?? 0} trades today`}
        tone={tone(pnlToday)}
      />
      <Card
        label="Win rate"
        value={`${winRate.toFixed(1)}%`}
        sub={`avg ${money(p.avg_gain)} / ${money(p.avg_loss)}`}
      />
      <Card
        label="Sharpe"
        value={sharpe.toFixed(2)}
        sub={`max DD ${dd.toFixed(1)}%`}
        tone={tone(sharpe)}
      />
      <Card
        label="Bot status"
        value={status?.running ? 'RUNNING' : 'STOPPED'}
        sub={status?.market_regime ? `regime · ${status.market_regime}` : status?.strategy || '—'}
        tone={status?.running ? 'positive' : ''}
      />
    </div>
  );
}
