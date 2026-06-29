import React, { useEffect, useMemo, useState } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useKnowledgeCells, useCorpusStatus } from '../hooks/useKnowledge.js';

/**
 * MITS Phase 0 — Knowledge Graph browser.
 *
 * Filter chips (ticker, pattern, regime, vol_state, time_bucket, horizon)
 * + a sortable matrix table + a drill-down modal showing the 20 most
 * recent matching observations and their outcomes.
 */

const HORIZONS = ['', '5min', '30min', '60min', '1d', '5d', '20d'];
const REGIMES = ['', 'trending_up', 'trending_down', 'choppy', 'unknown'];
const VOL_STATES = ['', 'low', 'normal', 'high'];
const TIME_BUCKETS = ['', 'pre', 'open', 'morning', 'mid', 'afternoon',
                                  'close', 'post', 'rth'];
// MITS Phase 1 — walk-forward sample splits.
const SAMPLE_SPLITS = [
  { value: 'out_of_sample', label: 'out-of-sample (live)' },
  { value: 'in_sample', label: 'in-sample (training)' },
  { value: 'combined', label: 'combined' },
];

const PCT = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const FIXED_PCT = (v, n = 0) => (v == null ? '—' : `${(v * 100).toFixed(n)}%`);

function FilterRow({ filters, setFilter, options }) {
  return (
    <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
      <input
        type="text"
        placeholder="Ticker"
        value={filters.ticker || ''}
        onChange={(e) => setFilter('ticker', e.target.value.toUpperCase().trim())}
        style={{ width: 100 }}
      />
      <input
        type="text"
        placeholder="Pattern slug"
        value={filters.pattern || ''}
        onChange={(e) => setFilter('pattern', e.target.value.trim())}
        style={{ width: 180 }}
        list="patterns-datalist"
      />
      {options.patterns && (
        <datalist id="patterns-datalist">
          {options.patterns.map((p) => <option key={p} value={p} />)}
        </datalist>
      )}
      <select
        value={filters.regime || ''}
        onChange={(e) => setFilter('regime', e.target.value)}
        title="Regime"
      >
        {REGIMES.map((r) => (
          <option key={r || 'any'} value={r}>{r || 'any regime'}</option>
        ))}
      </select>
      <select
        value={filters.vol_state || ''}
        onChange={(e) => setFilter('vol_state', e.target.value)}
        title="Vol state"
      >
        {VOL_STATES.map((v) => (
          <option key={v || 'any'} value={v}>{v || 'any vol'}</option>
        ))}
      </select>
      <select
        value={filters.time_bucket || ''}
        onChange={(e) => setFilter('time_bucket', e.target.value)}
        title="Time bucket"
      >
        {TIME_BUCKETS.map((t) => (
          <option key={t || 'any'} value={t}>{t || 'any session'}</option>
        ))}
      </select>
      <select
        value={filters.horizon || ''}
        onChange={(e) => setFilter('horizon', e.target.value)}
        title="Horizon"
      >
        {HORIZONS.map((h) => (
          <option key={h || 'any'} value={h}>{h || 'any horizon'}</option>
        ))}
      </select>
      <div className="row" style={{ gap: 4, alignItems: 'center', fontSize: 12 }}
                title="Walk-forward split — out-of-sample is the live edge, in-sample is the training corpus, combined is everything.">
        {SAMPLE_SPLITS.map((sp) => {
          const active = (filters.sample_split || 'out_of_sample') === sp.value;
          return (
            <button
              key={sp.value}
              className={`btn small ${active ? 'primary' : ''}`}
              onClick={() => setFilter('sample_split', sp.value)}
              type="button"
            >
              {sp.label}
            </button>
          );
        })}
      </div>
      <label style={{ display: 'flex', gap: 6, alignItems: 'center',
                            fontSize: 12 }} title="Minimum sample size">
        min N
        <input
          type="range" min={0} max={200} step={5}
          value={filters.min_samples || 10}
          onChange={(e) => setFilter('min_samples', parseInt(e.target.value, 10))}
        />
        <span style={{ fontFeatureSettings: '"tnum"', width: 28,
                              textAlign: 'right' }}>
          {filters.min_samples ?? 10}
        </span>
      </label>
    </div>
  );
}


