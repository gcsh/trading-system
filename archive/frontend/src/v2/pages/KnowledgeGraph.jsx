/* MITS Phase 19 Cluster B — Knowledge Graph v2 (/v2/knowledge).
 *
 * Per-ticker × pattern × regime matrix driven by knowledge_graph table
 * (live: /knowledge/cells + /knowledge/{ticker}/{pattern}).
 *
 * Layout:
 *   HEADER  ticker dropdown + horizon select + sample-split chips + regime filter chips
 *   ROW 1   KPI strip: cell count, avg posterior WR, best edge, hi-confidence count
 *   ROW 2   [ MAIN: KnowledgeMatrix (patterns × regimes) ] [ RIGHT: top-5 patterns sidebar ]
 *   ROW 3   Drill-in panel (when a cell is selected): primary cell + sibling/fallback chain
 *
 * Backend endpoints used (real, verified):
 *   /knowledge/cells?ticker=…&horizon=1d&sample_split=combined&limit=300
 *   /knowledge/{ticker}/{pattern}        — primary + siblings (hierarchical chain)
 *
 * No mocks, no synthetic data. EmptyState everywhere there's no data.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState, AlertBanner, KPIWidget,
} from '../../design/Components.jsx';
import KnowledgeMatrix from '../components/KnowledgeMatrix.jsx';

const DEFAULT_TICKERS = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'SPY', 'QQQ', 'AMZN', 'META', 'GOOGL', 'AMD'];
const HORIZONS = ['1d', '5d', '20d'];
const SPLITS = [
  { id: 'combined',      label: 'combined' },
  { id: 'in_sample',     label: 'in-sample' },
  { id: 'out_of_sample', label: 'out-of-sample' },
];
const ALL_REGIME = '__all__';
const KNOWN_REGIMES = ['trending_up', 'trending_down', 'choppy', 'live', 'unknown'];

function pct(v, places = 1) {
  if (v == null || !isFinite(v)) return '—';
  return `${(v * 100).toFixed(places)}%`;
}
function fmtN(n) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString();
}
function fmtAgo(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/* Composite score: posterior_wr × log(1+sample_size) — used to rank top patterns. */
function compositeScore(c) {
  if (!c) return 0;
  const wr = c.posterior_win_rate || 0;
  const n  = Math.max(0, c.sample_size || 0);
  return wr * Math.log10(n + 1);
}

