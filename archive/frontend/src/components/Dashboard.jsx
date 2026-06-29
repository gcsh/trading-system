import React from 'react';

export default function Dashboard({ status }) {
  const pnl = status.daily_pnl ?? 0;
  const pnlColor = pnl >= 0 ? 'var(--accent)' : 'var(--danger)';
  const plan = status.day_plan;
  return (
    <div className="panel col-12">
      <h2>Live Metrics</h2>
      <div className="row" style={{ gap: 32 }}>
        <div>
          <div className="metric" style={{ color: pnlColor }}>${pnl.toFixed(2)}</div>
          <div className="sub">daily P&amp;L</div>
        </div>
        <div>
          <div className="metric">{status.cycles ?? 0}</div>
          <div className="sub">cycles today</div>
        </div>
        <div>
          <div className="metric">{status.recent_signals?.length ?? 0}</div>
          <div className="sub">recent signals</div>
        </div>
        <div>
          <div className="metric">{status.strategy || '—'}</div>
          <div className="sub">active strategy</div>
        </div>
        <div>
          <div className="metric">{status.market_regime || '—'}</div>
          <div className="sub">market regime</div>
        </div>
        <div>
          <div className="metric">{status.running ? 'RUNNING' : 'STOPPED'}</div>
          <div className="sub">{status.last_cycle_at ? `last cycle ${status.last_cycle_at}` : 'idle'}</div>
        </div>
      </div>

      {plan && (
        <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
          <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 6 }}>
            Day plan · {plan.reason}
          </div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="pill on">primary: {plan.primary_strategy}</span>
            {(plan.recommended_tickers || []).map((t) => (
              <span key={t} className="pill">{t}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