function CellRow({ cell, onClick }) {
  const wr = cell.win_rate;
  const post = cell.posterior_win_rate;
  const lo = cell.confidence_lower;
  const hi = cell.confidence_upper;
  return (
    <tr onClick={() => onClick(cell)}
            style={{ cursor: 'pointer' }}
            title={`Click to drill down — last updated ${cell.last_updated || '—'}`}>
      <td><strong>{cell.ticker}</strong></td>
      <td>{cell.pattern}</td>
      <td>{cell.regime}</td>
      <td>{cell.vol_state}</td>
      <td>{cell.time_bucket}</td>
      <td>{cell.horizon}</td>
      <td>
        <span className={`pill ${
          cell.sample_split === 'out_of_sample' ? 'on'
            : cell.sample_split === 'in_sample' ? 'info'
            : 'off'
        }`} style={{ fontSize: 10 }}>
          {(cell.sample_split || 'combined').replace('_', '-')}
        </span>
      </td>
      <td style={{ textAlign: 'right', fontFeatureSettings: '"tnum"' }}>
        {cell.sample_size}
      </td>
      <td style={{ textAlign: 'right', fontFeatureSettings: '"tnum"',
                          color: wr != null && wr >= 0.5 ? 'var(--accent-2)' :
                                 'var(--danger-2)' }}>
        {PCT(wr)}
      </td>
      <td style={{ textAlign: 'right', fontFeatureSettings: '"tnum"' }}>
        {PCT(post)}
      </td>
      <td style={{ textAlign: 'right', fontFeatureSettings: '"tnum"',
                          color: cell.avg_return_pct != null
                            && cell.avg_return_pct >= 0 ? 'var(--accent-2)' :
                                                                   'var(--danger-2)' }}>
        {cell.avg_return_pct != null
          ? `${(cell.avg_return_pct * 100).toFixed(2)}%` : '—'}
      </td>
      <td style={{ color: 'var(--muted)', fontSize: 11 }}>
        [{FIXED_PCT(lo)} … {FIXED_PCT(hi)}]
      </td>
    </tr>
  );
}


