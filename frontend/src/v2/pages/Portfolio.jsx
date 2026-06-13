/* MITS Phase 19 Cluster D — Portfolio v2 (/v2/portfolio).
 *
 * Full portfolio cockpit. Surfaces:
 *   ROW 1  KPI strip:  Equity · Today P&L · Total P&L · Win rate · Drawdown · Sharpe · Leverage
 *   ROW 2  Equity curve (drawdown shaded)
 *   ROW 3  Positions table — sortable
 *   ROW 4  Sector heatmap   |  Correlation matrix
 *   ROW 5  Stress test card (SPY-3%, SPY-10%, VIX+50%)
 *   RIGHT  Today's events rail (last cycle, opens, closes)
 *
 * Endpoints:
 *   GET /portfolio/context        equity, sector, correlations, stress
 *   GET /portfolio/equity?limit=N historical snapshots
 *   GET /portfolio/performance    trade_count / win_rate / sharpe / drawdown
 *   GET /portfolio/risk           net_beta / sector value + pct
 *   GET /paper/positions          per-position state
 *   GET /bot/status               cycles + last_cycle_at + recent_signals
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Card, Stat, Pill, Section, AlertBanner, EmptyState, KPIWidget,
} from '../../design/Components.jsx';
import EquityCurve from '../components/EquityCurve.jsx';
import SectorHeatmap from '../components/SectorHeatmap.jsx';
import CorrelationMatrix from '../components/CorrelationMatrix.jsx';

const POLL_MS = 30_000;

/* ── helpers ────────────────────────────────────────────────────────── */
function fmtMoney(v, opts = {}) {
  if (v == null || !isFinite(v)) return '—';
  const sign = opts.sign && v >= 0 ? '+' : '';
  return `${sign}$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}%`;
}
function fmtPct(v) {
  if (v == null || !isFinite(v)) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}
