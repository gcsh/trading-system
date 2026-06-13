/**
 * MITS Phase 6 (P6.5) — $5k paper trial scorecard.
 *
 * Single-page proof-of-life for the bot. Sources `/trial-scorecard`
 * and renders:
 *   - Big-number equity vs starting + projection pill.
 *   - Progress gauge through the 30-day window.
 *   - Predicted-vs-realized weekly bars.
 *   - Stats grid (hit rate, max drawdown, Sharpe, trades).
 *   - AI-composed (or fallback) narrative paragraph.
 */
import React, { useEffect, useState } from 'react';

function money(v) {
  const n = Number(v);
  if (Number.isNaN(n)) return '-';
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function pct(v, digits = 2) {
  const n = Number(v);
  if (Number.isNaN(n) || v == null) return '-';
  return `${(n * 100).toFixed(digits)}%`;
}

function ProgressBar({ value, max, color = 'var(--accent)' }) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return (
    <div style={{ width: '100%', height: 14, background: 'var(--panel-2)', borderRadius: 8, overflow: 'hidden' }}>
      <div style={{ width: `${ratio * 100}%`, height: '100%', background: color, transition: 'width 200ms ease' }} />
    </div>
  );
}

function ProjectionPill({ projection }) {
  const map = {
    on_track: { className: 'pill on', label: 'ON TRACK' },
    off_track: { className: 'pill warning', label: 'OFF TRACK' },
    breached: { className: 'pill danger', label: 'BREACHED' },
  };
  const meta = map[projection] || { className: 'pill info', label: (projection || 'unknown').toUpperCase() };
  return <span className={meta.className} style={{ fontWeight: 700 }}>{meta.label}</span>;
}

function LayerSplitChart({
  statisticalPnl,
  opportunisticPnl,
  statisticalWinRate,
  opportunisticWinRate,
  statisticalClosed,
  opportunisticClosed,
}) {
  // MITS Phase 7 finishing pass — two-stack chart: statistical
  // Bayesian layer vs discretionary opportunistic layer. Lets the
  // operator see at a glance which layer drove crisis-day returns
  // without blending the two layers' edges.
  const stat = Number(statisticalPnl || 0);
  const opp = Number(opportunisticPnl || 0);
  const maxAbs = Math.max(1, Math.abs(stat), Math.abs(opp));
  const statW = (Math.abs(stat) / maxAbs) * 100;
  const oppW = (Math.abs(opp) / maxAbs) * 100;
  const statColor = stat >= 0 ? 'var(--accent-2)' : 'var(--danger-2)';
  const oppColor = opp >= 0 ? 'var(--accent-2)' : 'var(--danger-2)';
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '170px 1fr 120px 130px', alignItems: 'center', gap: 10, fontSize: 12 }}>
        <span style={{ color: 'var(--muted)', fontWeight: 600 }}>Statistical Bayesian</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <div style={{ width: `${statW}%`, height: 14, background: statColor, borderRadius: 4, opacity: 0.85 }} />
          <span style={{ color: 'var(--text-soft)', fontFeatureSettings: '"tnum"' }}>{money(stat)}</span>
        </div>
        <span style={{ color: 'var(--muted)' }}>
          Win rate: {statisticalWinRate != null ? pct(statisticalWinRate, 0) : '—'}
        </span>
        <span style={{ color: 'var(--muted)' }}>{statisticalClosed || 0} trades</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '170px 1fr 120px 130px', alignItems: 'center', gap: 10, fontSize: 12 }}>
        <span style={{ color: 'var(--muted)', fontWeight: 600 }}>Opportunistic Discretionary</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <div style={{ width: `${oppW}%`, height: 14, background: oppColor, borderRadius: 4, opacity: 0.85 }} />
          <span style={{ color: 'var(--text-soft)', fontFeatureSettings: '"tnum"' }}>{money(opp)}</span>
        </div>
        <span style={{ color: 'var(--muted)' }}>
          Win rate: {opportunisticWinRate != null ? pct(opportunisticWinRate, 0) : '—'}
        </span>
        <span style={{ color: 'var(--muted)' }}>{opportunisticClosed || 0} trades</span>
      </div>
    </div>
  );
}

