/**
 * MITS Phase 6 (P6.4) — Weekly retrospective page.
 *
 * Pulls /retrospective?week=YYYY-MM-DD + /retrospective/list and
 * renders the recap for the operator. Defaults to the most recent
 * completed Monday.
 */
import React, { useEffect, useState } from 'react';

function money(v) {
  const n = Number(v);
  if (Number.isNaN(n)) return '-';
  const sign = n >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function pct(v, digits = 0) {
  const n = Number(v);
  if (Number.isNaN(n) || v == null) return '-';
  return `${(n * 100).toFixed(digits)}%`;
}

function FamilyBars({ rows }) {
  if (!rows || rows.length === 0) {
    return <div style={{ color: 'var(--muted)' }}>No family attribution yet.</div>;
  }
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.pnl_dollars || 0)));
  return (
    <div style={{ display: 'grid', gap: 6 }}>
      {rows.map((r) => {
        const pnl = Number(r.pnl_dollars) || 0;
        const w = (Math.abs(pnl) / max) * 100;
        const color = pnl >= 0 ? 'var(--accent-2)' : 'var(--danger-2)';
        return (
          <div key={r.key} style={{ display: 'grid', gridTemplateColumns: '180px 1fr 120px', gap: 8, alignItems: 'center', fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>{r.key}</span>
            <div style={{ background: 'var(--panel-2)', height: 10, borderRadius: 4, position: 'relative' }}>
              <div style={{ width: `${w}%`, height: '100%', background: color, borderRadius: 4 }} />
            </div>
            <span style={{ color, textAlign: 'right', fontFeatureSettings: '"tnum"' }}>
              {money(pnl)} · {r.trade_count}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function TopList({ title, rows, positive }) {
  return (
    <div className="panel" style={{ padding: 12 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{title}</div>
      <div style={{ marginTop: 8, display: 'grid', gap: 4 }}>
        {rows && rows.length > 0 ? rows.map((r) => (
          <div key={r.key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
            <span style={{ fontWeight: 600 }}>{r.key}</span>
            <span style={{
              color: positive ? 'var(--accent-2)' : 'var(--danger-2)',
              fontFeatureSettings: '"tnum"',
            }}>
              {money(r.pnl_dollars)} · {r.trade_count} {r.trade_count === 1 ? 'trade' : 'trades'}
            </span>
          </div>
        )) : (
          <div style={{ color: 'var(--muted)', fontSize: 12 }}>None this week.</div>
        )}
      </div>
    </div>
  );
}

export default function Retrospective() {
  const [week, setWeek] = useState(''); // empty → default last completed Monday
  const [data, setData] = useState(null);
  const [list, setList] = useState([]);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const url = week ? `/retrospective?week=${week}` : '/retrospective';
        const [r1, r2] = await Promise.all([
          fetch(url),
          fetch('/retrospective/list?limit=12'),
        ]);
        if (!r1.ok) throw new Error(`retrospective ${r1.status}`);
        const body = await r1.json();
        const arr = r2.ok ? await r2.json() : [];
        if (alive) {
          setData(body);
          setList(arr);
          setErr(null);
        }
      } catch (e) {
        if (alive) setErr(e.message);
      }
    }
    load();
    return () => { alive = false; };
  }, [week]);

  const rebuildNow = async () => {
    try {
      const url = week
        ? `/retrospective?week=${week}&rebuild=true`
        : '/retrospective?rebuild=true';
      const r = await fetch(url);
      if (r.ok) {
        const body = await r.json();
        setData(body);
      }
    } catch (e) {
      setErr(e.message);
    }
  };

  if (err) {
    return <div className="panel" style={{ padding: 16 }}>Failed to load retrospective: {err}</div>;
  }
  if (!data) {
    return <div className="panel" style={{ padding: 16 }}>Loading…</div>;
  }

  if (!data.present) {
    return (
      <div className="panel" style={{ padding: 18 }}>
        <h3 style={{ marginTop: 0 }}>Weekly retrospective</h3>
        <p style={{ color: 'var(--muted)' }}>{data.message || 'No retrospective stored yet.'}</p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input
            type="date"
            value={week}
            onChange={(e) => setWeek(e.target.value)}
            style={{ padding: 6 }}
          />
          <button className="btn small primary" onClick={rebuildNow}>Build now</button>
        </div>
      </div>
    );
  }

  const wrText = data.win_rate != null ? pct(data.win_rate, 0) : '—';

  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div className="panel" style={{ padding: 16 }}>
        <div className="row" style={{ gap: 12, alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>Week of {data.week_start_date}</h2>
          <span className="panel-sub">{data.week_start_date} → {data.week_end_date}</span>
          <div style={{ flex: 1 }} />
          <input
            type="date"
            value={week || data.week_start_date}
            onChange={(e) => setWeek(e.target.value)}
            style={{ padding: 6 }}
          />
          <button className="btn small" onClick={rebuildNow}>Rebuild</button>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10 }}>
        <div className="panel" style={{ padding: 14 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Realized P&L</div>
          <div style={{
            fontSize: 26, fontWeight: 800,
            color: data.realized_pnl_dollars >= 0 ? 'var(--accent-2)' : 'var(--danger-2)',
            fontFeatureSettings: '"tnum"',
          }}>
            {money(data.realized_pnl_dollars)}
          </div>
        </div>
        <div className="panel" style={{ padding: 14 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Trades</div>
          <div style={{ fontSize: 26, fontWeight: 800 }}>{data.closed_trades}</div>
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>{data.total_trades} total · {wrText} WR</div>
        </div>
        <div className="panel" style={{ padding: 14 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Catalyst gate saves</div>
          <div style={{ fontSize: 26, fontWeight: 800 }}>{data.catalyst_gate_saves_count}</div>
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>~{money(data.catalyst_gate_saves_dollars_estimated)} estimated</div>
        </div>
        <div className="panel" style={{ padding: 14 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Avg hold (min)</div>
          <div style={{ fontSize: 26, fontWeight: 800 }}>{data.avg_hold_minutes != null ? Math.round(data.avg_hold_minutes) : '—'}</div>
        </div>
      </div>

      <div className="panel" style={{ padding: 14 }}>
        <h3 style={{ marginTop: 0 }}>Family P&L attribution</h3>
        <FamilyBars rows={data.family_pnl_attribution} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
        <TopList title="Top winning tickers" rows={data.top_winning_tickers} positive />
        <TopList title="Top losing tickers" rows={data.top_losing_tickers} positive={false} />
        <TopList title="Top winning patterns" rows={data.top_winning_patterns} positive />
        <TopList title="Top losing patterns" rows={data.top_losing_patterns} positive={false} />
      </div>

      {data.summary_paragraph && (
        <div className="panel" style={{ padding: 16 }}>
          <h3 style={{ marginTop: 0 }}>
            AI summary
            {data.summary_source === 'claude'
              ? <span className="pill info" style={{ fontSize: 10, marginLeft: 8 }}>claude</span>
              : <span className="pill" style={{ fontSize: 10, marginLeft: 8 }}>fallback</span>}
          </h3>
          <div style={{ fontSize: 14, lineHeight: 1.5 }}>{data.summary_paragraph}</div>
        </div>
      )}

      {list && list.length > 0 && (
        <div className="panel" style={{ padding: 14 }}>
          <h3 style={{ marginTop: 0 }}>Prior weeks</h3>
          <div style={{ display: 'grid', gap: 6 }}>
            {list.map((r) => (
              <button
                key={r.week_start_date}
                className="btn small ghost"
                onClick={() => setWeek(r.week_start_date)}
                style={{ display: 'grid', gridTemplateColumns: '120px 1fr 1fr', alignItems: 'center', gap: 8, textAlign: 'left' }}
              >
                <span style={{ color: 'var(--muted)' }}>{r.week_start_date}</span>
                <span style={{ color: r.realized_pnl_dollars >= 0 ? 'var(--accent-2)' : 'var(--danger-2)' }}>
                  {money(r.realized_pnl_dollars)}
                </span>
                <span style={{ color: 'var(--muted)' }}>{r.closed_trades} closed · {r.win_rate != null ? pct(r.win_rate, 0) : '—'} WR</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