function fmtN(n, digits = 2) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}
function ageString(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* ── KPI strip ──────────────────────────────────────────────────────── */
function KPIStrip({ ctx, perf }) {
  const equity = ctx?.equity ?? perf?.equity_end;
  const todayPnl = perf?.pnl_today;
  const totalPnl = perf?.total_pnl;
  const winRate = perf?.win_rate;
  const maxDD = perf?.max_drawdown_pct;
  const sharpe = perf?.sharpe;
  const leverage = ctx?.leverage;
  const equityChangePct = perf?.equity_change_pct;
  return (
    <div className="v2-pf-kpi">
      <Card>
        <KPIWidget
          label="Equity"
          icon="◈"
          value={fmtMoney(equity)}
          trend={equityChangePct > 0 ? 'up' : equityChangePct < 0 ? 'down' : 'flat'}
          trendText={equityChangePct != null ? fmtPctSigned(equityChangePct) : ''}
          hint="Current portfolio value (cash + market value)"
        />
      </Card>
      <Card>
        <Stat label="Today P&L"
              value={fmtMoney(todayPnl, { sign: true })}
              delta={todayPnl > 0 ? 'up' : todayPnl < 0 ? 'down' : 'flat'}
              deltaPositive={todayPnl != null ? todayPnl >= 0 : null}
              mono
              hint="Realised + unrealised P&L change today" />
      </Card>
      <Card>
        <Stat label="Total P&L"
              value={fmtMoney(totalPnl, { sign: true })}
              delta={fmtPctSigned(equityChangePct)}
              deltaPositive={totalPnl != null ? totalPnl >= 0 : null}
              mono
              hint="Cumulative P&L since trial start" />
      </Card>
      <Card>
        <Stat label="Win Rate"
              value={winRate != null ? fmtPct(winRate) : '—'}
              mono
              hint="Closed trades · % winners" />
      </Card>
      <Card>
        <Stat label="Max Drawdown"
              value={maxDD != null ? `-${fmtN(maxDD, 1)}%` : '—'}
              mono
              deltaPositive={maxDD != null && maxDD < 20 ? true : false}
              delta={maxDD != null ? (maxDD < 20 ? 'safe' : 'review') : null}
              hint="Largest peak-to-trough decline in equity" />
      </Card>
      <Card>
        <Stat label="Sharpe"
              value={sharpe != null ? fmtN(sharpe, 2) : '—'}
              mono
              hint="Risk-adjusted return ratio (annualised)" />
      </Card>
      <Card>
        <Stat label="Leverage"
              value={leverage != null ? fmtN(leverage, 2) : '—'}
              mono
              hint="Long notional / equity" />
      </Card>
    </div>
  );
}

/* ── Positions table ────────────────────────────────────────────────── */
function PositionsTable({ positions }) {
  const [sortKey, setSortKey] = useState('unrealized_pnl');
  const [sortDir, setSortDir] = useState('desc');

  const sorted = useMemo(() => {
    if (!Array.isArray(positions)) return [];
    const arr = [...positions];
    arr.sort((a, b) => {
      const va = a[sortKey];
      const vb = b[sortKey];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') {
        return sortDir === 'asc' ? va - vb : vb - va;
      }
      return sortDir === 'asc'
        ? String(va).localeCompare(String(vb))
        : String(vb).localeCompare(String(va));
    });
    return arr;
  }, [positions, sortKey, sortDir]);

  function header(key, label, align = 'left') {
    const active = sortKey === key;
    return (
      <th onClick={() => {
            if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
            else { setSortKey(key); setSortDir('desc'); }
          }}
          style={{ textAlign: align, cursor: 'pointer', userSelect: 'none' }}>
        {label}
        {active && <span style={{ marginLeft: 4, opacity: 0.6 }}>{sortDir === 'asc' ? '▲' : '▼'}</span>}
      </th>
    );
  }

  if (!Array.isArray(positions) || positions.length === 0) {
    return <EmptyState icon="∅" message="No open positions." />;
  }

  return (
    <div className="v2-pf-pos">
      <table className="v2-table v2-table--striped">
        <thead>
          <tr>
            {header('ticker', 'Ticker')}
            {header('kind', 'Kind')}
            {header('quantity', 'Qty', 'right')}
            {header('avg_cost', 'Avg Cost', 'right')}
            {header('current_price', 'Price', 'right')}
            {header('market_value', 'Value', 'right')}
            {header('unrealized_pnl', 'Unreal. P&L', 'right')}
            {header('unrealized_pnl_pct', '%', 'right')}
            {header('opened_at', 'Days Held', 'right')}
            {header('entry_grade', 'Grade', 'center')}
          </tr>
        </thead>
        <tbody>
          {sorted.map(p => {
            const days = p.opened_at
              ? Math.max(0, Math.floor((Date.now() - Date.parse(p.opened_at)) / 86400000))
              : null;
            const pnlPos = p.unrealized_pnl != null && p.unrealized_pnl >= 0;
            return (
              <tr key={`${p.ticker}-${p.id}`}>
                <td>
                  <Link to={`/v2/stock/${p.ticker}`} className="v2-pf-pos__tk">
                    <span className="mono">{p.ticker}</span>
                  </Link>
                </td>
                <td><Pill tone="neutral">{p.kind}</Pill></td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtN(p.quantity, 4)}</td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtMoney(p.avg_cost)}</td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtMoney(p.current_price)}</td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtMoney(p.market_value)}</td>
                <td style={{ textAlign: 'right' }}
                    className="mono"
                    title={p.unrealized_pnl != null ? `${p.unrealized_pnl_pct?.toFixed(2)}%` : ''}>
                  <span style={{ color: p.unrealized_pnl == null ? 'var(--text-tertiary)' : pnlPos ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {fmtMoney(p.unrealized_pnl, { sign: true })}
                  </span>
                </td>
                <td style={{ textAlign: 'right' }} className="mono">
                  <span style={{ color: p.unrealized_pnl_pct == null ? 'var(--text-tertiary)' : pnlPos ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {fmtPctSigned(p.unrealized_pnl_pct)}
                  </span>
                </td>
                <td style={{ textAlign: 'right' }} className="mono">{days != null ? `${days}d` : '—'}</td>
                <td style={{ textAlign: 'center' }}>
                  {p.entry_grade
                    ? <Pill tone={p.entry_grade === 'A' ? 'success' : p.entry_grade === 'B' ? 'info' : 'warning'}>
                        {p.entry_grade}
                      </Pill>
                    : <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ── Stress test card ───────────────────────────────────────────────── */
function StressCard({ ctx }) {
  // /portfolio/context only carries SPY -3% directly. Others we derive.
  const equity = ctx?.equity || 0;
  const spy3 = ctx?.stress_spy_down_3pct_pnl;
  const spy3pct = ctx?.stress_spy_down_3pct_pct;
  // Linear-ish extrapolation as honest fallback; flagged as estimate.
  const spy10 = spy3 != null ? spy3 * (10 / 3) : null;
  const vix50 = spy3 != null ? spy3 * 0.4 : null; // rough — vol shock partial-equity

  const rows = [
    { label: 'SPY -3%', pnl: spy3, pct: spy3pct, kind: 'measured' },
    { label: 'SPY -10%', pnl: spy10, pct: spy3pct != null ? spy3pct * (10 / 3) : null, kind: 'estimate' },
    { label: 'VIX +50%', pnl: vix50, pct: spy3pct != null ? spy3pct * 0.4 : null, kind: 'estimate' },
  ];

  return (
    <Card>
      <div className="v2-pf-stress">
        <div className="v2-pf-stress__title">Stress Scenarios</div>
        {rows.every(r => r.pnl == null)
          ? <EmptyState icon="⚠" message="No stress vector returned." />
          : (
            <table className="v2-pf-stress__tbl">
              <thead>
                <tr>
                  <th>Scenario</th>
                  <th style={{ textAlign: 'right' }}>Δ Portfolio</th>
                  <th style={{ textAlign: 'right' }}>%</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td>{r.label}</td>
                    <td style={{ textAlign: 'right', color: r.pnl != null && r.pnl < 0 ? 'var(--accent-red)' : 'var(--accent-green)' }}
                        className="mono">{fmtMoney(r.pnl, { sign: true })}</td>
                    <td style={{ textAlign: 'right' }} className="mono">{fmtPctSigned(r.pct != null ? r.pct * 100 : null)}</td>
                    <td>
                      <Pill tone={r.kind === 'measured' ? 'success' : 'warning'}>
                        {r.kind}
                      </Pill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        <div className="v2-pf-stress__note">
          Measured rows come from the engine's portfolio stress vector
          (<code className="mono">/portfolio/context</code>). Estimates are linear
          extrapolations — for higher-fidelity scenarios call
          <code className="mono"> /stress/scenario/&#123;name&#125;</code>.
        </div>
      </div>
      <style>{`
        .v2-pf-stress__title {
          font-size: var(--font-size-xs);
          font-weight: 700;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin-bottom: 10px;
        }
        .v2-pf-stress__tbl {
          width: 100%;
          font-size: var(--font-size-sm);
          border-collapse: collapse;
        }
        .v2-pf-stress__tbl th,
        .v2-pf-stress__tbl td {
          padding: 6px 8px;
          border-bottom: 1px solid var(--border-subtle);
        }
        .v2-pf-stress__tbl th {
          font-size: 10px; color: var(--text-tertiary);
          text-transform: uppercase; letter-spacing: 0.06em;
        }
        .v2-pf-stress__note {
          padding-top: 10px;
          font-size: 11px;
          color: var(--text-tertiary);
          line-height: 1.5;
        }
      `}</style>
    </Card>
  );
}

/* ── Today's events rail ────────────────────────────────────────────── */
function EventsRail({ status, ctx }) {
  const signals = Array.isArray(status?.recent_signals)
    ? status.recent_signals.slice(0, 8)
    : [];
  const lastCycle = status?.last_cycle_at;
  return (
    <Card>
      <div className="v2-pf-evt">
        <div className="v2-pf-evt__title">Today's Activity</div>
        <div className="v2-pf-evt__meta mono">
          Last cycle: {ageString(lastCycle)} · {status?.cycles ?? 0} total
        </div>
        {signals.length === 0
          ? <div className="v2-pf-evt__empty">No recent signals.</div>
          : signals.map((s, i) => (
            <Link key={i} to={`/v2/stock/${s.ticker}`} className="v2-pf-evt__row">
              <div className="v2-pf-evt__row-head">
                <span className="mono v2-pf-evt__tk">{s.ticker}</span>
                <Pill tone={s.action?.includes('BUY') || s.action?.includes('SELL_CSP') ? 'info'
                          : s.action?.includes('CLOSE') ? 'warning' : 'neutral'}>
                  {s.action}
                </Pill>
              </div>
              <div className="v2-pf-evt__row-meta">
                {s.strategy && <span className="mono">{s.strategy}</span>}
                {s.confidence != null && (
                  <span className="mono"> · conf {fmtN(s.confidence, 2)}</span>
                )}
              </div>
              <div className="v2-pf-evt__row-ts mono">{ageString(s.timestamp)}</div>
            </Link>
          ))}
      </div>
      <style>{`
        .v2-pf-evt__title {
          font-size: var(--font-size-xs);
          font-weight: 700;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin-bottom: 4px;
        }
        .v2-pf-evt__meta {
          font-size: 11px;
          color: var(--text-tertiary);
          margin-bottom: 10px;
        }
        .v2-pf-evt__empty {
          color: var(--text-tertiary);
          font-size: var(--font-size-sm);
          padding: var(--space-4) 0;
          text-align: center;
        }
        .v2-pf-evt__row {
          display: block;
          padding: 8px 10px;
          margin-bottom: 6px;
          border-radius: var(--radius-md);
          background: var(--bg-secondary);
          text-decoration: none;
          color: inherit;
          border: 1px solid transparent;
          transition: border-color var(--transition-fast);
        }
        .v2-pf-evt__row:hover { border-color: var(--border-default); }
        .v2-pf-evt__row-head {
          display: flex; align-items: center; justify-content: space-between;
          gap: 6px; margin-bottom: 4px;
        }
        .v2-pf-evt__tk {
          font-weight: 700; color: var(--accent-cyan);
          font-size: var(--font-size-sm);
        }
        .v2-pf-evt__row-meta {
          font-size: 11px;
          color: var(--text-tertiary);
        }
        .v2-pf-evt__row-ts {
          font-size: 10px;
          color: var(--text-muted);
          margin-top: 2px;
        }
      `}</style>
    </Card>
  );
}

/* ── Page ───────────────────────────────────────────────────────────── */
export default function Portfolio() {
  const [ctx, setCtx] = useState(null);
  const [equity, setEquity] = useState([]);
  const [perf, setPerf] = useState(null);
  const [risk, setRisk] = useState(null);
  const [positions, setPositions] = useState([]);
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function fetchAll() {
      const safe = async (url, fb = null) => {
        try {
          const r = await fetch(url);
          if (!r.ok) throw new Error(`${url} ${r.status}`);
          const ct = r.headers.get('content-type') || '';
          if (!ct.includes('json')) throw new Error(`${url} non-JSON`);
          return await r.json();
        } catch (e) {
          return fb;
        }
      };
      const [c, e, p, ri, ps, st] = await Promise.all([
        safe('/portfolio/context'),
        safe('/portfolio/equity?limit=300', []),
        safe('/portfolio/performance'),
        safe('/portfolio/risk'),
        safe('/paper/positions', []),
        safe('/bot/status'),
      ]);
      if (cancelled) return;
      setCtx(c);
      setEquity(Array.isArray(e) ? e : []);
      setPerf(p);
      setRisk(ri);
      setPositions(Array.isArray(ps) ? ps : []);
      setStatus(st);
      setLoaded(true);
      setErr(c == null && p == null ? 'Portfolio endpoints unavailable.' : null);
    }
    fetchAll();
    const id = setInterval(fetchAll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Derive { ticker → unrealized_pnl } and { ticker → sector } for heatmap PnL aggregation.
  const pnlByTicker = useMemo(() => {
    const m = {};
    for (const p of positions || []) {
      if (p.ticker && p.unrealized_pnl != null) m[p.ticker] = Number(p.unrealized_pnl);
    }
    return m;
  }, [positions]);

  // Sector data prefers /portfolio/risk (carries value + pct), else /portfolio/context.
  const sectors = useMemo(() => {
    if (risk?.by_sector && typeof risk.by_sector === 'object'
        && Object.keys(risk.by_sector).length) {
      return risk.by_sector;
    }
    if (ctx?.by_sector && typeof ctx.by_sector === 'object') {
      return ctx.by_sector;
    }
    return {};
  }, [risk, ctx]);

  const correlation = ctx?.pairwise_correlation || {};
  // Order tickers by exposure (largest first).
  const corrTickers = useMemo(() => {
    if (!positions || !positions.length) return Object.keys(correlation);
    return [...positions]
      .filter(p => correlation[p.ticker])
      .sort((a, b) => (b.market_value || 0) - (a.market_value || 0))
      .map(p => p.ticker);
  }, [positions, correlation]);

  return (
    <div className="v2-root v2-pf">
      <Section title="Portfolio"
               subtitle={loaded ? `${positions.length} positions · ${perf?.trade_count ?? 0} total trades` : 'Loading…'}>
        {err && <AlertBanner severity="warning">{err}</AlertBanner>}

        {/* ROW 1 — KPIs */}
        <KPIStrip ctx={ctx} perf={perf} />

        {/* ROW 2 — equity + activity rail (right) */}
        <div className="v2-pf-grid v2-pf-grid--main">
          <Card>
            <div className="v2-pf-eq-head">
              <h3 className="v2-pf-h3">Equity Curve</h3>
              <span className="v2-pf-h3-sub mono">
                {equity.length} snapshots
              </span>
            </div>
            <EquityCurve data={equity} height={280} />
          </Card>
          <EventsRail status={status} ctx={ctx} />
        </div>

        {/* ROW 3 — positions */}
        <Card>
          <h3 className="v2-pf-h3">Positions</h3>
          <PositionsTable positions={positions} />
        </Card>

        {/* ROW 4 — sector + correlations */}
        <div className="v2-pf-grid v2-pf-grid--half">
          <Card>
            <h3 className="v2-pf-h3">Sector Heatmap</h3>
            <SectorHeatmap
              sectors={sectors}
              pnlByTicker={pnlByTicker}
              tickerSectors={{}}
              height={260}
            />
          </Card>
          <Card>
            <h3 className="v2-pf-h3">Correlation Matrix</h3>
            <CorrelationMatrix matrix={correlation} tickers={corrTickers} maxN={10} />
          </Card>
        </div>

        {/* ROW 5 — stress */}
        <StressCard ctx={ctx} />
      </Section>

      <style>{`
        .v2-pf { padding: var(--space-4) var(--space-6); }
        .v2-pf-kpi {
          display: grid;
          grid-template-columns: repeat(7, 1fr);
          gap: var(--space-3);
          margin-bottom: var(--space-4);
        }
        .v2-pf-grid {
          display: grid;
          gap: var(--space-4);
          margin-bottom: var(--space-4);
        }
        .v2-pf-grid--main { grid-template-columns: 2fr 1fr; }
        .v2-pf-grid--half { grid-template-columns: 1fr 1fr; }
        .v2-pf-h3 {
          font-size: var(--font-size-base);
          font-weight: 700;
          color: var(--text-primary);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin: 0 0 var(--space-3);
        }
        .v2-pf-h3-sub {
          font-size: 11px; color: var(--text-tertiary);
          margin-left: var(--space-3);
        }
        .v2-pf-eq-head { display: flex; align-items: baseline; margin-bottom: var(--space-2); }
        .v2-pf-pos { width: 100%; overflow-x: auto; }
        .v2-pf-pos__tk {
          text-decoration: none;
          color: var(--accent-cyan);
          font-weight: 700;
        }
        .v2-pf-pos__tk:hover { text-decoration: underline; }
        @media (max-width: 1280px) {
          .v2-pf-kpi { grid-template-columns: repeat(4, 1fr); }
        }
        @media (max-width: 900px) {
          .v2-pf-kpi { grid-template-columns: repeat(2, 1fr); }
          .v2-pf-grid--main,
          .v2-pf-grid--half { grid-template-columns: 1fr; }
        }
      `}</style>
    </div>
  );
}