function WeeklyChart({ rows }) {
  if (!rows || rows.length === 0) {
    return <div style={{ color: 'var(--muted)' }}>No weeks yet.</div>;
  }
  const max = Math.max(1, ...rows.map((r) => Math.max(Math.abs(r.predicted_pnl || 0), Math.abs(r.realized_pnl || 0))));
  return (
    <div style={{ display: 'grid', gap: 6 }}>
      {rows.map((r) => {
        const pred = Number(r.predicted_pnl) || 0;
        const real = Number(r.realized_pnl) || 0;
        const predW = (Math.abs(pred) / max) * 50;
        const realW = (Math.abs(real) / max) * 50;
        return (
          <div key={r.week_start} style={{ display: 'grid', gridTemplateColumns: '110px 1fr 1fr', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <span style={{ color: 'var(--muted)' }}>{r.week_start}</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: `${predW}%`, height: 10, background: 'var(--accent)', opacity: 0.7, borderRadius: 4 }} />
              <span style={{ color: 'var(--text-soft)', fontFeatureSettings: '"tnum"' }}>{money(pred)}</span>
              <span style={{ color: 'var(--muted)' }}>predicted</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: `${realW}%`, height: 10, background: real >= 0 ? 'var(--accent-2)' : 'var(--danger-2)', borderRadius: 4 }} />
              <span style={{ color: 'var(--text-soft)', fontFeatureSettings: '"tnum"' }}>{money(real)}</span>
              <span style={{ color: 'var(--muted)' }}>realized</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StatBox({ label, value, sub }) {
  return (
    <div className="panel" style={{ padding: 12 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', letterSpacing: '0.06em', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, marginTop: 4 }}>{value}</div>
      {sub != null && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

export default function TrialScorecard() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const r = await fetch('/trial-scorecard');
        if (!r.ok) throw new Error(`/trial-scorecard → ${r.status}`);
        const body = await r.json();
        if (alive) setData(body);
      } catch (e) {
        if (alive) setErr(e.message);
      }
    }
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (err) {
    return <div className="panel" style={{ padding: 16 }}>Failed to load trial scorecard: {err}</div>;
  }
  if (!data) {
    return <div className="panel" style={{ padding: 16 }}>Loading…</div>;
  }

  const startEq = Number(data.starting_equity || 0);
  const curEq = Number(data.current_equity || 0);
  const totalPct = Number(data.total_return_pct || 0);
  const isUp = totalPct >= 0;

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div className="panel" style={{ padding: 18 }}>
        <div style={{ display: 'flex', gap: 16, alignItems: 'baseline', flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: 12, color: 'var(--muted)', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              30-day paper trial
            </div>
            <div style={{ fontSize: 32, fontWeight: 800, fontFeatureSettings: '"tnum"' }}>
              {money(curEq)}
              <span style={{ fontSize: 14, marginLeft: 10, color: isUp ? 'var(--accent-2)' : 'var(--danger-2)' }}>
                {pct(totalPct)} (start {money(startEq)})
              </span>
            </div>
          </div>
          <div style={{ flex: 1 }} />
          <ProjectionPill projection={data.projection} />
          {data.data_health && (
            <span
              title={data.data_health.tooltip}
              style={{
                marginLeft: 8,
                padding: '4px 10px',
                borderRadius: 999,
                fontSize: 11,
                fontWeight: 700,
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
                background:
                  data.data_health.status === 'green' ? 'rgba(95,201,206,0.18)'
                  : data.data_health.status === 'yellow' ? 'rgba(232,154,76,0.18)'
                  : data.data_health.status === 'red' ? 'rgba(232,96,110,0.18)'
                  : 'rgba(154,165,178,0.18)',
                color:
                  data.data_health.status === 'green' ? '#5fc9ce'
                  : data.data_health.status === 'yellow' ? '#e89a4c'
                  : data.data_health.status === 'red' ? '#e8606e'
                  : 'var(--muted)',
              }}>
              Data {data.data_health.status}
            </span>
          )}
        </div>
        <div style={{ marginTop: 14 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--muted)' }}>
            <span>Day {data.trading_days_elapsed} of {data.days_total} trading days</span>
            <span>{data.trial_start_date} → {data.trial_end_date}</span>
          </div>
          <div style={{ marginTop: 4 }}>
            <ProgressBar value={data.trading_days_elapsed} max={data.days_total} />
          </div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10 }}>
        <StatBox label="Hit rate"
                     value={data.hit_rate != null ? pct(data.hit_rate, 0) : '—'}
                     sub={`${data.high_conviction_setups_taken} taken of ${data.high_conviction_setups_total}`} />
        <StatBox label="Max drawdown"
                     value={pct(data.max_drawdown_pct, 1)}
                     sub={money(data.max_drawdown_dollars)} />
        <StatBox label="Sharpe (annualized)"
                     value={data.sharpe_ratio_estimate != null ? data.sharpe_ratio_estimate : '—'}
                     sub="from daily snapshots" />
        <StatBox label="High-conviction won"
                     value={data.high_conviction_setups_won}
                     sub={`${data.high_conviction_setups_taken} taken`} />
      </div>

      <div className="panel" style={{ padding: 16 }}>
        <div className="panel-head" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>Statistical vs Opportunistic — layer split</h3>
          <span className="panel-sub">Bayesian discipline (statistical) vs Claude-driven crisis-day discretion (opportunistic). The split shows which layer drove the trial's P&L.</span>
        </div>
        <LayerSplitChart
          statisticalPnl={data.statistical_pnl_dollars}
          opportunisticPnl={data.opportunistic_pnl_dollars}
          statisticalWinRate={data.statistical_win_rate}
          opportunisticWinRate={data.opportunistic_win_rate}
          statisticalClosed={data.statistical_trades_closed}
          opportunisticClosed={data.opportunistic_trades_closed}
        />
      </div>

      <div className="panel" style={{ padding: 16 }}>
        <div className="panel-head" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>Predicted vs realized — weekly</h3>
          <span className="panel-sub">Predicted P&L is corpus edge (posterior * sample), not a dollar forecast.</span>
        </div>
        <WeeklyChart rows={data.weekly_pnl_predicted_vs_realized} />
      </div>

      <div className="panel" style={{ padding: 16 }}>
        <h3 style={{ marginTop: 0 }}>Status narrative</h3>
        <div style={{ fontSize: 14, lineHeight: 1.5 }}>{data.narrative}</div>
      </div>
    </div>
  );
}
