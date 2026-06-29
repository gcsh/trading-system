/* MITS Phase 19 Stream 1 — MissionControl v2 (/v2/).
 *
 * Operator landing page. Surfaces critical state at-a-glance:
 *   ROW 0   Throughput alert banner (submission_rate < 0.5% → red)
 *   ROW 1   KPI strip (Equity, Today P&L, Cycles, Last cycle age)
 *   ROW 2   Decision Funnel  |  Smoking Gun + confidence histogram
 *   ROW 3   Quality vs Quantity sparklines
 *   ROW 4   Watchlist quick-access (clickable → /v2/stock/:ticker)
 *   ROW 5   Recent Activity Feed (clickable → /v2/decision/cockpit/:id)
 *   ROW 6   Safety Flag Status (read-only chips)
 *
 * All data is from real backend endpoints. EmptyState shown when a
 * source returns 404/500. No mock data.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Card, Stat, Pill, Sparkline, AlertBanner, Section,
  Table, EmptyState, KPIWidget,
} from '../../design/Components.jsx';
import FunnelChart from '../components/FunnelChart.jsx';
import useFunnel from '../hooks/useFunnel.js';
import { useLivePrices } from '../hooks/useLivePrice.js';
import { pickLiveBadge } from '../../lib/liveBadge.js';

const V2_PILL_TONE = { success: 'success', warning: 'warning', danger: 'error', muted: 'neutral' };

const POLL_STATUS_MS    = 5_000;
const POLL_PROVENANCE_MS = 30_000;
const WATCHLIST_REFRESH_MS = 60_000;

/* ── helpers ───────────────────────────────────────────────────────── */
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
function fmtN(n) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString();
}
function ageString(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

/* ── confidence histogram inline component ─────────────────────────── */
function ConfHist({ histogram }) {
  if (!histogram || !Array.isArray(histogram.non_hold)) return null;
  const nh = histogram.non_hold;
  const sub = histogram.submitted || [];
  const max = Math.max(1, ...nh);
  return (
    <div className="v2-mc-hist">
      {nh.map((v, i) => {
        const h = Math.max(2, (v / max) * 56);
        const sbN = sub[i] || 0;
        const isFirst = i === 0;
        return (
          <div key={i} className="v2-mc-hist__col" title={
            `bin ${(i * 0.1).toFixed(1)}–${((i + 1) * 0.1).toFixed(1)} · ` +
            `non_hold=${v} · submitted=${sbN}`
          }>
            <div className="v2-mc-hist__bar"
                 style={{
                   height:     `${h}px`,
                   background: isFirst ? 'var(--accent-red)' : 'var(--accent-cyan)',
                 }} />
            {sbN > 0 && (
              <div className="v2-mc-hist__sub"
                   style={{ height: `${Math.max(2, (sbN / max) * 56)}px` }} />
            )}
            <div className="v2-mc-hist__lbl mono">{(i * 0.1).toFixed(1)}</div>
          </div>
        );
      })}
      <style>{`
        .v2-mc-hist {
          display: flex; align-items: flex-end; gap: 4px;
          padding: 8px 4px 0;
          height: 90px;
        }
        .v2-mc-hist__col {
          display: flex; flex-direction: column; align-items: center;
          flex: 1; gap: 2px;
        }
        .v2-mc-hist__bar {
          width: 100%;
          border-radius: 2px 2px 0 0;
          min-height: 2px;
          transition: height 0.2s;
        }
        .v2-mc-hist__sub {
          width: 100%;
          background: var(--accent-green);
          border-radius: 2px 2px 0 0;
        }
        .v2-mc-hist__lbl {
          color: var(--text-tertiary);
          font-size: 9px;
        }
      `}</style>
    </div>
  );
}

/* ── page ──────────────────────────────────────────────────────────── */
export default function MissionControl() {
  const [status, setStatus] = useState(null);
  const [statusErr, setStatusErr] = useState(null);
  const [provenance, setProvenance] = useState(null);
  const [provErr, setProvErr] = useState(null);
  const [scorecard, setScorecard] = useState(null);
  const [flags, setFlags] = useState(null);
  const [watchlist, setWatchlist] = useState(null);
  const [watchlistErr, setWatchlistErr] = useState(null);
  const [equityHist, setEquityHist] = useState(null);

  const { funnel, history: funnelHistory, refresh: refreshFunnel } =
    useFunnel({ historyDays: 7 });

  /* ── /bot/status poll ────────────────────────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await fetch('/bot/status');
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setStatus(j);
          setStatusErr(null);
        }
      } catch (e) {
        if (!cancelled) setStatusErr(e.message || 'status fetch failed');
      }
    }
    tick();
    const id = setInterval(tick, POLL_STATUS_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  /* ── /decision/provenance poll ───────────────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await fetch('/decision/provenance?limit=20');
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setProvenance(j);
          setProvErr(null);
        }
      } catch (e) {
        if (!cancelled) setProvErr(e.message || 'provenance fetch failed');
      }
    }
    tick();
    const id = setInterval(tick, POLL_PROVENANCE_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  /* ── /decision/scorecard, /learning/flags ────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch('/decision/scorecard');
        if (r.ok) {
          const j = await r.json();
          if (!cancelled) setScorecard(j);
        }
      } catch (_) { /* render EmptyState */ }
      try {
        const r = await fetch('/learning/flags');
        if (r.ok) {
          const j = await r.json();
          if (!cancelled) setFlags(j);
        }
      } catch (_) {}
    })();
    return () => { cancelled = true; };
  }, []);

  /* ── /watchlist/items (initial + 1m refresh) ─────────────────────── */
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await fetch('/watchlist/items');
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setWatchlist(Array.isArray(j) ? j : []);
          setWatchlistErr(null);
        }
      } catch (e) {
        if (!cancelled) setWatchlistErr(e.message || 'watchlist fetch failed');
      }
    }
    tick();
    const id = setInterval(tick, WATCHLIST_REFRESH_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  /* ── /portfolio/equity (for the sparkline) ────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch('/portfolio/equity');
        if (r.ok) {
          const j = await r.json();
          if (!cancelled && Array.isArray(j)) {
            setEquityHist(j.slice(-40));
          }
        }
      } catch (_) {}
    })();
    return () => { cancelled = true; };
  }, []);

  /* ── pick top-8 watchlist tickers for live polling ───────────────── */
  const wlTopTickers = useMemo(() => {
    if (!Array.isArray(watchlist)) return [];
    return watchlist.slice(0, 8).map((w) => w.ticker).filter(Boolean);
  }, [watchlist]);
  const { ticks: liveTicks } = useLivePrices(wlTopTickers);

  /* ── derived values ──────────────────────────────────────────────── */
  const funnelRow      = funnel?.row || null;
  const funnelStages   = funnel?.report?.stages || [];
  const submissionRate = useMemo(() => {
    if (!funnelRow) return null;
    const evals = Number(funnelRow.n_evaluations || 0);
    const subs  = Number(funnelRow.n_submitted   || 0);
    if (!evals) return null;
    return subs / evals;
  }, [funnelRow]);

  const throughputSeverity = useMemo(() => {
    if (submissionRate == null) return null;
    if (submissionRate < 0.005) return 'critical';
    if (submissionRate < 0.010) return 'warning';
    return 'ok';
  }, [submissionRate]);

  const confHist = funnelRow?.confidence_histogram;
  const smokingGun = useMemo(() => {
    if (!confHist?.non_hold) return null;
    const total = confHist.non_hold.reduce((a, b) => a + b, 0);
    if (!total) return null;
    const zeroBin = confHist.non_hold[0] || 0;
    const pct = zeroBin / total;
    return {
      total, zeroBin, pct,
      isAlarming: pct > 0.5,
    };
  }, [confHist]);

  const compositeMean = scorecard?.composite_distribution?.mean
    ?? funnelRow?.composite_quality_mean ?? null;

  /* ── sparkline data for KPI strip ────────────────────────────────── */
  const equityPoints = useMemo(() => {
    if (!Array.isArray(equityHist)) return [];
    return equityHist.map((r) => Number(r.portfolio_value || 0));
  }, [equityHist]);

  const composeSpark = useMemo(() => {
    if (!funnelHistory?.rows) return [];
    return funnelHistory.rows.map((r) => Number(r.composite_quality_mean || 0));
  }, [funnelHistory]);

  const submissionRateSpark = useMemo(() => {
    if (!funnelHistory?.rows) return [];
    return funnelHistory.rows.map((r) => {
      const e = Number(r.n_evaluations || 0);
      const s = Number(r.n_submitted   || 0);
      return e ? s / e : 0;
    });
  }, [funnelHistory]);

  /* ── tables ──────────────────────────────────────────────────────── */
  const watchlistCols = useMemo(() => ([
    { key: 'ticker',  label: 'Ticker', mono: true },
    { key: 'price',   label: 'Price',  mono: true, align: 'right' },
    { key: 'change',  label: 'Δ%',     mono: true, align: 'right' },
    { key: 'source',  label: 'Source' },
    { key: 'go',      label: '', align: 'right' },
  ]), []);

  const watchlistRows = useMemo(() => {
    if (!Array.isArray(watchlist)) return [];
    return watchlist.map((w) => {
      const liveTick = liveTicks[w.ticker];
      const price = liveTick?.price ?? w.quote?.price;
      const prev = w.quote?.prev_close;
      let changePct = w.quote?.change_pct;
      if (liveTick && prev && prev > 0) {
        changePct = ((liveTick.price - prev) / prev) * 100;
      }
      const positive = (changePct ?? 0) >= 0;
      return {
        __key: w.id || w.ticker,
        ticker: (
          <Link to={`/v2/stock/${w.ticker}`}
                className="v2-mc-link mono">
            {w.ticker}
          </Link>
        ),
        price: price != null
          ? <span className="mono">${Number(price).toFixed(2)}</span>
          : <span className="dim">—</span>,
        change: changePct != null
          ? <span style={{ color: positive ? 'var(--accent-green)' : 'var(--accent-red)' }}>
              {fmtPctSigned(changePct)}
            </span>
          : <span className="dim">—</span>,
        source: (() => {
          // 2026-06-15 — consult /quote freshness booleans via pickLiveBadge
          // so a stale yfinance print can't show as success-green here.
          const badge = pickLiveBadge(liveTick || w.quote);
          return (
            <Pill tone={V2_PILL_TONE[badge.tone] || 'neutral'} title={badge.title}>
              {badge.label}
            </Pill>
          );
        })(),
        go: <Link to={`/v2/stock/${w.ticker}`}
                  className="v2-mc-go">open →</Link>,
      };
    });
  }, [watchlist, liveTicks]);

  const provCols = useMemo(() => ([
    { key: 'when',   label: 'When' },
    { key: 'ticker', label: 'Ticker', mono: true },
    { key: 'status', label: 'Status' },
    { key: 'reco',   label: 'Recommendation' },
    { key: 'go',     label: '', align: 'right' },
  ]), []);

  const provRows = useMemo(() => {
    if (!provenance?.items) return [];
    return provenance.items.slice(0, 12).map((p) => {
      const consensusStance = p.consensus?.stance ||
        p.chairman_memo?.headline_recommendation || '—';
      const recoConfidence = p.consensus?.confidence != null
        ? `${Math.round(p.consensus.confidence * 100)}%` : '';
      const statusTone = p.event_status === 'submitted' ? 'success'
        : p.event_status === 'rejected' ? 'warning'
        : p.event_status === 'error' ? 'error' : 'neutral';
      return {
        __key: p.id,
        when:   <span className="dim mono">{ageString(p.decision_timestamp)}</span>,
        ticker: (
          <Link to={`/v2/stock/${p.ticker}`} className="v2-mc-link mono">
            {p.ticker}
          </Link>
        ),
        status: <Pill tone={statusTone}>{p.event_status}</Pill>,
        reco:   <span className="mono">
                  {consensusStance.toUpperCase()}
                  {recoConfidence ? <span className="dim"> · {recoConfidence}</span> : null}
                </span>,
        go:     <Link to={`/v1/decision-cockpit/${p.id}`} className="v2-mc-go">
                  cockpit →
                </Link>,
      };
    });
  }, [provenance]);

  /* ── render ──────────────────────────────────────────────────────── */
  const cycles = status?.cycles_completed ?? status?.cycles;
  const lastCycle = status?.last_cycle_at ?? status?.last_cycle;
  const equity = equityPoints.length
    ? equityPoints[equityPoints.length - 1]
    : null;
  const dailyPnl = status?.daily_pnl;

  return (
    <div className="v2-mc">
      {/* ─────────── ROW 0: throughput alert ─────────── */}
      {throughputSeverity === 'critical' && (
        <AlertBanner severity="critical">
          <strong>
            {fmtN(funnelRow?.n_submitted)} submissions in last{' '}
            {funnelRow?.window_days ?? funnel?.window_days ?? 14} days{' '}
            ({((submissionRate || 0) * 100).toFixed(2)}%)
          </strong>
          {' '} — pipeline is failing to convert. {' '}
          <Link to="/v2/learning/funnel" className="v2-mc-banner-link">
            Investigate the funnel ▸
          </Link>
        </AlertBanner>
      )}
      {throughputSeverity === 'warning' && (
        <AlertBanner severity="warning">
          Submission rate {((submissionRate || 0) * 100).toFixed(2)}% is below
          the 1.0% threshold.{' '}
          <Link to="/v2/learning/funnel" className="v2-mc-banner-link">
            Investigate ▸
          </Link>
        </AlertBanner>
      )}

      {/* ─────────── ROW 1: KPI strip ─────────── */}
      <Section title="Top of book" subtitle="real-time engine + portfolio state">
        <div className="v2-mc-grid v2-mc-grid--4">
          <Card>
            <Stat
              label="Equity"
              value={equity != null ? fmtMoney(equity) : '—'}
              delta={dailyPnl != null ? fmtMoney(dailyPnl, { sign: true }) : null}
              deltaPositive={dailyPnl != null ? dailyPnl >= 0 : undefined}
              mono
            />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={equityPoints}
                color={dailyPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'} />
            </div>
          </Card>
          <Card>
            <Stat
              label="Today P&L"
              value={dailyPnl != null ? fmtMoney(dailyPnl, { sign: true }) : '—'}
              hint={status?.market_regime ? `regime: ${status.market_regime}` : undefined}
              mono
            />
          </Card>
          <Card>
            <KPIWidget
              icon="◎"
              label="Cycles"
              value={fmtN(cycles)}
              trend={status?.running ? 'up' : 'flat'}
              trendText={status?.running ? 'engine running' : 'engine paused'}
            />
          </Card>
          <Card>
            <KPIWidget
              icon="◔"
              label="Last cycle"
              value={ageString(lastCycle)}
              trend="flat"
              trendText={status?.strategy ? `strategy: ${status.strategy}` : '—'}
            />
          </Card>
        </div>
        {statusErr && (
          <div className="v2-mc-err mono">/bot/status: {statusErr}</div>
        )}
      </Section>

      {/* ─────────── ROW 2: funnel + smoking gun ─────────── */}
      <Section title="Decision funnel"
               subtitle={`14-day window — last computed ${ageString(funnelRow?.computed_at)}`}
               actions={
                 <button type="button"
                         className="v2-mc-refresh"
                         onClick={refreshFunnel}>
                   refresh
                 </button>
               }>
        <div className="v2-mc-grid v2-mc-grid--funnel">
          <Card>
            {funnelStages.length > 0 ? (
              <FunnelChart stages={funnelStages} />
            ) : (
              <EmptyState
                icon="∅"
                message="No funnel row yet — the nightly job runs at 21:55 ET."
              />
            )}
          </Card>
          <Card glow={smokingGun?.isAlarming ? 'red' : 'none'}
                variant="outlined">
            <div className="v2-mc-gun">
              <div className="v2-mc-gun__title">
                <Pill tone={smokingGun?.isAlarming ? 'error' : 'neutral'}
                      size="md">
                  {smokingGun?.isAlarming ? 'SMOKING GUN' : 'CONFIDENCE'}
                </Pill>
              </div>
              {smokingGun?.isAlarming ? (
                <>
                  <div className="v2-mc-gun__body mono">
                    {fmtN(smokingGun.zeroBin)} / {fmtN(smokingGun.total)} non-HOLD
                    votes land in the lowest confidence bin
                    (<b>{(smokingGun.pct * 100).toFixed(1)}%</b>).
                  </div>
                  <div className="v2-mc-gun__body" style={{ marginTop: 6 }}>
                    Brain output is collapsing to ~0 confidence on the
                    actionable side. Almost nothing clears the policy gate.
                  </div>
                </>
              ) : smokingGun ? (
                <div className="v2-mc-gun__body mono">
                  Confidence is healthy ({fmtN(smokingGun.zeroBin)}/{fmtN(smokingGun.total)}
                  {' '}in the lowest bin → {(smokingGun.pct * 100).toFixed(1)}%).
                </div>
              ) : (
                <div className="v2-mc-gun__body dim">
                  Confidence histogram not yet computed.
                </div>
              )}
              <ConfHist histogram={confHist} />
              <div className="v2-mc-gun__legend">
                <span><span className="dot" style={{ background: 'var(--accent-red)' }}></span>0–0.1 bin</span>
                <span><span className="dot" style={{ background: 'var(--accent-cyan)' }}></span>non-HOLD</span>
                <span><span className="dot" style={{ background: 'var(--accent-green)' }}></span>submitted</span>
              </div>
              {smokingGun?.isAlarming && (
                <Link to="/v2/learning/funnel" className="v2-mc-gun__cta">
                  Open the funnel page → diagnose Brain confidence ▸
                </Link>
              )}
            </div>
          </Card>
        </div>
      </Section>

      {/* ─────────── ROW 3: quality vs quantity sparklines ─────────── */}
      <Section title="Quality vs quantity"
               subtitle="7-day trend">
        <div className="v2-mc-grid v2-mc-grid--2">
          <Card>
            <Stat
              label="Composite quality (mean)"
              value={compositeMean != null
                ? Number(compositeMean).toFixed(2) : '—'}
              delta={scorecard?.composite_distribution?.median != null
                ? `median ${scorecard.composite_distribution.median.toFixed(2)}` : null}
              mono
            />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={composeSpark}
                color="var(--accent-cyan)" width={260} />
            </div>
          </Card>
          <Card>
            <Stat
              label="Submission rate"
              value={submissionRate != null
                ? `${(submissionRate * 100).toFixed(3)}%` : '—'}
              delta={funnelRow
                ? `${fmtN(funnelRow.n_submitted)} of ${fmtN(funnelRow.n_evaluations)} evals`
                : null}
              mono
            />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={submissionRateSpark}
                color={throughputSeverity === 'critical'
                  ? 'var(--accent-red)' : 'var(--accent-yellow)'}
                width={260} />
            </div>
          </Card>
        </div>
      </Section>

      {/* ─────────── ROW 4: watchlist ─────────── */}
      <Section title="Watchlist"
               subtitle={Array.isArray(watchlist)
                 ? `${watchlist.length} tickers — top 8 polled live`
                 : 'loading…'}
               actions={
                 <Link to="/v2/watchlist" className="v2-mc-cta mono">
                   open full watchlist →
                 </Link>
               }>
        <Card>
          {watchlistErr ? (
            <EmptyState
              icon="!"
              message={`Watchlist unavailable: ${watchlistErr}`}
            />
          ) : Array.isArray(watchlist) && watchlistRows.length ? (
            <Table cols={watchlistCols}
                   rows={watchlistRows.slice(0, 12)}
                   striped sticky />
          ) : (
            <EmptyState
              icon="∅"
              message="Watchlist is empty — add tickers in /v1/settings."
            />
          )}
        </Card>
      </Section>

      {/* ─────────── ROW 5: recent activity ─────────── */}
      <Section title="Recent decisions"
               subtitle={provenance?.count != null
                 ? `${provenance.count} provenance rows scanned`
                 : '—'}>
        <Card>
          {provErr ? (
            <EmptyState
              icon="!"
              message={`Provenance feed: ${provErr}`}
            />
          ) : provRows.length ? (
            <Table cols={provCols} rows={provRows} striped sticky />
          ) : (
            <EmptyState
              icon="∅"
              message="No decisions yet — engine has not started a cycle today."
            />
          )}
        </Card>
      </Section>

      {/* ─────────── ROW 6: safety flag status ─────────── */}
      <Section title="Learning safety flags"
               subtitle="all OFF by default — flip via /opt/trading-bot/.env on EC2">
        <Card>
          {flags ? (
            <div className="v2-mc-flags">
              {Object.entries(flags).map(([k, v]) => (
                <Pill key={k} tone={v ? 'success' : 'neutral'} size="md">
                  {k.replaceAll('_', ' ')}: {v ? 'ON' : 'OFF'}
                </Pill>
              ))}
            </div>
          ) : (
            <EmptyState icon="∅" message="Flag state not available." />
          )}
        </Card>
      </Section>

      {/* shared styles */}
      <style>{`
        .v2-mc-grid {
          display: grid;
          gap: var(--space-4);
        }
        .v2-mc-grid--4 { grid-template-columns: repeat(4, 1fr); }
        .v2-mc-grid--2 { grid-template-columns: repeat(2, 1fr); }
        .v2-mc-grid--funnel {
          grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
        }
        @media (max-width: 1100px) {
          .v2-mc-grid--4 { grid-template-columns: repeat(2, 1fr); }
          .v2-mc-grid--funnel { grid-template-columns: 1fr; }
        }
        .v2-mc-link {
          color: var(--accent-cyan); text-decoration: none;
        }
        .v2-mc-link:hover { text-decoration: underline; }
        .v2-mc-go {
          color: var(--text-tertiary); text-decoration: none;
          font-size: 11px; font-family: 'JetBrains Mono', monospace;
        }
        .v2-mc-go:hover { color: var(--accent-cyan); }
        .v2-mc-cta {
          color: var(--accent-cyan); text-decoration: none;
          font-size: 12px;
        }
        .v2-mc-cta:hover { text-decoration: underline; }
        .v2-mc-refresh {
          padding: 4px 10px;
          background: transparent;
          border: 1px solid var(--border-default);
          border-radius: var(--radius-sm);
          color: var(--text-secondary);
          font-size: 11px;
          cursor: pointer;
        }
        .v2-mc-refresh:hover { color: var(--accent-cyan); border-color: var(--accent-cyan); }
        .v2-mc-err {
          color: var(--accent-red); font-size: 11px; padding: 6px 0;
        }
        .v2-mc-banner-link {
          color: inherit; text-decoration: underline;
        }
        .v2-mc-gun { display: flex; flex-direction: column; gap: 8px; }
        .v2-mc-gun__title { display: flex; gap: 8px; align-items: center; }
        .v2-mc-gun__body { color: var(--text-secondary); font-size: 12px; line-height: 1.5; }
        .v2-mc-gun__body.dim { color: var(--text-tertiary); }
        .v2-mc-gun__legend {
          display: flex; flex-wrap: wrap; gap: 12px;
          font-size: 10px; color: var(--text-tertiary);
          padding-top: 4px;
        }
        .v2-mc-gun__legend .dot {
          display: inline-block; width: 8px; height: 8px;
          border-radius: 2px; margin-right: 4px;
          vertical-align: middle;
        }
        .v2-mc-gun__cta {
          color: var(--accent-cyan); text-decoration: none;
          font-size: 12px; margin-top: 4px;
        }
        .v2-mc-gun__cta:hover { text-decoration: underline; }
        .v2-mc-flags {
          display: flex; flex-wrap: wrap; gap: 8px;
        }
        .dim, .v2-mc-link.dim { color: var(--text-tertiary); }
      `}</style>
    </div>
  );
}
