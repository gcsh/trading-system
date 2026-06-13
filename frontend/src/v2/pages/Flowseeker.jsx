/* MITS Phase 19 Cluster B — Flowseeker v2 (/v2/flow).
 *
 * Live options-flow + dark-pool intel.
 *
 * Layout:
 *   HEADER     ticker + auto-refresh toggle
 *   ROW 1      KPI strip: total premium, bull/bear counts, large blocks, urgency avg
 *   ROW 2      Filter chips: ALL / SWEEPS / BLOCKS / DARK POOLS / BULLISH / BEARISH
 *   ROW 3 L    Sweep timeline (left) | FlowDepthChart for selected ticker (right)
 *   ROW 4      Per-ticker FlowIntel snapshot (dealer regime + flow profile)
 *
 * Endpoints (verified):
 *   GET /flow/{ticker}?limit=200          — per-ticker flow ticks  []
 *   GET /flow/live?limit=200              — global flow ticks      []
 *   GET /flow/summary                     — { count, bullish, bearish, net_sentiment, total_premium, high_urgency }
 *   GET /flow/darkpool?limit=200          — dark-pool prints       []
 *   GET /flowintel/{ticker}               — { dealer_positioning, flow_profile }
 *
 * Auto-polls every 10s when "live" is on.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState, AlertBanner, KPIWidget,
} from '../../design/Components.jsx';
import FlowDepthChart from '../components/FlowDepthChart.jsx';

const DEFAULT_TICKERS = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'SPY', 'QQQ', 'AMZN', 'META', 'GOOGL', 'AMD'];
const POLL_MS = 10_000;
const FILTERS = [
  { id: 'all',      label: 'ALL'       },
  { id: 'sweeps',   label: 'SWEEPS'    },
  { id: 'blocks',   label: 'BLOCKS'    },
  { id: 'dark',     label: 'DARK POOL' },
  { id: 'bullish',  label: 'BULLISH'   },
  { id: 'bearish',  label: 'BEARISH'   },
];

function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  if (x >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (x >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${Number(n).toFixed(0)}`;
}
function fmtAgo(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}
function fmtTs(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    }).format(new Date(ms)) + ' ET';
  } catch (_) {
    return new Date(ms).toISOString().slice(11, 19);
  }
}

/* ── single flow card ─────────────────────────────────────────────── */
function FlowCard({ tick }) {
  const sentiment = (tick.sentiment || '').toLowerCase();
  const sentTone = sentiment === 'bullish' ? 'success'
                 : sentiment === 'bearish' ? 'error'
                 : 'neutral';
  const ttype = (tick.trade_type || tick.type || '').toLowerCase();
  const otype = (tick.option_type || '').toLowerCase();
  const premium = Number(tick.premium || 0);
  const urgency = Number(tick.urgency || 0);
  const session = (tick.session || '').toLowerCase();
  return (
    <div className={`v2-fs-card v2-fs-card--${sentiment || 'neutral'}`}>
      <div className="v2-fs-card__head">
        <span className="v2-fs-card__sym mono">{tick.ticker || '—'}</span>
        <Pill tone={sentTone}>{(sentiment || 'neutral').toUpperCase()}</Pill>
        {ttype && (
          <Pill tone="info">{ttype.toUpperCase()}</Pill>
        )}
        <span className="v2-fs-card__time mono dim">
          {fmtTs(tick.timestamp || tick.ts)}
        </span>
      </div>
      <div className="v2-fs-card__body">
        <div className="v2-fs-card__line">
          <span className="dim mono">strike</span>
          <span className="mono">${Number(tick.strike).toFixed(tick.strike % 1 === 0 ? 0 : 2)}</span>
          <span className="dim mono">expiry</span>
          <span className="mono">{tick.expiry || '—'}</span>
          <span className="dim mono">type</span>
          <span className={`mono ${otype === 'call' ? 'pos' : otype === 'put' ? 'neg' : ''}`}>
            {otype.toUpperCase() || '—'}
          </span>
        </div>
        <div className="v2-fs-card__line">
          <span className="dim mono">premium</span>
          <span className="mono v2-fs-card__prem">${fmtBig(premium)}</span>
          <span className="dim mono">size</span>
          <span className="mono">{fmtBig(tick.size || tick.volume)}</span>
          <span className="dim mono">urgency</span>
          <span className={`mono ${urgency >= 0.7 ? 'pos' : ''}`}>
            {urgency ? `${(urgency * 100).toFixed(0)}%` : '—'}
          </span>
        </div>
        {session && (
          <div className="v2-fs-card__line">
            <span className="dim mono">session</span>
            <span className="mono">{session}</span>
          </div>
        )}
      </div>
      <style>{`
        .v2-fs-card {
          display: flex; flex-direction: column; gap: 6px;
          padding: 10px 12px;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-subtle);
          border-left: 3px solid var(--border-default);
          border-radius: var(--radius-md);
        }
        .v2-fs-card--bullish { border-left-color: var(--accent-green); }
        .v2-fs-card--bearish { border-left-color: var(--accent-red); }
        .v2-fs-card__head {
          display: flex; align-items: center; gap: 8px;
        }
        .v2-fs-card__sym {
          font-size: 14px; font-weight: 800;
          color: var(--text-primary);
        }
        .v2-fs-card__time { margin-left: auto; font-size: 11px; }
        .v2-fs-card__body {
          display: flex; flex-direction: column; gap: 4px;
          font-size: 11px;
        }
        .v2-fs-card__line {
          display: flex; gap: 8px; align-items: baseline;
        }
        .v2-fs-card__line .dim {
          color: var(--text-tertiary);
          text-transform: uppercase;
          font-size: 9px;
          letter-spacing: 0.06em;
        }
        .v2-fs-card__prem { color: var(--accent-cyan); }
        .v2-fs-card .pos { color: var(--accent-green); }
        .v2-fs-card .neg { color: var(--accent-red); }
      `}</style>
    </div>
  );
}

