/* MITS Phase 19 Cluster A — Trade Journal v2 (/v2/journal).
 *
 * Full audit log of every real trade. Bloomberg-density table with
 * KPI strip, filter bar, sortable columns, pagination, right-rail
 * drill-in card. EmptyState when no rows.
 *
 * Layout:
 *   ROW 0  KPI strip — Total / Win Rate / Profit Factor / Avg PnL /
 *                       Max Trade / Best / Worst
 *   ROW 1  Filter bar — ticker · action · status · source · date range
 *   ROW 2  Main table (left ~70%) | Detail drawer (right ~30%)
 *
 * Data sources:
 *   GET /trades/list?limit=500 (we filter client-side for snappy UX)
 *   GET /trades/summary       (KPI strip)
 *   GET /trades/{id}/detail   (right-rail drawer)
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  Card, Stat, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import TradeTimelineRow from '../components/TradeTimelineRow.jsx';

const POLL_TRADES_MS = 15_000;
const PAGE_SIZE = 50;

/* ── helpers ────────────────────────────────────────────────────────── */
function fmtMoney(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x < 0 ? '-' : '';
  return `${sign}$${Math.abs(x).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v) * 100;
  return `${x.toFixed(1)}%`;
}
function fmtN(v) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toLocaleString();
}

/* ── KPI strip ──────────────────────────────────────────────────────── */
function KPIStrip({ summary, trades }) {
  const closedPnLs = (trades || [])
    .filter(t => t.pnl != null)
    .map(t => Number(t.pnl));
  const wins = closedPnLs.filter(p => p > 0);
  const losses = closedPnLs.filter(p => p < 0);
  const profitFactor = (() => {
    const gross = wins.reduce((s, x) => s + x, 0);
    const lossSum = losses.reduce((s, x) => s + Math.abs(x), 0);
    if (!lossSum) return gross > 0 ? Infinity : 0;
    return gross / lossSum;
  })();
  const best = closedPnLs.length ? Math.max(...closedPnLs) : null;
  const worst = closedPnLs.length ? Math.min(...closedPnLs) : null;

  return (
    <div className="v2-trj-kpi">
      <Card><Stat label="Total Trades"
        value={fmtN(summary?.trade_count ?? trades?.length ?? 0)}
        hint="All real-account trades (excludes resets + synthetic backfill)"
      /></Card>
      <Card><Stat label="Closed"
        value={fmtN(summary?.closed_count ?? closedPnLs.length)}
        hint="Trades with realised P&L"
      /></Card>
      <Card><Stat label="Win Rate"
        value={fmtPctSigned(summary?.win_rate)}
        deltaPositive={summary?.win_rate >= 0.5}
        delta={summary?.win_rate >= 0.5 ? 'edge' : 'edge?'}
        hint="Wins / closed trades"
      /></Card>
      <Card><Stat label="Total P&L"
        value={fmtMoney(summary?.total_pnl)}
        deltaPositive={summary?.total_pnl >= 0}
        delta={summary?.total_pnl >= 0 ? '↑' : '↓'}
        hint="Sum of realised P&L"
      /></Card>
      <Card><Stat label="Profit Factor"
        value={isFinite(profitFactor)
          ? profitFactor.toFixed(2)
          : (profitFactor === Infinity ? '∞' : '—')}
        hint="Gross wins / gross losses (>1.5 = strong edge)"
      /></Card>
      <Card><Stat label="Avg Gain"
        value={fmtMoney(summary?.avg_gain)}
        hint="Average winning trade"
      /></Card>
      <Card><Stat label="Avg Loss"
        value={fmtMoney(summary?.avg_loss)}
        hint="Average losing trade"
      /></Card>
      <Card><Stat label="Best Trade"
        value={fmtMoney(best)}
        deltaPositive={best != null && best > 0}
        hint="Largest realised P&L"
      /></Card>
      <Card><Stat label="Worst Trade"
        value={fmtMoney(worst)}
        deltaPositive={worst != null && worst >= 0}
        hint="Largest realised loss"
      /></Card>
    </div>
  );
}

/* ── Filter bar ─────────────────────────────────────────────────────── */
function FilterBar({ filters, setFilters, tickers, sources }) {
  return (
    <div className="v2-trj-filters">
      <div className="v2-trj-filter">
        <label>Ticker</label>
        <select
          value={filters.ticker}
          onChange={(e) => setFilters({ ...filters, ticker: e.target.value })}
        >
          <option value="">All</option>
          {tickers.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <div className="v2-trj-filter">
        <label>Action</label>
        <select
          value={filters.action}
          onChange={(e) => setFilters({ ...filters, action: e.target.value })}
        >
          <option value="">All</option>
          <option value="BUY">BUY</option>
          <option value="SELL">SELL</option>
        </select>
      </div>
      <div className="v2-trj-filter">
        <label>Status</label>
        <select
          value={filters.status}
          onChange={(e) => setFilters({ ...filters, status: e.target.value })}
        >
          <option value="">All</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="submitted">Submitted</option>
          <option value="failed">Failed</option>
        </select>
      </div>
      <div className="v2-trj-filter">
        <label>Source</label>
        <select
          value={filters.source}
          onChange={(e) => setFilters({ ...filters, source: e.target.value })}
        >
          <option value="">All</option>
          {sources.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      <div className="v2-trj-filter">
        <label>From</label>
        <input type="date" value={filters.from}
          onChange={(e) => setFilters({ ...filters, from: e.target.value })} />
      </div>
      <div className="v2-trj-filter">
        <label>To</label>
        <input type="date" value={filters.to}
          onChange={(e) => setFilters({ ...filters, to: e.target.value })} />
      </div>
      <div className="v2-trj-filter">
        <label>P&L Sign</label>
        <select
          value={filters.pnlSign}
          onChange={(e) => setFilters({ ...filters, pnlSign: e.target.value })}
        >
          <option value="">All</option>
          <option value="win">Wins</option>
          <option value="loss">Losses</option>
        </select>
      </div>
      <button
        type="button"
        className="v2-trj-filter__reset"
        onClick={() => setFilters({
          ticker: '', action: '', status: '', source: '',
          from: '', to: '', pnlSign: '',
        })}
      >
        Reset
      </button>
    </div>
  );
}

/* ── Right-rail trade detail drawer ─────────────────────────────────── */
function TradeDetailDrawer({ tradeId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!tradeId) { setDetail(null); return; }
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetch(`/trades/${tradeId}/detail`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(j => { if (!cancelled) { setDetail(j); setLoading(false); } })
      .catch(e => { if (!cancelled) { setErr(e.message); setLoading(false); } });
    return () => { cancelled = true; };
  }, [tradeId]);

  if (!tradeId) return null;
  return (
    <Card variant="elevated" className="v2-trj-drawer">
      <div className="v2-trj-drawer__head">
        <span className="v2-trj-drawer__title">Trade #{tradeId}</span>
        <button type="button"
                className="v2-trj-drawer__close"
                onClick={onClose}
                aria-label="Close detail">✕</button>
      </div>
      {loading && <div className="v2-trj-drawer__body">Loading…</div>}
      {err && <div className="v2-trj-drawer__body v2-stat__delta--neg">Error: {err}</div>}
      {detail && (
        <div className="v2-trj-drawer__body">
          <div className="v2-trj-drawer__row">
            <span className="v2-trj-drawer__k">Ticker</span>
            <span className="mono">{detail.trade?.ticker}</span>
          </div>
          <div className="v2-trj-drawer__row">
            <span className="v2-trj-drawer__k">Action</span>
            <span className="mono">{detail.trade?.action}</span>
          </div>
          <div className="v2-trj-drawer__row">
            <span className="v2-trj-drawer__k">Price</span>
            <span className="mono">{fmtMoney(detail.trade?.price)}</span>
          </div>
          <div className="v2-trj-drawer__row">
            <span className="v2-trj-drawer__k">Qty</span>
            <span className="mono">{detail.trade?.quantity}</span>
          </div>
          <div className="v2-trj-drawer__row">
            <span className="v2-trj-drawer__k">P&L</span>
            <span className="mono">{fmtMoney(detail.trade?.pnl)}</span>
          </div>
          <div className="v2-trj-drawer__row">
            <span className="v2-trj-drawer__k">Status</span>
            <span className="mono">{detail.trade?.status}</span>
          </div>
          {detail.decision && (
            <>
              <div className="v2-trj-drawer__sep">— Decision —</div>
              <div className="v2-trj-drawer__row">
                <span className="v2-trj-drawer__k">Grade</span>
                <span className="mono">{detail.decision.grade || '—'}</span>
              </div>
              <div className="v2-trj-drawer__row">
                <span className="v2-trj-drawer__k">Win Prob</span>
                <span className="mono">
                  {detail.decision.win_probability != null
                    ? `${(detail.decision.win_probability * 100).toFixed(1)}%`
                    : '—'}
                </span>
              </div>
              <div className="v2-trj-drawer__row">
                <span className="v2-trj-drawer__k">Regime</span>
                <span className="mono">{detail.decision.regime_label || '—'}</span>
              </div>
            </>
          )}
          {detail.execution && (
            <>
              <div className="v2-trj-drawer__sep">— Execution —</div>
              <div className="v2-trj-drawer__row">
                <span className="v2-trj-drawer__k">Expected</span>
                <span className="mono">{fmtMoney(detail.execution.expected_price)}</span>
              </div>
              <div className="v2-trj-drawer__row">
                <span className="v2-trj-drawer__k">Filled</span>
                <span className="mono">{fmtMoney(detail.execution.fill_price)}</span>
              </div>
              <div className="v2-trj-drawer__row">
                <span className="v2-trj-drawer__k">Slip (bps)</span>
                <span className="mono">{detail.execution.slippage_bps ?? '—'}</span>
              </div>
            </>
          )}
          {detail.audit && !detail.audit.ok && (
            <div className="v2-trj-drawer__sep v2-stat__delta--neg">
              ⚠ {detail.audit.violations.length} audit violation(s)
            </div>
          )}
          <div className="v2-trj-drawer__actions">
            <Link to={`/v2/decision/cockpit/${tradeId}`}
                  className="v2-trj-drawer__btn">
              Open Cockpit →
            </Link>
          </div>
        </div>
      )}
    </Card>
  );
}

/* ── Page ───────────────────────────────────────────────────────────── */
export default function TradeJournal() {
  const [sp, setSp] = useSearchParams();
  const [trades, setTrades] = useState(null);     // null=loading, []=loaded
  const [summary, setSummary] = useState(null);
  const [loadErr, setLoadErr] = useState(null);
  const [page, setPage] = useState(0);
  const [filters, setFilters] = useState({
    ticker: sp.get('ticker') || '',
    action: sp.get('action') || '',
    status: sp.get('status') || '',
    source: sp.get('source') || '',
    from: '',
    to: '',
    pnlSign: sp.get('pnl') === 'losses' ? 'loss' : sp.get('pnl') === 'wins' ? 'win' : '',
  });
  const [selectedId, setSelectedId] = useState(
    sp.get('id') ? Number(sp.get('id')) : null
  );
  const [sortKey, setSortKey] = useState('timestamp');
  const [sortDir, setSortDir] = useState('desc');

  // Fetch trades + summary
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [tRes, sRes] = await Promise.all([
          fetch('/trades/list?limit=500'),
          fetch('/trades/summary'),
        ]);
        if (!tRes.ok) throw new Error(`/trades/list HTTP ${tRes.status}`);
        const tJson = await tRes.json();
        let sJson = null;
        try { if (sRes.ok) sJson = await sRes.json(); } catch (e) { /* ok */ }
        if (cancelled) return;
        setTrades(Array.isArray(tJson) ? tJson : []);
        setSummary(sJson);
        setLoadErr(null);
      } catch (e) {
        if (!cancelled) { setLoadErr(e.message); setTrades([]); }
      }
    }
    load();
    const id = setInterval(load, POLL_TRADES_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Distinct tickers + sources for filter dropdowns
  const tickers = useMemo(() => {
    if (!trades) return [];
    return Array.from(new Set(trades.map(t => t.ticker).filter(Boolean))).sort();
  }, [trades]);
  const sources = useMemo(() => {
    if (!trades) return [];
    return Array.from(new Set(trades.map(t => t.signal_source).filter(Boolean))).sort();
  }, [trades]);

  // Apply filters
  const filtered = useMemo(() => {
    if (!trades) return [];
    return trades.filter(t => {
      if (filters.ticker && t.ticker !== filters.ticker) return false;
      if (filters.action && !String(t.action || '').toUpperCase().startsWith(filters.action))
        return false;
      if (filters.status && t.status !== filters.status) return false;
      if (filters.source && t.signal_source !== filters.source) return false;
      if (filters.pnlSign === 'win' && !(t.pnl != null && Number(t.pnl) > 0))
        return false;
      if (filters.pnlSign === 'loss' && !(t.pnl != null && Number(t.pnl) < 0))
        return false;
      if (filters.from) {
        const ts = Date.parse(t.timestamp || '');
        if (Number.isNaN(ts) || ts < Date.parse(filters.from)) return false;
      }
      if (filters.to) {
        const ts = Date.parse(t.timestamp || '');
        const toTs = Date.parse(filters.to) + 86400_000; // include day
        if (Number.isNaN(ts) || ts > toTs) return false;
      }
      return true;
    });
  }, [trades, filters]);

  // Sort
  const sorted = useMemo(() => {
    const arr = [...filtered];
    arr.sort((a, b) => {
      let av = a[sortKey];
      let bv = b[sortKey];
      if (sortKey === 'timestamp') {
        av = Date.parse(av || '') || 0;
        bv = Date.parse(bv || '') || 0;
      }
      if (av == null) av = -Infinity;
      if (bv == null) bv = -Infinity;
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return arr;
  }, [filtered, sortKey, sortDir]);

  // Reset page when filter changes
  useEffect(() => { setPage(0); }, [filters, sortKey, sortDir]);

  const pageStart = page * PAGE_SIZE;
  const paged = sorted.slice(pageStart, pageStart + PAGE_SIZE);
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));

  function onColClick(key) {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  function selectRow(id) {
    setSelectedId(id);
    const next = new URLSearchParams(sp);
    if (id) next.set('id', String(id));
    else next.delete('id');
    setSp(next, { replace: true });
  }

  return (
    <div className="v2-root v2-trj-page">
      <Section
        title="Trade Journal"
        subtitle="Every real-account trade — sortable, filterable, drillable"
        actions={
          <Pill tone="info" size="md">
            {(trades || []).length} loaded · {sorted.length} after filter
          </Pill>
        }
      >
        <KPIStrip summary={summary} trades={trades || []} />

        <Card variant="default" style={{ marginTop: 16 }}>
          <FilterBar
            filters={filters}
            setFilters={setFilters}
            tickers={tickers}
            sources={sources}
          />
        </Card>

        {loadErr && (
          <Card variant="outlined" style={{
            marginTop: 16, borderColor: 'var(--accent-red-dim)',
            color: 'var(--accent-red)',
          }}>
            Could not load trades: {loadErr}
          </Card>
        )}

        <div className="v2-trj-main" style={{ marginTop: 16 }}>
          <div className="v2-trj-table-wrap">
            {trades && trades.length === 0 && !loadErr && (
              <Card>
                <EmptyState
                  icon="◉"
                  message="No trades yet. Once the bot fires its first real signal it'll appear here."
                  action={
                    <Link to="/v2/" className="v2-trj-empty__cta">
                      Back to Mission Control →
                    </Link>
                  }
                />
              </Card>
            )}
            {sorted.length > 0 && (
              <Card>
                <table className="v2-trj-table v2-table">
                  <thead>
                    <tr>
                      <th onClick={() => onColClick('timestamp')}
                          style={{ cursor: 'pointer' }}>
                        Time {sortKey === 'timestamp' && (sortDir === 'asc' ? '▲' : '▼')}
                      </th>
                      <th onClick={() => onColClick('ticker')}
                          style={{ cursor: 'pointer' }}>
                        Instrument {sortKey === 'ticker' && (sortDir === 'asc' ? '▲' : '▼')}
                      </th>
                      <th>Action</th>
                      <th style={{ textAlign: 'right' }}>Qty</th>
                      <th onClick={() => onColClick('price')}
                          style={{ textAlign: 'right', cursor: 'pointer' }}>
                        Price {sortKey === 'price' && (sortDir === 'asc' ? '▲' : '▼')}
                      </th>
                      <th onClick={() => onColClick('pnl')}
                          style={{ textAlign: 'right', cursor: 'pointer' }}>
                        P&L {sortKey === 'pnl' && (sortDir === 'asc' ? '▲' : '▼')}
                      </th>
                      <th>Status</th>
                      <th>Source</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {paged.map(t => (
                      <TradeTimelineRow
                        key={t.id}
                        trade={t}
                        selected={selectedId === t.id}
                        onSelect={() => selectRow(selectedId === t.id ? null : t.id)}
                      />
                    ))}
                  </tbody>
                </table>
                {totalPages > 1 && (
                  <div className="v2-trj-paginate">
                    <button type="button" disabled={page === 0}
                            onClick={() => setPage(p => Math.max(0, p - 1))}>
                      ‹ Prev
                    </button>
                    <span className="mono">
                      Page {page + 1} / {totalPages}
                    </span>
                    <button type="button"
                            disabled={page >= totalPages - 1}
                            onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}>
                      Next ›
                    </button>
                  </div>
                )}
              </Card>
            )}
          </div>

          {selectedId && (
            <div className="v2-trj-detail-col">
              <TradeDetailDrawer
                tradeId={selectedId}
                onClose={() => selectRow(null)}
              />
            </div>
          )}
        </div>
      </Section>
    </div>
  );
}
