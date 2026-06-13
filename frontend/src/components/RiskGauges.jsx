import React from 'react';
import { num, money, pct } from '../lib/format.js';

function Gauge({ label, current, limit, formatter = (v) => v, tone }) {
  const cur = num(current);
  const lim = num(limit, 1);
  const ratio = lim > 0 ? Math.min(1, Math.abs(cur) / lim) : 0;
  const finalTone = tone || (ratio >= 0.9 ? 'danger' : ratio >= 0.6 ? 'warn' : '');
  return (
    <div className={`gauge ${finalTone}`}>
      <div className="gauge-label">
        <span>{label}</span>
        <span style={{ color: 'var(--muted)', fontFeatureSettings: '"tnum"' }}>
          {formatter(cur)} / {formatter(lim)}
        </span>
      </div>
      <div className="gauge-track">
        <div className="gauge-fill" style={{ width: `${(ratio * 100).toFixed(1)}%` }} />
      </div>
    </div>
  );
}

export default function RiskGauges({ risk, performance }) {
  const r = risk || {};
  const p = performance || {};
  const todayLoss = Math.max(0, -num(p.pnl_today));
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Risk monitor</h2>
        <span className="panel-sub">live vs configured caps</span>
      </div>
      <Gauge
        label="Daily loss"
        current={todayLoss}
        limit={r.daily_loss_limit_usd}
        formatter={money}
        tone={todayLoss >= num(r.daily_loss_limit_usd) ? 'danger' : undefined}
      />
      <Gauge
        label="Stop loss %"
        current={r.stop_loss_pct}
        limit={20}
        formatter={(v) => `${num(v).toFixed(1)}%`}
      />
      <Gauge
        label="Take profit %"
        current={r.take_profit_pct}
        limit={50}
        formatter={(v) => `${num(v).toFixed(1)}%`}
      />
      <Gauge
        label="Max cash usage %"
        current={r.max_cash_usage_pct}
        limit={100}
        formatter={(v) => `${num(v).toFixed(0)}%`}
      />
      <Gauge
        label="Open position slots"
        current={p.open_count}
        limit={r.max_open_positions}
        formatter={(v) => num(v).toFixed(0)}
      />
    </div>
  );
}