/* ── page ──────────────────────────────────────────────────────────── */
export default function Flowseeker() {
  const [ticker, setTicker] = useState('AAPL');
  const [filter, setFilter] = useState('all');
  const [live, setLive] = useState(true);
  const [globalFlow, setGlobalFlow] = useState([]);
  const [tickerFlow, setTickerFlow] = useState([]);
  const [darkPool, setDarkPool] = useState([]);
  const [summary, setSummary] = useState(null);
  const [flowIntel, setFlowIntel] = useState(null);
  const [err, setErr] = useState(null);
  const [lastTickAt, setLastTickAt] = useState(null);
  const pollIdRef = useRef(null);

  const fetchAll = async () => {
    const errs = [];
    try {
      const r = await fetch('/flow/live?limit=200');
      if (r.ok) setGlobalFlow(await r.json());
      else errs.push(`live=${r.status}`);
    } catch (e) { errs.push(`live=${e.message}`); }
    try {
      const r = await fetch(`/flow/${encodeURIComponent(ticker)}?limit=200`);
      if (r.ok) setTickerFlow(await r.json());
      else errs.push(`flow/${ticker}=${r.status}`);
    } catch (e) { errs.push(`flow/${ticker}=${e.message}`); }
    try {
      const r = await fetch('/flow/darkpool?limit=200');
      if (r.ok) setDarkPool(await r.json());
      else errs.push(`darkpool=${r.status}`);
    } catch (e) { errs.push(`darkpool=${e.message}`); }
    try {
      const r = await fetch('/flow/summary');
      if (r.ok) setSummary(await r.json());
      else errs.push(`summary=${r.status}`);
    } catch (e) { errs.push(`summary=${e.message}`); }
    try {
      const r = await fetch(`/flowintel/${encodeURIComponent(ticker)}`);
      if (r.ok) setFlowIntel(await r.json());
      else errs.push(`flowintel=${r.status}`);
    } catch (e) { errs.push(`flowintel=${e.message}`); }
    setErr(errs.length ? errs.join(' · ') : null);
    setLastTickAt(new Date().toISOString());
  };

  /* ── initial fetch + polling ─────────────────────────────────────── */
  useEffect(() => {
    fetchAll();
    if (!live) {
      if (pollIdRef.current) clearInterval(pollIdRef.current);
      return;
    }
    pollIdRef.current = setInterval(fetchAll, POLL_MS);
    return () => {
      if (pollIdRef.current) clearInterval(pollIdRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, ticker]);

  /* ── normalize raw arrays ────────────────────────────────────────── */
  const allFlow = useMemo(() => {
    if (Array.isArray(globalFlow)) return globalFlow;
    return globalFlow?.flow || [];
  }, [globalFlow]);

  const tickerFlowArr = useMemo(() => {
    if (Array.isArray(tickerFlow)) return tickerFlow;
    return tickerFlow?.flow || [];
  }, [tickerFlow]);

  const dpArr = useMemo(() => {
    if (Array.isArray(darkPool)) return darkPool;
    return darkPool?.flow || [];
  }, [darkPool]);

  /* ── filter pipeline applied to GLOBAL flow for the timeline ─────── */
  const filteredFlow = useMemo(() => {
    let src = allFlow;
    if (filter === 'dark') {
      // Dark-pool stream gets its own source.
      return dpArr;
    }
    if (filter === 'sweeps') {
      src = src.filter((t) => (t.trade_type || '').toLowerCase() === 'sweep');
    } else if (filter === 'blocks') {
      src = src.filter((t) => (t.trade_type || '').toLowerCase() === 'block');
    } else if (filter === 'bullish') {
      src = src.filter((t) => (t.sentiment || '').toLowerCase() === 'bullish');
    } else if (filter === 'bearish') {
      src = src.filter((t) => (t.sentiment || '').toLowerCase() === 'bearish');
    }
    return src.slice(0, 80);
  }, [allFlow, dpArr, filter]);

  /* ── KPI strip values (prefer /flow/summary; otherwise derive) ───── */
  const kpis = useMemo(() => {
    if (summary && (summary.count || 0) > 0) {
      return {
        premium: summary.total_premium || 0,
        bull: summary.bullish || 0,
        bear: summary.bearish || 0,
        blocks: allFlow.filter((t) => (t.trade_type || '').toLowerCase() === 'block').length,
        urgency: 0,    // summary doesn't carry avg urgency; show 0 if missing
        sentiment: summary.net_sentiment || 'neutral',
        count: summary.count,
      };
    }
    // Derive from allFlow when summary is empty.
    const arr = allFlow;
    const bull = arr.filter((t) => (t.sentiment || '').toLowerCase() === 'bullish').length;
    const bear = arr.filter((t) => (t.sentiment || '').toLowerCase() === 'bearish').length;
    const blocks = arr.filter((t) => (t.trade_type || '').toLowerCase() === 'block').length;
    const premium = arr.reduce((s, t) => s + (Number(t.premium) || 0), 0);
    const urgArr = arr.map((t) => Number(t.urgency)).filter((u) => isFinite(u));
    const urgency = urgArr.length ? urgArr.reduce((s, u) => s + u, 0) / urgArr.length : 0;
    return {
      premium, bull, bear, blocks, urgency,
      sentiment: bull > bear ? 'bullish' : bear > bull ? 'bearish' : 'neutral',
      count: arr.length,
    };
  }, [summary, allFlow]);

  /* ── flow-intel snapshot summary ─────────────────────────────────── */
  const intelSummary = useMemo(() => {
    if (!flowIntel) return null;
    const dp = flowIntel.dealer_positioning || {};
    const fp = flowIntel.flow_profile || {};
    return {
      regime: dp.regime,
      netGex: dp.net_gex,
      pinProb: dp.pinning_probability,
      hedging: dp.hedging_pressure,
      dominantWall: dp.dominant_wall,
      bullishSweeps: fp.bullish_sweeps,
      bearishSweeps: fp.bearish_sweeps,
      totalPremium: fp.total_premium,
      avgUrgency: fp.avg_urgency,
      direction: fp.direction,
      notes: dp.notes || [],
    };
  }, [flowIntel]);

  /* ── render ──────────────────────────────────────────────────────── */
  return (
    <div className="v2-fs">
      {/* ─── HEADER ─── */}
      <div className="v2-fs-header">
        <div className="v2-fs-header__main">
          <h1 className="v2-fs-header__title">Flowseeker</h1>
          <span className="dim">
            live options-flow + dark-pool intel · polling every 10s
          </span>
        </div>
        <div className="v2-fs-header__controls">
          <label className="v2-fs-ctrl">
            <span className="v2-fs-ctrl__l">ticker (right rail)</span>
            <select className="v2-fs-ctrl__input mono"
                    value={ticker}
                    onChange={(e) => setTicker(e.target.value.toUpperCase())}>
              {DEFAULT_TICKERS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <button type="button"
                  className={`v2-fs-live ${live ? 'v2-fs-live--on' : ''}`}
                  onClick={() => setLive((v) => !v)}>
            <span className="v2-fs-live__dot" />
            {live ? 'LIVE' : 'PAUSED'}
            <span className="dim mono"> · {fmtAgo(lastTickAt)} ago</span>
          </button>
        </div>
      </div>

      {err && (
        <AlertBanner severity="warning">
          Some flow endpoints failed: {err}. Showing partial data.
        </AlertBanner>
      )}

      {/* ─── ROW 1: KPI ─── */}
      <Section title="Flow KPI"
               subtitle={`net sentiment: ${kpis.sentiment} · ${kpis.count} ticks`}>
        <div className="v2-fs-kpi-row">
          <KPIWidget icon="◉"
                     label="Total Premium"
                     value={`$${fmtBig(kpis.premium)}`}
                     trend={kpis.premium > 0 ? 'up' : 'flat'}
                     trendText="across all sweeps today"
                     hint="sum of premium from /flow/summary or /flow/live" />
          <KPIWidget icon="◉"
                     label="Bull / Bear sweeps"
                     value={`${kpis.bull} / ${kpis.bear}`}
                     trend={kpis.bull > kpis.bear ? 'up' : kpis.bear > kpis.bull ? 'down' : 'flat'}
                     trendText="ratio across live ticks"
                     hint="count of bullish vs bearish sentiment ticks" />
          <KPIWidget icon="◉"
                     label="Large blocks"
                     value={kpis.blocks}
                     trend={kpis.blocks > 5 ? 'up' : 'flat'}
                     trendText="trade_type == 'block'"
                     hint="off-exchange or large institutional prints" />
          <KPIWidget icon="◉"
                     label="Avg urgency"
                     value={kpis.urgency ? `${(kpis.urgency * 100).toFixed(0)}%` : '—'}
                     trend={kpis.urgency >= 0.7 ? 'up' : 'flat'}
                     trendText="aggressor-side weighting"
                     hint="mean of urgency field across live ticks" />
        </div>
      </Section>

      {/* ─── ROW 2: Filter chips ─── */}
      <div className="v2-fs-chips">
        {FILTERS.map((f) => (
          <button key={f.id}
                  type="button"
                  className={`v2-fs-chip ${filter === f.id ? 'v2-fs-chip--on' : ''}`}
                  onClick={() => setFilter(f.id)}>
            {f.label}
          </button>
        ))}
      </div>

      {/* ─── ROW 3: Timeline + depth chart ─── */}
      <div className="v2-fs-grid v2-fs-grid--main">
        <Section title={`Flow stream · ${filter}`}
                 subtitle={`${filteredFlow.length} cards${filteredFlow.length > 80 ? ' (capped at 80)' : ''}`}>
          <Card>
            {filteredFlow.length === 0 ? (
              <EmptyState
                icon="∅"
                message={
                  filter === 'dark'
                    ? 'No dark-pool prints right now — /flow/darkpool returned empty.'
                    : 'No flow ticks right now — /flow/live returned empty. (Market may be closed or vendor stream paused.)'
                } />
            ) : (
              <div className="v2-fs-stream">
                {filteredFlow.map((t, i) => (
                  <FlowCard key={`${t.timestamp || ''}-${t.ticker || ''}-${i}`} tick={t} />
                ))}
              </div>
            )}
          </Card>
        </Section>

        <Section title={`${ticker} depth`}
                 subtitle={`${tickerFlowArr.length} ticks · premium-by-strike`}>
          <Card>
            <FlowDepthChart
              flows={tickerFlowArr}
              topN={12}
              spot={flowIntel?.dealer_positioning?.spot}
            />
          </Card>
        </Section>
      </div>

      {/* ─── ROW 4: FlowIntel snapshot ─── */}
      <Section title={`${ticker} flow intelligence`}
               subtitle="dealer positioning + per-ticker flow profile">
        <Card>
          {intelSummary ? (
            <div className="v2-fs-intel">
              <div className="v2-fs-intel__panel">
                <div className="v2-fs-intel__head">DEALER POSITIONING</div>
                <div className="v2-fs-intel__row">
                  <Stat label="Regime" value={intelSummary.regime || '—'} mono />
                  <Stat label="Net GEX" value={`$${fmtBig(intelSummary.netGex)}`} mono />
                  <Stat label="Pin prob"
                        value={intelSummary.pinProb != null ? `${(intelSummary.pinProb * 100).toFixed(0)}%` : '—'} mono />
                  <Stat label="Hedging" value={intelSummary.hedging || '—'} mono />
                  <Stat label="Dominant wall" value={intelSummary.dominantWall || '—'} mono />
                </div>
                {intelSummary.notes.length > 0 && (
                  <ul className="v2-fs-intel__notes">
                    {intelSummary.notes.map((n, i) => (
                      <li key={i} className="dim">• {n}</li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="v2-fs-intel__panel">
                <div className="v2-fs-intel__head">FLOW PROFILE</div>
                <div className="v2-fs-intel__row">
                  <Stat label="Direction"
                        value={(intelSummary.direction || 'neutral').toUpperCase()}
                        mono />
                  <Stat label="Bull / Bear sweeps"
                        value={`${intelSummary.bullishSweeps || 0} / ${intelSummary.bearishSweeps || 0}`}
                        mono />
                  <Stat label="Total premium"
                        value={`$${fmtBig(intelSummary.totalPremium)}`}
                        mono />
                  <Stat label="Avg urgency"
                        value={intelSummary.avgUrgency
                          ? `${(intelSummary.avgUrgency * 100).toFixed(0)}%`
                          : '—'}
                        mono />
                </div>
              </div>
            </div>
          ) : (
            <EmptyState icon="∅" message={`No /flowintel/${ticker} response.`} />
          )}
        </Card>
      </Section>

      <style>{`
        .v2-fs-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-end;
          gap: 16px;
          padding-bottom: 16px;
          border-bottom: 1px solid var(--border-subtle);
          margin-bottom: var(--space-4);
          flex-wrap: wrap;
        }
        .v2-fs-header__title {
          margin: 0; font-size: var(--font-size-2xl);
          font-weight: 800; letter-spacing: -0.02em;
          color: var(--text-primary);
        }
        .v2-fs-header__main { display: flex; flex-direction: column; gap: 4px; }
        .v2-fs-header__controls {
          display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap;
        }
        .v2-fs-ctrl { display: flex; flex-direction: column; gap: 4px; }
        .v2-fs-ctrl__l {
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 10px;
        }
        .v2-fs-ctrl__input {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-primary);
          padding: 6px 10px;
          font-size: 12px;
          border-radius: var(--radius-sm);
          min-width: 120px;
        }
        .v2-fs-live {
          display: inline-flex; align-items: center; gap: 6px;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-tertiary);
          padding: 6px 14px;
          font-family: var(--font-mono);
          font-size: 11px;
          font-weight: 700;
          border-radius: var(--radius-sm);
          cursor: pointer;
        }
        .v2-fs-live--on {
          color: var(--accent-green);
          border-color: var(--accent-green);
          background: rgba(0, 255, 136, 0.08);
        }
        .v2-fs-live__dot {
          width: 6px; height: 6px;
          background: var(--accent-green);
          border-radius: 50%;
          animation: v2-fs-pulse 1.5s ease-in-out infinite;
        }
        .v2-fs-live:not(.v2-fs-live--on) .v2-fs-live__dot {
          background: var(--text-muted);
          animation: none;
        }
        @keyframes v2-fs-pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.35; }
        }

        .v2-fs-kpi-row {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: var(--space-4);
        }
        @media (max-width: 1100px) {
          .v2-fs-kpi-row { grid-template-columns: repeat(2, 1fr); }
        }

        .v2-fs-chips {
          display: flex; gap: 6px; flex-wrap: wrap;
          margin-bottom: var(--space-4);
        }
        .v2-fs-chip {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-secondary);
          font-family: var(--font-mono);
          font-size: 11px;
          padding: 6px 14px;
          border-radius: 999px;
          cursor: pointer;
          letter-spacing: 0.04em;
        }
        .v2-fs-chip:hover {
          border-color: var(--accent-cyan);
          color: var(--accent-cyan);
        }
        .v2-fs-chip--on {
          background: var(--accent-cyan);
          color: var(--bg-primary);
          border-color: var(--accent-cyan);
          font-weight: 700;
        }

        .v2-fs-grid--main {
          display: grid;
          grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
          gap: var(--space-4);
          margin-bottom: var(--space-6);
        }
        @media (max-width: 1100px) {
          .v2-fs-grid--main { grid-template-columns: 1fr; }
        }
        .v2-fs-stream {
          display: flex; flex-direction: column; gap: 8px;
          max-height: 720px;
          overflow-y: auto;
          padding-right: 4px;
        }

        .v2-fs-intel {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: var(--space-6);
        }
        @media (max-width: 900px) {
          .v2-fs-intel { grid-template-columns: 1fr; }
        }
        .v2-fs-intel__head {
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 10px;
          margin-bottom: 10px;
        }
        .v2-fs-intel__row {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: var(--space-3);
        }
        .v2-fs-intel__notes {
          list-style: none; padding: 0; margin: 10px 0 0;
          display: flex; flex-direction: column; gap: 4px;
          font-size: 11px;
        }
        .dim { color: var(--text-tertiary); }
      `}</style>
    </div>
  );
}