export default function KnowledgeGraph() {
  const [ticker, setTicker] = useState('AAPL');
  const [horizon, setHorizon] = useState('1d');
  const [split, setSplit] = useState('combined');
  const [regimeFilter, setRegimeFilter] = useState(ALL_REGIME);
  const [cells, setCells] = useState([]);
  const [cellsErr, setCellsErr] = useState(null);
  const [cellsLoading, setCellsLoading] = useState(false);
  const [selected, setSelected] = useState(null);   // { pattern, regime, cell }
  const [drill, setDrill] = useState(null);
  const [drillErr, setDrillErr] = useState(null);
  const [drillLoading, setDrillLoading] = useState(false);

  /* ── fetch /knowledge/cells ──────────────────────────────────────── */
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    setCellsLoading(true);
    (async () => {
      try {
        const url = `/knowledge/cells?ticker=${encodeURIComponent(ticker)}`
                  + `&horizon=${encodeURIComponent(horizon)}`
                  + `&sample_split=${encodeURIComponent(split)}`
                  + `&limit=300`;
        const r = await fetch(url);
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setCells(Array.isArray(j) ? j : []);
          setCellsErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setCellsErr(e.message || 'fetch failed');
          setCells([]);
        }
      } finally {
        if (!cancelled) setCellsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [ticker, horizon, split]);

  /* ── fetch drill-in /knowledge/{ticker}/{pattern} ────────────────── */
  useEffect(() => {
    if (!selected?.pattern || !ticker) {
      setDrill(null);
      return;
    }
    let cancelled = false;
    setDrillLoading(true);
    (async () => {
      try {
        const url = `/knowledge/${encodeURIComponent(ticker)}/${encodeURIComponent(selected.pattern)}`;
        const r = await fetch(url);
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setDrill(j);
          setDrillErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setDrillErr(e.message || 'drill fetch failed');
          setDrill(null);
        }
      } finally {
        if (!cancelled) setDrillLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [ticker, selected?.pattern]);

  /* ── derive available regimes from the loaded cells ──────────────── */
  const availableRegimes = useMemo(() => {
    const s = new Set();
    for (const c of cells) if (c.regime) s.add(c.regime);
    const arr = Array.from(s);
    // Stable order: known first, then anything else alphabetically.
    arr.sort((a, b) => {
      const ia = KNOWN_REGIMES.indexOf(a);
      const ib = KNOWN_REGIMES.indexOf(b);
      if (ia >= 0 && ib >= 0) return ia - ib;
      if (ia >= 0) return -1;
      if (ib >= 0) return 1;
      return a.localeCompare(b);
    });
    return arr;
  }, [cells]);

  /* ── filtered cells by regime ─────────────────────────────────────── */
  const filteredCells = useMemo(() => {
    if (regimeFilter === ALL_REGIME) return cells;
    return cells.filter((c) => c.regime === regimeFilter);
  }, [cells, regimeFilter]);

  /* ── matrix axes: top-30 patterns by sample size ─────────────────── */
  const patternsAxis = useMemo(() => {
    const byPat = new Map();
    for (const c of filteredCells) {
      const k = c.pattern;
      if (!k) continue;
      byPat.set(k, (byPat.get(k) || 0) + (c.sample_size || 0));
    }
    return Array.from(byPat.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 30)
      .map(([p]) => p);
  }, [filteredCells]);

  const regimesAxis = useMemo(() => {
    if (regimeFilter !== ALL_REGIME) return [regimeFilter];
    return availableRegimes;
  }, [availableRegimes, regimeFilter]);

  /* ── KPI calculations ────────────────────────────────────────────── */
  const kpis = useMemo(() => {
    const arr = filteredCells;
    if (!arr.length) return null;
    const wrAvg = arr.reduce((s, c) => s + (c.posterior_win_rate || 0), 0) / arr.length;
    const nSum = arr.reduce((s, c) => s + (c.sample_size || 0), 0);
    const highConf = arr.filter((c) => c.confidence_level === 'high').length;
    const best = arr.reduce((b, c) => {
      const sc = compositeScore(c);
      return (!b || sc > b.score) ? { cell: c, score: sc } : b;
    }, null);
    return {
      count:    arr.length,
      wrAvg,
      nSum,
      highConf,
      best:     best?.cell || null,
    };
  }, [filteredCells]);

  /* ── top-5 patterns for sidebar (current ticker × regime filter) ──── */
  const topPatterns = useMemo(() => {
    const arr = filteredCells.slice();
    arr.sort((a, b) => compositeScore(b) - compositeScore(a));
    // Dedup by pattern (keep best per pattern).
    const seen = new Set();
    const out = [];
    for (const c of arr) {
      if (seen.has(c.pattern)) continue;
      seen.add(c.pattern);
      out.push(c);
      if (out.length >= 5) break;
    }
    return out;
  }, [filteredCells]);

  /* ── render ──────────────────────────────────────────────────────── */
  return (
    <div className="v2-kg">
      {/* ─── HEADER ─── */}
      <div className="v2-kg-header">
        <div className="v2-kg-header__main">
          <h1 className="v2-kg-header__title">Knowledge Graph</h1>
          <span className="dim">
            per-stock × pattern × regime posterior win-rates
          </span>
        </div>
        <div className="v2-kg-header__controls">
          <label className="v2-kg-ctrl">
            <span className="v2-kg-ctrl__l">ticker</span>
            <select className="v2-kg-ctrl__input mono"
                    value={ticker}
                    onChange={(e) => { setSelected(null); setTicker(e.target.value.toUpperCase()); }}>
              {DEFAULT_TICKERS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <label className="v2-kg-ctrl">
            <span className="v2-kg-ctrl__l">horizon</span>
            <select className="v2-kg-ctrl__input mono"
                    value={horizon}
                    onChange={(e) => { setSelected(null); setHorizon(e.target.value); }}>
              {HORIZONS.map((h) => (
                <option key={h} value={h}>{h}</option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="v2-kg-chips-row">
        <div className="v2-kg-chips">
          <span className="v2-kg-chips__label">walk-forward split:</span>
          {SPLITS.map((s) => (
            <button key={s.id}
                    type="button"
                    className={`v2-kg-chip ${split === s.id ? 'v2-kg-chip--on' : ''}`}
                    onClick={() => { setSelected(null); setSplit(s.id); }}>
              {s.label}
            </button>
          ))}
        </div>
        <div className="v2-kg-chips">
          <span className="v2-kg-chips__label">regime:</span>
          <button type="button"
                  className={`v2-kg-chip ${regimeFilter === ALL_REGIME ? 'v2-kg-chip--on' : ''}`}
                  onClick={() => setRegimeFilter(ALL_REGIME)}>
            all
          </button>
          {availableRegimes.map((r) => (
            <button key={r}
                    type="button"
                    className={`v2-kg-chip ${regimeFilter === r ? 'v2-kg-chip--on' : ''}`}
                    onClick={() => setRegimeFilter(r)}>
              {r.replaceAll('_', ' ')}
            </button>
          ))}
        </div>
      </div>

      {cellsErr && (
        <AlertBanner severity="warning">
          /knowledge/cells failed: {cellsErr}. Showing partial data.
        </AlertBanner>
      )}

      {/* ─── ROW 1: KPI strip ─── */}
      <Section title="Snapshot"
               subtitle={`${ticker} · horizon ${horizon} · split ${split}`}>
        <div className="v2-kg-kpi-row">
          <KPIWidget icon="◉"
                     label="Cells"
                     value={kpis ? fmtN(kpis.count) : '—'}
                     trend="flat"
                     trendText={`${availableRegimes.length} regimes`}
                     hint="number of (pattern, regime, vol_state, time_bucket) cells in scope" />
          <KPIWidget icon="◉"
                     label="Avg Win-Rate"
                     value={kpis ? pct(kpis.wrAvg) : '—'}
                     trend={kpis && kpis.wrAvg >= 0.55 ? 'up' : kpis && kpis.wrAvg <= 0.45 ? 'down' : 'flat'}
                     trendText={kpis ? `n=${fmtN(kpis.nSum)} total obs` : ''}
                     hint="unweighted posterior win-rate across all cells in view" />
          <KPIWidget icon="◉"
                     label="High-Confidence"
                     value={kpis ? fmtN(kpis.highConf) : '—'}
                     trend={kpis && kpis.highConf > 5 ? 'up' : 'flat'}
                     trendText="cells flagged 'high' by Wilson CI"
                     hint="cells with confidence_level == 'high'" />
          <KPIWidget icon="◉"
                     label="Best Edge"
                     value={kpis?.best ? pct(kpis.best.posterior_win_rate) : '—'}
                     trend={kpis?.best?.posterior_win_rate >= 0.6 ? 'up' : 'flat'}
                     trendText={kpis?.best ? `${kpis.best.pattern} (n=${kpis.best.sample_size})` : ''}
                     hint="single cell with highest composite score (posterior_wr × log(n))" />
        </div>
      </Section>

      {/* ─── ROW 2: Matrix + sidebar ─── */}
      <div className="v2-kg-grid v2-kg-grid--main">
        <Section title={`Pattern × Regime matrix`}
                 subtitle={cellsLoading
                   ? 'loading…'
                   : `${patternsAxis.length} patterns × ${regimesAxis.length} regimes`}>
          <Card>
            {cellsLoading && !cells.length ? (
              <EmptyState icon="…" message={`Loading knowledge cells for ${ticker}…`} />
            ) : patternsAxis.length === 0 || regimesAxis.length === 0 ? (
              <EmptyState icon="∅"
                          message={`No knowledge cells for ${ticker} at horizon ${horizon} with split=${split}.`} />
            ) : (
              <KnowledgeMatrix
                cells={filteredCells}
                patterns={patternsAxis}
                regimes={regimesAxis}
                selected={selected}
                onCellClick={(pattern, regime, cell) =>
                  setSelected({ pattern, regime, cell })} />
            )}
          </Card>
        </Section>

        <Section title="Top patterns"
                 subtitle="ranked by posterior_wr × log(n)">
          <Card>
            {topPatterns.length === 0 ? (
              <EmptyState icon="∅" message="No patterns ranked yet." />
            ) : (
              <ul className="v2-kg-top">
                {topPatterns.map((c, i) => {
                  const wr = c.posterior_win_rate;
                  const wrCls = wr >= 0.6 ? 'pos' : wr <= 0.4 ? 'neg' : 'neu';
                  return (
                    <li key={`${c.pattern}-${i}`}>
                      <button type="button"
                              className="v2-kg-top__btn"
                              onClick={() => setSelected({
                                pattern: c.pattern,
                                regime: c.regime,
                                cell: c,
                              })}>
                        <span className="v2-kg-top__rank mono">#{i + 1}</span>
                        <div className="v2-kg-top__body">
                          <div className="v2-kg-top__name mono">{c.pattern}</div>
                          <div className="v2-kg-top__meta dim">
                            {c.regime.replaceAll('_', ' ')} · n={c.sample_size}
                          </div>
                        </div>
                        <span className={`v2-kg-top__wr mono ${wrCls}`}>
                          {pct(wr)}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </Section>
      </div>

      {/* ─── ROW 3: Drill-in ─── */}
      {selected && (
        <Section
          title={`Drill-in: ${selected.pattern} × ${selected.regime.replaceAll('_', ' ')}`}
          subtitle={drillLoading ? 'loading hierarchical fallback chain…'
                    : drill?.primary_cell ? `last updated ${fmtAgo(drill.primary_cell.last_updated)} ago`
                    : ''}
          actions={
            <button type="button" className="v2-kg-close"
                    onClick={() => setSelected(null)}>
              close ✕
            </button>
          }>
          <Card>
            {drillErr && (
              <AlertBanner severity="warning">
                /knowledge/{ticker}/{selected.pattern} failed: {drillErr}.
              </AlertBanner>
            )}
            {drill?.primary_cell ? (
              <div className="v2-kg-drill">
                <div className="v2-kg-drill__primary">
                  <div className="v2-kg-drill__title">PRIMARY CELL</div>
                  <div className="v2-kg-drill__stats">
                    <Stat label="Sample size" value={fmtN(drill.primary_cell.sample_size)} mono />
                    <Stat label="Posterior WR" value={pct(drill.primary_cell.posterior_win_rate)} mono />
                    <Stat label="Wilson CI"
                          value={`[${pct(drill.primary_cell.confidence_lower)}, ${pct(drill.primary_cell.confidence_upper)}]`}
                          mono />
                    <Stat label="Avg return"
                          value={pct(drill.primary_cell.avg_return_pct, 2)}
                          mono />
                    <Stat label="Confidence"
                          value={drill.primary_cell.confidence_level || '—'}
                          mono />
                    <Stat label="Horizon"
                          value={drill.primary_cell.horizon || '—'}
                          mono />
                  </div>
                </div>

                {Array.isArray(drill.siblings) && drill.siblings.length > 0 ? (
                  <div className="v2-kg-drill__siblings">
                    <div className="v2-kg-drill__title">
                      HIERARCHICAL FALLBACK CHAIN
                      <span className="dim mono"> (top {Math.min(8, drill.siblings.length)} of {drill.siblings.length})</span>
                    </div>
                    <table className="v2-kg-drill__table">
                      <thead>
                        <tr>
                          <th>regime</th>
                          <th>split</th>
                          <th>horizon</th>
                          <th className="r">n</th>
                          <th className="r">WR</th>
                          <th className="r">CI</th>
                          <th className="r">avg ret</th>
                        </tr>
                      </thead>
                      <tbody>
                        {drill.siblings.slice(0, 8).map((s) => (
                          <tr key={s.id}>
                            <td className="mono">{s.regime}</td>
                            <td className="mono">{s.sample_split}</td>
                            <td className="mono">{s.horizon}</td>
                            <td className="r mono">{fmtN(s.sample_size)}</td>
                            <td className="r mono">{pct(s.posterior_win_rate)}</td>
                            <td className="r mono dim">
                              [{pct(s.confidence_lower, 0)}, {pct(s.confidence_upper, 0)}]
                            </td>
                            <td className="r mono">{pct(s.avg_return_pct, 2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyState icon="∅"
                              message="No sibling cells in fallback chain." />
                )}
              </div>
            ) : drillLoading ? (
              <EmptyState icon="…" message="Loading cell detail…" />
            ) : (
              <EmptyState icon="∅"
                          message={`No detail for ${ticker} / ${selected.pattern}.`} />
            )}
          </Card>
        </Section>
      )}

      <style>{`
        .v2-kg-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-end;
          gap: 16px;
          padding-bottom: 16px;
          border-bottom: 1px solid var(--border-subtle);
          margin-bottom: var(--space-4);
          flex-wrap: wrap;
        }
        .v2-kg-header__title {
          margin: 0; font-size: var(--font-size-2xl);
          font-weight: 800; letter-spacing: -0.02em;
          color: var(--text-primary);
        }
        .v2-kg-header__main { display: flex; flex-direction: column; gap: 4px; }
        .v2-kg-header__controls { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
        .v2-kg-ctrl {
          display: flex; flex-direction: column; gap: 4px;
        }
        .v2-kg-ctrl__l {
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 10px;
        }
        .v2-kg-ctrl__input {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-primary);
          padding: 6px 10px;
          font-size: 12px;
          border-radius: var(--radius-sm);
          min-width: 120px;
        }
        .v2-kg-chips-row {
          display: flex; gap: 24px; flex-wrap: wrap;
          margin-bottom: var(--space-4);
        }
        .v2-kg-chips {
          display: flex; align-items: center; gap: 6px;
          flex-wrap: wrap;
        }
        .v2-kg-chips__label {
          color: var(--text-tertiary);
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          margin-right: 4px;
        }
        .v2-kg-chip {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-secondary);
          font-family: var(--font-mono);
          font-size: 11px;
          padding: 4px 10px;
          border-radius: 999px;
          cursor: pointer;
        }
        .v2-kg-chip:hover { border-color: var(--accent-cyan); color: var(--accent-cyan); }
        .v2-kg-chip--on {
          background: var(--accent-cyan);
          color: var(--bg-primary);
          border-color: var(--accent-cyan);
          font-weight: 700;
        }
        .v2-kg-kpi-row {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: var(--space-4);
        }
        .v2-kg-grid--main {
          display: grid;
          grid-template-columns: minmax(0, 3fr) minmax(0, 1fr);
          gap: var(--space-4);
          margin-bottom: var(--space-6);
        }
        @media (max-width: 1100px) {
          .v2-kg-kpi-row { grid-template-columns: repeat(2, 1fr); }
          .v2-kg-grid--main { grid-template-columns: 1fr; }
        }
        .v2-kg-top {
          list-style: none; padding: 0; margin: 0;
          display: flex; flex-direction: column; gap: 6px;
        }
        .v2-kg-top__btn {
          display: grid;
          grid-template-columns: auto 1fr auto;
          align-items: center;
          gap: 8px;
          width: 100%;
          padding: 8px 10px;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-sm);
          color: var(--text-primary);
          cursor: pointer;
          text-align: left;
        }
        .v2-kg-top__btn:hover {
          border-color: var(--accent-cyan);
          background: var(--bg-elevated);
        }
        .v2-kg-top__rank {
          color: var(--accent-cyan);
          font-size: 12px;
          font-weight: 700;
        }
        .v2-kg-top__body { display: flex; flex-direction: column; gap: 2px; }
        .v2-kg-top__name { font-size: 12px; }
        .v2-kg-top__meta { font-size: 10px; }
        .v2-kg-top__wr { font-size: 13px; font-weight: 700; }
        .v2-kg-top__wr.pos { color: var(--accent-green); }
        .v2-kg-top__wr.neg { color: var(--accent-red); }
        .v2-kg-top__wr.neu { color: var(--accent-yellow); }
        .v2-kg-drill {
          display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
          gap: var(--space-6);
        }
        @media (max-width: 900px) {
          .v2-kg-drill { grid-template-columns: 1fr; }
        }
        .v2-kg-drill__title {
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 10px;
          margin-bottom: 8px;
        }
        .v2-kg-drill__stats {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: var(--space-3);
        }
        .v2-kg-drill__table {
          width: 100%;
          border-collapse: collapse;
          font-size: 11px;
        }
        .v2-kg-drill__table th {
          color: var(--text-tertiary);
          text-transform: uppercase;
          font-size: 10px;
          letter-spacing: 0.06em;
          text-align: left;
          padding: 6px 8px;
          border-bottom: 1px solid var(--border-subtle);
        }
        .v2-kg-drill__table th.r,
        .v2-kg-drill__table td.r { text-align: right; }
        .v2-kg-drill__table td {
          padding: 6px 8px;
          border-bottom: 1px solid var(--border-subtle);
        }
        .v2-kg-drill__table tr:hover td {
          background: var(--bg-elevated);
        }
        .v2-kg-close {
          background: transparent;
          border: 1px solid var(--border-default);
          color: var(--text-tertiary);
          padding: 4px 10px;
          font-size: 11px;
          border-radius: var(--radius-sm);
          cursor: pointer;
        }
        .v2-kg-close:hover {
          color: var(--accent-red);
          border-color: var(--accent-red);
        }
        .dim { color: var(--text-tertiary); }
      `}</style>
    </div>
  );
}