function CellDetailModal({ cell, onClose }) {
  const [body, setBody] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!cell) return;
    let cancelled = false;
    const url = `/knowledge/${encodeURIComponent(cell.ticker)}/${encodeURIComponent(cell.pattern)}?history_days=30`;
    fetch(url)
      .then((r) => r.ok ? r.json() : null)
      .then((b) => !cancelled && setBody(b))
      .catch((e) => !cancelled && setErr(e));
    return () => { cancelled = true; };
  }, [cell]);

  if (!cell) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{
          maxWidth: 880, width: '90%', maxHeight: '85vh', overflow: 'auto',
          padding: 20,
        }}
      >
        <div className="row" style={{ justifyContent: 'space-between',
                                                    alignItems: 'baseline' }}>
          <h2 style={{ margin: 0 }}>
            {cell.ticker} / {cell.pattern}
          </h2>
          <div className="row" style={{ gap: 6 }}>
            <a
              href={`/analysis/${encodeURIComponent(cell.ticker)}?pattern=${encodeURIComponent(cell.pattern)}`}
              className="btn small primary"
              style={{ textDecoration: 'none' }}
            >
              View on chart →
            </a>
            <button className="btn small" onClick={onClose}>Close</button>
          </div>
        </div>
        <div className="row" style={{ gap: 16, marginTop: 12, fontSize: 13 }}>
          <span>regime: <strong>{cell.regime}</strong></span>
          <span>vol: <strong>{cell.vol_state}</strong></span>
          <span>session: <strong>{cell.time_bucket}</strong></span>
          <span>horizon: <strong>{cell.horizon}</strong></span>
          <span>samples: <strong>{cell.sample_size}</strong></span>
          <span>win rate: <strong>{PCT(cell.win_rate)}</strong></span>
          <span>posterior: <strong>{PCT(cell.posterior_win_rate)}</strong></span>
        </div>
        {/* MITS Phase 1 — posterior sparkline driven by the nightly
            knowledge_graph_history snapshot. Falls back to the
            current-point svg dot when history hasn't accumulated yet. */}
        <div className="panel" style={{ marginTop: 16, padding: 12 }}>
          <div style={{ fontSize: 11, letterSpacing: '0.04em',
                              textTransform: 'uppercase',
                              color: 'var(--muted)' }}>
            Posterior win-rate · last 30 days
          </div>
          {body && Array.isArray(body.history) && body.history.length > 1 ? (
            <>
              <div style={{ height: 120, marginTop: 6 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart
                    data={body.history.map((h) => ({
                      date: h.snapshot_date,
                      posterior: h.posterior_win_rate != null
                        ? Math.round(h.posterior_win_rate * 1000) / 10 : null,
                      lower: h.confidence_lower != null
                        ? Math.round(h.confidence_lower * 1000) / 10 : null,
                      upper: h.confidence_upper != null
                        ? Math.round(h.confidence_upper * 1000) / 10 : null,
                      n: h.sample_size,
                    }))}
                    margin={{ top: 4, right: 4, bottom: 4, left: 4 }}
                  >
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3"
                                            vertical={false} />
                    <XAxis dataKey="date" hide />
                    <YAxis domain={[0, 100]} hide />
                    <Tooltip
                      formatter={(v, name) => [v == null ? '-' : `${v}%`, name]}
                      labelFormatter={(d) => (
                        body.resolution === 'weekly'
                          ? `week of ${d}`
                          : d
                      )}
                    />
                    <Area type="monotone" dataKey="upper"
                                stroke="none" fill="var(--accent-2)"
                                fillOpacity={0.10} isAnimationActive={false} />
                    <Area type="monotone" dataKey="lower"
                                stroke="none" fill="var(--panel)"
                                fillOpacity={1} isAnimationActive={false} />
                    <Line type="monotone" dataKey="posterior"
                                stroke="var(--accent-2)" strokeWidth={2}
                                dot={false} isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <div style={{ marginTop: 4, fontSize: 10,
                                  color: 'var(--muted)' }}>
                {body.history.length} {body.resolution === 'weekly'
                  ? 'weekly buckets' : 'daily snapshots'}
                {body.resolution === 'weekly'
                  ? ' · auto-bucketed for readability'
                  : ''}
              </div>
            </>
          ) : (
            <>
              <svg width="100%" height="36" viewBox="0 0 200 36"
                     style={{ marginTop: 6 }}>
                <line x1={0} x2={200} y1={18} y2={18} stroke="var(--border)"
                          strokeDasharray="3,3" />
                <line x1={0} x2={200}
                          y1={36 - (cell.confidence_upper || 0) * 36}
                          y2={36 - (cell.confidence_upper || 0) * 36}
                          stroke="var(--muted)" strokeWidth="0.5"
                          strokeDasharray="2,2" />
                <line x1={0} x2={200}
                          y1={36 - (cell.confidence_lower || 0) * 36}
                          y2={36 - (cell.confidence_lower || 0) * 36}
                          stroke="var(--muted)" strokeWidth="0.5"
                          strokeDasharray="2,2" />
                <circle
                  cx={100}
                  cy={36 - (cell.posterior_win_rate || 0.5) * 36}
                  r={4}
                  fill={cell.posterior_win_rate >= 0.5
                    ? 'var(--accent-2)' : 'var(--danger-2)'}
                />
              </svg>
              <div style={{ marginTop: 6, fontSize: 11, color: 'var(--muted)' }}>
                History accumulating — sparkline appears once 2+ nightly
                snapshots exist for this cell.
              </div>
            </>
          )}
        </div>

        <h3 style={{ marginTop: 20, marginBottom: 8 }}>
          20 most recent observations
        </h3>
        {err && <div style={{ color: 'var(--danger)' }}>Load error</div>}
        {body == null && !err && <div>Loading…</div>}
        {body && (
          <table className="table" style={{ width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>timestamp</th>
                <th>tf</th>
                <th>regime</th>
                <th>spot</th>
                <th>outcomes</th>
              </tr>
            </thead>
            <tbody>
              {(body.recent_observations || []).map((o) => (
                <tr key={o.id}>
                  <td>{o.timestamp}</td>
                  <td>{o.timeframe}</td>
                  <td>{o.regime}</td>
                  <td>{o.spot ?? '—'}</td>
                  <td>
                    {(o.outcomes || []).map((oc) => (
                      <span key={oc.horizon} className="pill"
                                style={{ marginRight: 4 }}
                                title={`entry ${oc.entry_price} → exit ${oc.exit_price}`}>
                        {oc.horizon}:{' '}
                        <span style={{ color: oc.was_winner
                          ? 'var(--accent-2)' : 'var(--danger-2)' }}>
                          {oc.return_pct != null
                            ? `${(oc.return_pct * 100).toFixed(1)}%` : '—'}
                        </span>
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {body && (body.recent_observations || []).length === 0 && (
          <div style={{ color: 'var(--muted)' }}>
            No observations stored yet for this cell.
          </div>
        )}
      </div>
    </div>
  );
}


export default function KnowledgeGraph() {
  const [filters, setFilters] = useState({
    min_samples: 10,
    // MITS Phase 1 — default to out-of-sample (live) edge.
    sample_split: 'out_of_sample',
  });
  const setFilter = (key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
  };

  const { rows, loading, refresh } = useKnowledgeCells(filters);
  const statusRows = useCorpusStatus(10000);
  const [drillCell, setDrillCell] = useState(null);
  const [sort, setSort] = useState({ key: 'sample_size', dir: 'desc' });

  // Patterns list pulled from the current rows for the datalist.
  const patternsAll = useMemo(() => {
    const set = new Set();
    for (const r of rows) set.add(r.pattern);
    return Array.from(set).sort();
  }, [rows]);

  const sorted = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) => {
      const va = a[sort.key];
      const vb = b[sort.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (va < vb) return sort.dir === 'asc' ? -1 : 1;
      if (va > vb) return sort.dir === 'asc' ? 1 : -1;
      return 0;
    });
    return arr;
  }, [rows, sort]);

  const flipSort = (key) => {
    setSort((s) => ({
      key,
      dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc',
    }));
  };

  const triggerRebuild = async () => {
    const t = filters.ticker;
    if (!t) {
      alert('Set the Ticker filter first.');
      return;
    }
    await fetch(`/knowledge/corpus/rebuild/${encodeURIComponent(t)}`,
                      { method: 'POST' });
    refresh();
  };

  return (
    <div className="grid">
      <div className="panel col-12 panel--intel">
        <div className="panel-head">
          <h2>Knowledge Graph</h2>
          <span className="panel-sub">
            historical pattern × cohort × horizon matrix
          </span>
        </div>
        <FilterRow filters={filters} setFilter={setFilter}
                          options={{ patterns: patternsAll }} />
        <div className="row" style={{ marginTop: 8, gap: 8,
                                                    color: 'var(--muted)',
                                                    fontSize: 12 }}>
          <span>{rows.length} cells</span>
          {loading && <span>(loading…)</span>}
          <button className="btn small" onClick={refresh}>Refresh</button>
          {filters.ticker && (
            <button className="btn small" onClick={triggerRebuild}>
              Rebuild corpus for {filters.ticker}
            </button>
          )}
        </div>
      </div>

      <div className="panel col-12">
        <div className="panel-head">
          <h2>Cells</h2>
          <span className="panel-sub">click a row to drill down</span>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="table" style={{ width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                <th onClick={() => flipSort('ticker')}>Ticker</th>
                <th onClick={() => flipSort('pattern')}>Pattern</th>
                <th onClick={() => flipSort('regime')}>Regime</th>
                <th onClick={() => flipSort('vol_state')}>Vol</th>
                <th onClick={() => flipSort('time_bucket')}>Session</th>
                <th onClick={() => flipSort('horizon')}>Horizon</th>
                <th onClick={() => flipSort('sample_split')}>Split</th>
                <th onClick={() => flipSort('sample_size')} style={{ textAlign: 'right' }}>N</th>
                <th onClick={() => flipSort('win_rate')} style={{ textAlign: 'right' }}>WR</th>
                <th onClick={() => flipSort('posterior_win_rate')} style={{ textAlign: 'right' }}>Posterior</th>
                <th onClick={() => flipSort('avg_return_pct')} style={{ textAlign: 'right' }}>Avg ret</th>
                <th>CI 95%</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((c) => (
                <CellRow
                  key={`${c.ticker}-${c.pattern}-${c.regime}-${c.vol_state}-${c.time_bucket}-${c.horizon}`}
                  cell={c}
                  onClick={setDrillCell}
                />
              ))}
              {sorted.length === 0 && (
                <tr>
                  <td colSpan={12} style={{ textAlign: 'center',
                                                  color: 'var(--muted)',
                                                  padding: 24 }}>
                    No cells match the current filters. Try lowering
                    "min N" or clearing the ticker filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel col-12">
        <div className="panel-head">
          <h2>Corpus status</h2>
          <span className="panel-sub">per-ticker bootstrap state</span>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="table" style={{ width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Status</th>
                <th style={{ textAlign: 'right' }}>Observations</th>
                <th style={{ textAlign: 'right' }}>Outcomes</th>
                <th style={{ textAlign: 'right' }}>Cells</th>
                <th>Last built</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {statusRows.map((r) => (
                <tr key={r.ticker}>
                  <td><strong>{r.ticker}</strong></td>
                  <td>{r.status}</td>
                  <td style={{ textAlign: 'right',
                                      fontFeatureSettings: '"tnum"' }}>
                    {r.observation_count}
                  </td>
                  <td style={{ textAlign: 'right',
                                      fontFeatureSettings: '"tnum"' }}>
                    {r.outcome_count}
                  </td>
                  <td style={{ textAlign: 'right',
                                      fontFeatureSettings: '"tnum"' }}>
                    {r.cell_count}
                  </td>
                  <td style={{ fontSize: 11 }}>{r.last_built_at || '—'}</td>
                  <td style={{ color: 'var(--danger)', fontSize: 11 }}>
                    {r.error || ''}
                  </td>
                </tr>
              ))}
              {statusRows.length === 0 && (
                <tr><td colSpan={7} style={{ color: 'var(--muted)',
                                                              padding: 18,
                                                              textAlign: 'center' }}>
                  No corpus builds yet — add a ticker to the watchlist to kick one off.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <CellDetailModal cell={drillCell} onClose={() => setDrillCell(null)} />
    </div>
  );
}
