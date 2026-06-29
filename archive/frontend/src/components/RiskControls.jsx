import React from 'react';

const FIELDS = [
  ['max_position_size_usd', 'Max position size ($)'],
  ['max_open_positions', 'Max open positions'],
  ['daily_loss_limit_usd', 'Daily loss circuit ($)'],
  ['stop_loss_pct', 'Stop loss (%)'],
  ['take_profit_pct', 'Take profit (%)'],
  ['max_cash_usage_pct', 'Max cash usage (%)'],
];

export default function RiskControls({ value, onChange }) {
  const update = (key, raw) => {
    const num = Number(raw);
    if (Number.isNaN(num)) return;
    onChange({ ...value, [key]: num });
  };

  return (
    <div className="panel col-6">
      <h2>Risk Controls</h2>
      <div className="grid" style={{ gap: 8 }}>
        {FIELDS.map(([key, label]) => (
          <div key={key} className="col-6">
            <label>{label}</label>
            <input
              type="number"
              value={value?.[key] ?? 0}
              onChange={(e) => update(key, e.target.value)}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
