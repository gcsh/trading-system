import React, { useEffect, useState } from 'react';

/**
 * Stage-1 Measurement Foundation widget. Polls /metrics/summary every 30s and
 * renders the canonical KPI panel: Sharpe, Sortino, max DD, expectancy, profit
 * factor, win rate, Brier, calibration error. Each KPI shows "n/a" when the
 * sample is too thin (not 0) — being honest about not-enough-data is the whole
 * point of this card.
 */

function Tile({ label, value, hint, tone }) {
  return (
    <div style={{
      background: 'var(--panel-2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '10px 12px', minWidth: 130,
    }}>
      <div style={{ fontSize: 10.5, color: 'var(--muted)',
          textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {label}
      </div>
      <div className={tone || ''} style={{
          fontWeight: 700, fontSize: 18, fontFeatureSettings: '"tnum"', marginTop: 2,
      }}>
        {value}
      </div>
      {hint && (
        <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 2 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

const fmtRatio = (v) => v == null ? 'n/a' : v.toFixed(2);
const fmtPct = (v) => v == null ? 'n/a' : `${(v * 100).toFixed(1)}%`;
const fmtMoney = (v) => v == null ? 'n/a'
  : (v >= 0 ? '+' : '') + `$${v.toFixed(2)}`;
const fmtPf = (v) => v == null ? 'n/a' : v === 'inf' ? '∞' : v.toFixed(2);
const fmtEce = (v) => v == null ? 'n/a' : v.toFixed(3);

const toneFor = (kind, v) => {
  if (v == null) return '';
  if (kind === 'pnl' || kind === 'expectancy') return v >= 0 ? 'pos' : 'neg';
  if (kind === 'sharpe' || kind === 'sortino') return v >= 1 ? 'pos' : v < 0 ? 'neg' : '';
  if (kind === 'pf') {
    const n = v === 'inf' ? Infinity : v;
    return n >= 1.5 ? 'pos' : n < 1 ? 'neg' : '';
  }
  return '';
};

export default function MetricsCard() {
  const [body, setBody] = useState(null);
  const [gates, setGates] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let active = true;
    const load = () => {
      Promise.all([
        fetch('/metrics/summary').then((r) => r.ok ? r.json() : null),
        fetch('/gates/status').then((r) => r.ok ? r.json() : null),
      ])
        .then(([d, g]) => { if (active) { if (d) setBody(d); if (g) setGates(g); setError(null); } })
        .catch((e) => { if (active) setError(String(e)); });
    };
    load();
    const id = setInterval(load, 30 * 1000);
    return () => { active = false; clearInterval(id); };
  }, []);

  // Render a placeholder while the first fetch is in flight so the card never
  // silently disappears. Same shape as the eventual card so layout doesn't jump.
  if (!body) {
    return (
      <div className="panel" style={{ padding: '12px 14px', background: 'var(--bg-elev)' }}>
        <div className="panel-head">
          <h2 style={{ margin: 0, fontSize: 14 }}>📊 Performance metrics</h2>
          <span className="panel-sub">{error ? `error: ${error}` : 'loading…'}</span>
        </div>
      </div>
    );
  }
  const d = body.data || {};
  const q = body.label_quality || {};

  const overallTone = gates?.overall === 'pass' ? 'on'
    : gates?.overall === 'fail' ? 'danger'
    : 'warn';
  const gateLabel = gates?.overall === 'pass' ? 'all gates pass'
    : gates?.overall === 'fail' ? `${gates.fail_count} gates fail`
    : `${gates?.insufficient_count || 0} gates need more data`;

  return (
    <div className="panel" style={{ padding: '12px 14px', background: 'var(--bg-elev)' }}>
      <div className="panel-head">
        <h2 style={{ margin: 0, fontSize: 14 }}>📊 Performance metrics</h2>
        <span className="panel-sub" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span>{q.closed} closed · {q.open} open · {q.with_prediction} with prediction</span>
          {gates && <span className={`pill ${overallTone}`}>{gateLabel}</span>}
        </span>
      </div>

      {!q.ok && q.warnings?.length > 0 && (
        <div style={{
          background: 'var(--warn-soft, rgba(214,158,46,0.18))',
          color: 'var(--warn, #d69e2e)',
          border: '1px solid var(--warn, #d69e2e)',
          padding: '6px 10px', borderRadius: 6, fontSize: 12, margin: '8px 0',
        }}>
          ⚠ {q.warnings[0]}
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
        <Tile label="Total P&L" value={fmtMoney(d.total_pnl)}
              tone={toneFor('pnl', d.total_pnl)} />
        <Tile label="Expectancy" value={fmtMoney(d.expectancy)}
              hint="per trade" tone={toneFor('expectancy', d.expectancy)} />
        <Tile label="Win rate" value={fmtPct(d.win_rate)} />
        <Tile label="Profit factor" value={fmtPf(d.profit_factor)}
              hint="gross win / gross loss" tone={toneFor('pf', d.profit_factor)} />
        <Tile label="Sharpe" value={fmtRatio(d.sharpe)}
              hint="annualized" tone={toneFor('sharpe', d.sharpe)} />
        <Tile label="Sortino" value={fmtRatio(d.sortino)}
              hint="downside-only" tone={toneFor('sortino', d.sortino)} />
        <Tile label="Max DD" value={fmtPct(d.max_drawdown_pct)}
              tone={d.max_drawdown_pct == null ? '' : 'neg'} />
        <Tile label="Avg win" value={fmtMoney(d.avg_win)} tone="pos" />
        <Tile label="Avg loss" value={fmtMoney(d.avg_loss)} tone="neg" />
        <Tile label="Brier"
              value={d.brier == null ? 'n/a' : d.brier.toFixed(3)}
              hint="lower is better" />
        <Tile label="Calibration err" value={fmtEce(d.calibration_error)}
              hint="|predicted − actual|" />
      </div>
    </div>
  );
}
