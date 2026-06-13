/* MITS Phase 19 Stream 1 — Stock Detail v2 (/v2/stock/:ticker).
 *
 * Per-ticker page. Every signal we have on a ticker, on one screen:
 *   HEADER  ticker + name + live price + Δ% + market state + source
 *   ROW 1   KPI strip: VWAP, RSI14, ADX, IV Rank, GEX Net
 *   ROW 2   OHLC chart (real candles + theory overlays + entry zone)
 *   ROW 3   GEX mini panel  |  Flow mini panel
 *   ROW 4   Regime Vector  |  Strategy Matrix  |  Memory
 *   ROW 5   Decision History (last 20 provenance rows for this ticker)
 *   ROW 6   "Why didn't I trade THIS?" — top blocking_factors
 *
 * Endpoints consumed (all REAL backend, no mocks):
 *   /quote/{ticker}                      — 1s tick (via useLivePrice)
 *   /analysis/{ticker}?window=today|5d|all — bars + regime_vector + thesis
 *   /theories/multi/{ticker}?theories=&window= — overlays
 *   /heatseeker/{ticker}                 — GEX walls, gamma flip, regime
 *   /flow/{ticker}                       — recent flow ticks (Alpaca)
 *   /strategy/matrix/{ticker}            — 10 strategy candidates
 *   /decision/provenance?ticker=X&limit=20 — per-ticker decision history
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Card, Stat, Pill, Section, Table, EmptyState, AlertBanner,
} from '../../design/Components.jsx';
import OHLCChart from '../components/OHLCChart.jsx';
import TheoryOverlay, { buildOverlays } from '../components/TheoryOverlay.jsx';
import useLivePrice from '../hooks/useLivePrice.js';

/* ── analysis window mapping ───────────────────────────────────────── */
const INTERVAL_TO_WINDOW = {
  '1m':  'today',  // intraday 1m bars
  '5m':  'today',
  '15m': '5d',
  '1h':  '5d',
  '1d':  'all',
};
const INTERVALS = ['1m', '5m', '15m', '1h', '1d'];
const THEORY_WINDOW_MAP = {
  '1m':  '1m',
  '5m':  '1m',
  '15m': '3m',
  '1h':  '3m',
  '1d':  '1y',
};

/* ── helpers ───────────────────────────────────────────────────────── */
function fmtMoney(v, places = 2) {
  if (v == null || !isFinite(v)) return '—';
  return `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: places, maximumFractionDigits: places,
  })}`;
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}%`;
}
function fmtN(n, places = 0) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: places, maximumFractionDigits: places,
  });
}
function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  if (x >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (x >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return `${Number(n).toFixed(2)}`;
}
function ageString(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function computeRSI(bars, period = 14) {
  if (!Array.isArray(bars) || bars.length < period + 1) return null;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = bars[i].close - bars[i - 1].close;
    if (d >= 0) gain += d; else loss -= d;
  }
  let avgG = gain / period, avgL = loss / period;
  for (let i = period + 1; i < bars.length; i++) {
    const d = bars[i].close - bars[i - 1].close;
    avgG = (avgG * (period - 1) + (d > 0 ? d : 0)) / period;
    avgL = (avgL * (period - 1) + (d < 0 ? -d : 0)) / period;
  }
  if (avgL === 0) return 100;
  const rs = avgG / avgL;
  return 100 - (100 / (1 + rs));
}

function computeVWAP(bars) {
  if (!Array.isArray(bars) || !bars.length) return null;
  let pv = 0, v = 0;
  for (const b of bars) {
    const typ = (Number(b.high) + Number(b.low) + Number(b.close)) / 3;
    const vol = Number(b.volume || 0);
    pv += typ * vol; v += vol;
  }
  return v > 0 ? pv / v : null;
}

function computeADX(bars, period = 14) {
  if (!Array.isArray(bars) || bars.length < period + 1) return null;
  const trs = [], pdm = [], ndm = [];
  for (let i = 1; i < bars.length; i++) {
    const h = Number(bars[i].high), l = Number(bars[i].low),
          ph = Number(bars[i - 1].high), pl = Number(bars[i - 1].low),
          pc = Number(bars[i - 1].close);
    const tr = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    trs.push(tr);
    const upMove = h - ph;
    const downMove = pl - l;
    pdm.push((upMove > downMove && upMove > 0) ? upMove : 0);
    ndm.push((downMove > upMove && downMove > 0) ? downMove : 0);
  }
  if (trs.length < period) return null;
  const sma = (a, n) => a.slice(0, n).reduce((s, x) => s + x, 0) / n;
  let smTR = sma(trs, period), smPDM = sma(pdm, period), smNDM = sma(ndm, period);
  let dxSum = 0; let dxCount = 0;
  for (let i = period; i < trs.length; i++) {
    smTR = smTR - smTR / period + trs[i];
    smPDM = smPDM - smPDM / period + pdm[i];
    smNDM = smNDM - smNDM / period + ndm[i];
    const pdi = smTR ? (smPDM / smTR) * 100 : 0;
    const ndi = smTR ? (smNDM / smTR) * 100 : 0;
    const sum = pdi + ndi;
    if (sum > 0) {
      dxSum += (Math.abs(pdi - ndi) / sum) * 100;
      dxCount += 1;
    }
  }
  return dxCount ? dxSum / dxCount : null;
}

/* ── page ──────────────────────────────────────────────────────────── */
export default function StockDetail() {
  const { ticker: rawTicker } = useParams();
  const ticker = (rawTicker || '').toUpperCase();

  const [chartInterval, setChartInterval] = useState('5m');
  const [analysis, setAnalysis] = useState(null);
  const [analysisErr, setAnalysisErr] = useState(null);
  const [heat, setHeat] = useState(null);
  const [flow, setFlow] = useState(null);
  const [strategyMatrix, setStrategyMatrix] = useState(null);
  const [provenance, setProvenance] = useState(null);
  const [theoryPayload, setTheoryPayload] = useState(null);
  const [theoryErr, setTheoryErr] = useState(null);

  const { tick: liveTick } = useLivePrice(ticker);

  /* ── /analysis/{ticker}?window=... ───────────────────────────────── */
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    const win = INTERVAL_TO_WINDOW[chartInterval] || 'today';
    (async () => {
      try {
        const r = await fetch(
          `/analysis/${encodeURIComponent(ticker)}?window=${encodeURIComponent(win)}`,
        );
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setAnalysis(j);
          setAnalysisErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setAnalysisErr(e.message || 'analysis fetch failed');
          setAnalysis(null);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [ticker, chartInterval]);

  /* ── /heatseeker, /flow, /strategy/matrix, /decision/provenance ───── */
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/heatseeker/${encodeURIComponent(ticker)}`);
        if (r.ok) {
          const j = await r.json();
          if (!cancelled) setHeat(j);
        }
      } catch (_) {}
      try {
        const r = await fetch(`/flow/${encodeURIComponent(ticker)}`);
        if (r.ok) {
          const j = await r.json();
          if (!cancelled) setFlow(Array.isArray(j) ? j : (j?.flow || []));
        }
      } catch (_) {}
      try {
        const r = await fetch(`/strategy/matrix/${encodeURIComponent(ticker)}`);
        if (r.ok) {
          const j = await r.json();
          if (!cancelled) setStrategyMatrix(j);
        }
      } catch (_) {}
      try {
        const r = await fetch(
          `/decision/provenance?ticker=${encodeURIComponent(ticker)}&limit=20`,
        );
        if (r.ok) {
          const j = await r.json();
          if (!cancelled) setProvenance(j);
        }
      } catch (_) {}
    })();
    return () => { cancelled = true; };
  }, [ticker]);

  /* ── /theories/multi/{ticker} ─────────────────────────────────────── */
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    const win = THEORY_WINDOW_MAP[chartInterval] || '3m';
    (async () => {
      try {
        const url = `/theories/multi/${encodeURIComponent(ticker)}?window=${win}` +
                    `&theories=price_action,ma_ribbon,bollinger`;
        const r = await fetch(url);
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setTheoryPayload(j);
          setTheoryErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setTheoryPayload(null);
          setTheoryErr(e.message || 'theory fetch failed');
        }
      }
    })();
    return () => { cancelled = true; };
  }, [ticker, chartInterval]);

  /* ── derived ─────────────────────────────────────────────────────── */
  const bars = useMemo(() => {
    return Array.isArray(analysis?.bars) ? analysis.bars : [];
  }, [analysis]);

  const livePrice = liveTick?.price ?? heat?.spot_price ?? null;
  const prevClose = useMemo(() => {
    if (!bars.length) return null;
    return bars[0]?.open;
  }, [bars]);
  const changePct = useMemo(() => {
    if (livePrice == null || !prevClose) return null;
    return ((livePrice - prevClose) / prevClose) * 100;
  }, [livePrice, prevClose]);

  const regimeVector = analysis?.regime_vector || null;
  const fastThesis = analysis?.fast_thesis || null;

  const rsi   = useMemo(() => computeRSI(bars, 14), [bars]);
  const vwap  = useMemo(() => computeVWAP(bars), [bars]);
  const adx   = useMemo(() => computeADX(bars, 14), [bars]);

  const ivRank = regimeVector?.iv_rank?.value ?? null;
  const netGex = useMemo(() => {
    if (!Array.isArray(heat?.gex_by_strike)) return null;
    return heat.gex_by_strike.reduce(
      (s, x) => s + (Number(x.net_gex) || 0), 0,
    );
  }, [heat]);

  const overlays = useMemo(() => {
    return buildOverlays(theoryPayload || {}, {});
  }, [theoryPayload]);

  /* ── activeTheories for legend ───────────────────────────────────── */
  const activeTheories = useMemo(() => {
    if (theoryPayload?.annotations) {
      return Object.keys(theoryPayload.annotations).filter(
        (k) => theoryPayload.annotations[k],
      );
    }
    return [];
  }, [theoryPayload]);

  /* ── decision history table rows ─────────────────────────────────── */
  const decCols = useMemo(() => ([
    { key: 'when',   label: 'When' },
    { key: 'status', label: 'Status' },
    { key: 'stance', label: 'Stance', mono: true },
    { key: 'conf',   label: 'Conf', mono: true, align: 'right' },
    { key: 'go',     label: '', align: 'right' },
  ]), []);

  const decRows = useMemo(() => {
    if (!provenance?.items) return [];
    return provenance.items.map((p) => {
      const stance = p.consensus?.stance || '—';
      const conf = p.consensus?.confidence != null
        ? `${Math.round(p.consensus.confidence * 100)}%` : '—';
      const statusTone = p.event_status === 'submitted' ? 'success'
        : p.event_status === 'rejected' ? 'warning'
        : p.event_status === 'error' ? 'error' : 'neutral';
      return {
        __key: p.id,
        when:   <span className="dim mono">{ageString(p.decision_timestamp)} ago</span>,
        status: <Pill tone={statusTone}>{p.event_status}</Pill>,
        stance: stance.toUpperCase(),
        conf,
        go: <Link to={`/v1/decision-cockpit/${p.id}`} className="v2-sd-go">
              cockpit →
            </Link>,
      };
    });
  }, [provenance]);

  /* ── "why didn't I trade" — blocking factors aggregation ─────────── */
  const blockingAggregate = useMemo(() => {
    if (!provenance?.items) return null;
    const counts = {};
    const holdItems = provenance.items.filter(
      (p) => p.event_status !== 'submitted' && p.event_status !== 'filled',
    ).slice(0, 5);
    let totalSeen = 0;
    for (const p of holdItems) {
      const bf = p.policy_result?.blocking_factors;
      if (Array.isArray(bf)) {
        for (const b of bf) {
          const k = b.rule || b.category || 'unknown';
          counts[k] = (counts[k] || 0) + 1;
          totalSeen += 1;
        }
      }
    }
    if (!totalSeen) return { items: [], windowN: holdItems.length };
    const items = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([rule, n]) => ({ rule, n }));
    return { items, windowN: holdItems.length };
  }, [provenance]);

  /* ── strategy matrix top candidates ──────────────────────────────── */
  const stratCands = useMemo(() => {
    return Array.isArray(strategyMatrix?.candidates)
      ? strategyMatrix.candidates.slice(0, 5)
      : [];
  }, [strategyMatrix]);

  /* ── flow snapshot summary ───────────────────────────────────────── */
  const flowSummary = useMemo(() => {
    if (!Array.isArray(flow)) return null;
    const recent = flow.slice(0, 30);
    const bull = recent.filter((t) => t.sentiment === 'bullish').length;
    const bear = recent.filter((t) => t.sentiment === 'bearish').length;
    const calls = recent.filter((t) => t.option_type === 'call').length;
    const puts = recent.filter((t) => t.option_type === 'put').length;
    const totalPremium = recent.reduce((s, t) => s + Number(t.premium || 0), 0);
    return { bull, bear, calls, puts, totalPremium, count: recent.length };
  }, [flow]);

  /* ── render ──────────────────────────────────────────────────────── */
  if (!ticker) {
    return (
      <EmptyState icon="?" message="No ticker in URL." />
    );
  }

  const priceSource = liveTick?.source || analysis?.bar_source || '—';
  const priceAge = liveTick?.age_seconds != null
    ? `${liveTick.age_seconds.toFixed(0)}s` : '—';

  return (
    <div className="v2-sd">
      {/* ─────────── HEADER ─────────── */}
      <div className="v2-sd-header">
        <div className="v2-sd-header__main">
          <div className="v2-sd-header__sym mono">{ticker}</div>
          <div className="v2-sd-header__name dim">
            {analysis?.name || (regimeVector?.ticker ? `Live ticker` : 'Live ticker')}
          </div>
          {regimeVector?.intraday_regime?.value && (
            <Pill tone={
              regimeVector.intraday_regime.value === 'normal' ? 'neutral'
              : regimeVector.intraday_regime.value === 'panic' ? 'error'
              : 'warning'
            }>
              {regimeVector.intraday_regime.value.toUpperCase()}
            </Pill>
          )}
        </div>
        <div className="v2-sd-header__price">
          <span className="v2-sd-header__px mono">
            {livePrice != null ? fmtMoney(livePrice) : '—'}
          </span>
          {changePct != null && (
            <span className={`v2-sd-header__chg mono ${changePct >= 0 ? 'pos' : 'neg'}`}>
              {fmtPctSigned(changePct)}
            </span>
          )}
        </div>
        <div className="v2-sd-header__meta">
          <Pill tone={liveTick?.source === 'alpaca' ? 'success'
                      : priceSource?.toString().includes('stale') ? 'warning' : 'neutral'}>
            {priceSource}
          </Pill>
          <span className="dim mono">age {priceAge}</span>
        </div>
      </div>

      {analysisErr && (
        <AlertBanner severity="warning">
          /analysis/{ticker} failed: {analysisErr}. Showing partial data.
        </AlertBanner>
      )}

      {/* ─────────── ROW 1: KPI strip ─────────── */}
      <Section title="Snapshot" subtitle="computed from current bars + analyser">
        <div className="v2-sd-grid v2-sd-grid--5">
          <Card>
            <Stat label="VWAP"
                  value={vwap != null ? fmtMoney(vwap) : '—'}
                  hint="volume-weighted avg price (this window)"
                  mono />
          </Card>
          <Card>
            <Stat label="RSI 14"
                  value={rsi != null ? rsi.toFixed(1) : '—'}
                  hint={rsi != null && rsi > 70 ? 'overbought' :
                        rsi != null && rsi < 30 ? 'oversold' : 'neutral'}
                  mono />
          </Card>
          <Card>
            <Stat label="ADX 14"
                  value={adx != null ? adx.toFixed(1) : '—'}
                  hint={adx != null && adx > 25 ? 'trending' : 'choppy'}
                  mono />
          </Card>
          <Card>
            <Stat label="IV Rank"
                  value={ivRank != null ? Number(ivRank).toFixed(1) : '—'}
                  hint={regimeVector?.iv_regime?.value
                    ? `regime: ${regimeVector.iv_regime.value}` : null}
                  mono />
          </Card>
          <Card>
            <Stat label="GEX Net"
                  value={netGex != null ? fmtBig(netGex) : '—'}
                  hint={heat?.dealer_regime ? `dealer: ${heat.dealer_regime}` : null}
                  delta={heat?.gamma_flip != null
                    ? `flip @ ${fmtMoney(heat.gamma_flip)}` : null}
                  mono />
          </Card>
        </div>
      </Section>

      {/* ─────────── ROW 2: OHLC chart ─────────── */}
      <Section title="Chart"
               subtitle={`${bars.length} bars · source ${analysis?.bar_source || '—'}`}
               actions={
                 <div className="v2-sd-intervals">
                   {INTERVALS.map((iv) => (
                     <button key={iv}
                             type="button"
                             className={`v2-sd-iv ${iv === chartInterval ? 'v2-sd-iv--active' : ''}`}
                             onClick={() => setChartInterval(iv)}>
                       {iv}
                     </button>
                   ))}
                 </div>
               }>
        <Card>
          {bars.length === 0 ? (
            <EmptyState
              icon="∅"
              message={analysisErr
                ? `Analysis unavailable: ${analysisErr}`
                : 'No bars returned for this window.'}
            />
          ) : (
            <OHLCChart
              bars={bars}
              overlays={overlays}
              liveTick={liveTick}
              ticker={ticker}
              height={500}
            />
          )}
          <div style={{ marginTop: 10 }}>
            {activeTheories.length ? (
              <TheoryOverlay
                activeTheories={activeTheories}
                ticker={ticker}
              />
            ) : (
              <TheoryOverlay
                ticker={ticker}
                empty
                emptyMessage={
                  theoryErr
                    ? `Theory overlays unavailable: ${theoryErr}`
                    : 'Theory overlays will appear once /theories/multi returns data for this window.'
                }
              />
            )}
          </div>
        </Card>
      </Section>

      {/* ─────────── ROW 3: GEX + Flow mini panels ─────────── */}
      <div className="v2-sd-grid v2-sd-grid--gx-flow">
        <Section title="GEX snapshot"
                 subtitle={heat?.timestamp ? `as of ${ageString(heat.timestamp)} ago` : null}
                 actions={
                   <Link to="/v2/gex" className="v2-sd-cta">open GEX dashboard →</Link>
                 }>
          <Card>
            {heat ? (
              <div className="v2-sd-grid v2-sd-grid--3">
                <Stat label="Call wall" value={fmtMoney(heat.call_wall)}
                      hint="resistance" mono />
                <Stat label="Put wall" value={fmtMoney(heat.put_wall)}
                      hint="support" mono />
                <Stat label="Gamma flip" value={fmtMoney(heat.gamma_flip)}
                      hint={heat.dealer_regime} mono />
              </div>
            ) : (
              <EmptyState
                icon="∅"
                message="No GEX snapshot — /heatseeker not yet responding."
              />
            )}
          </Card>
        </Section>
        <Section title="Flow snapshot"
                 subtitle={flowSummary ? `last ${flowSummary.count} ticks` : null}
                 actions={
                   <Link to="/v2/flow" className="v2-sd-cta">open flow intel →</Link>
                 }>
          <Card>
            {flowSummary ? (
              <div className="v2-sd-grid v2-sd-grid--3">
                <Stat label="Bullish / Bearish"
                      value={`${flowSummary.bull} / ${flowSummary.bear}`}
                      mono />
                <Stat label="Calls / Puts"
                      value={`${flowSummary.calls} / ${flowSummary.puts}`}
                      mono />
                <Stat label="Premium (Σ)"
                      value={fmtBig(flowSummary.totalPremium)}
                      hint="sum of recent sweep/block premium"
                      mono />
              </div>
            ) : (
              <EmptyState
                icon="∅"
                message="No flow data — /flow not yet responding."
              />
            )}
          </Card>
        </Section>
      </div>

      {/* ─────────── ROW 4: Regime / Strategy / Memory ─────────── */}
      <Section title="Decision context" subtitle="regime vector + strategy matrix + memory">
        <div className="v2-sd-grid v2-sd-grid--3">
          <Card>
            <div className="v2-sd-card-title">Regime vector</div>
            {regimeVector ? (
              <div className="v2-sd-pillstack">
                {['trend','volatility_state','iv_regime','intraday_regime','macro_regime'].map((k) => {
                  const dim = regimeVector[k];
                  if (!dim) return null;
                  const v = dim.value;
                  const display = typeof v === 'string' ? v
                    : (v?.regime || JSON.stringify(v));
                  const tone = dim.health === 'green' ? 'success'
                    : dim.health === 'yellow' ? 'warning' : 'error';
                  return (
                    <Pill key={k} tone={tone}>
                      {k.replaceAll('_', ' ')}: {String(display).toLowerCase()}
                    </Pill>
                  );
                })}
              </div>
            ) : (
              <EmptyState icon="∅" message="No regime vector — analyser did not run." />
            )}
          </Card>
          <Card>
            <div className="v2-sd-card-title">Strategy matrix</div>
            {stratCands.length ? (
              <ul className="v2-sd-strat">
                {stratCands.map((c) => (
                  <li key={c.strategy_name}>
                    <span className="mono">{c.label || c.strategy_name}</span>
                    <span className="dim mono"> fit {(c.fit_score ?? 0).toFixed(2)}</span>
                    {c.cohort_win_rate != null && (
                      <span className="dim mono">
                        {' · '}WR {(c.cohort_win_rate * 100).toFixed(1)}%
                        {c.cohort_n ? ` (n=${c.cohort_n})` : ''}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState icon="∅" message="No strategy candidates — matrix off or no data." />
            )}
          </Card>
          <Card>
            <div className="v2-sd-card-title">Memory + thesis</div>
            {fastThesis?.headline ? (
              <div className="v2-sd-thesis">
                <div className="mono"
                     style={{ color: 'var(--accent-cyan)', marginBottom: 6 }}>
                  {fastThesis.headline}
                </div>
                {fastThesis.summary && (
                  <div className="dim" style={{ fontSize: 12, lineHeight: 1.5 }}>
                    {fastThesis.summary}
                  </div>
                )}
                {Array.isArray(fastThesis.invalidators) && fastThesis.invalidators.length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <div className="dim" style={{ fontSize: 10, marginBottom: 4 }}>
                      INVALIDATORS:
                    </div>
                    {fastThesis.invalidators.slice(0, 3).map((inv, i) => (
                      <div key={i} className="dim" style={{ fontSize: 11 }}>
                        • {inv}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : analysis?.summary ? (
              <div className="dim" style={{ fontSize: 12, lineHeight: 1.5 }}>
                {analysis.summary}
              </div>
            ) : (
              <EmptyState icon="∅" message="No thesis yet — try a different window or wait for the next analyser pass." />
            )}
          </Card>
        </div>
      </Section>

      {/* ─────────── ROW 5: decision history for this ticker ─────────── */}
      <Section title="Decision history"
               subtitle={provenance?.count != null
                 ? `${provenance.count} rows for ${ticker}`
                 : 'loading…'}>
        <Card>
          {decRows.length ? (
            <Table cols={decCols} rows={decRows} striped sticky />
          ) : (
            <EmptyState icon="∅"
                        message={`No decisions logged for ${ticker} yet.`} />
          )}
        </Card>
      </Section>

      {/* ─────────── ROW 6: why didn't I trade ─────────── */}
      <Section title={`Why didn't I trade ${ticker}?`}
               subtitle="top blocking_factors from the last 5 non-submitted decisions">
        <Card>
          {blockingAggregate?.items?.length ? (
            <>
              <div className="v2-sd-block-summary">
                Across the last {blockingAggregate.windowN} non-submitted
                decisions, the engine cited:
              </div>
              <ul className="v2-sd-block-list">
                {blockingAggregate.items.map((b) => (
                  <li key={b.rule}>
                    <span className="mono"
                          style={{ color: 'var(--accent-yellow)' }}>
                      {b.rule}
                    </span>
                    <span className="dim mono"> × {b.n}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <EmptyState icon="∅"
                        message="No blocking factors recorded — engine has not produced enough HOLD decisions for this ticker." />
          )}
        </Card>
      </Section>

      <style>{`
        .v2-sd-header {
          display: grid;
          grid-template-columns: 1fr auto auto;
          gap: 16px;
          align-items: center;
          padding: 12px 0 16px;
          border-bottom: 1px solid var(--border-subtle);
          margin-bottom: var(--space-6);
        }
        .v2-sd-header__main {
          display: flex; align-items: baseline; gap: 12px;
          flex-wrap: wrap;
        }
        .v2-sd-header__sym {
          font-size: var(--font-size-2xl);
          font-weight: 800;
          letter-spacing: 0.04em;
          color: var(--text-primary);
        }
        .v2-sd-header__name { font-size: 13px; color: var(--text-tertiary); }
        .v2-sd-header__price {
          display: flex; gap: 12px; align-items: baseline;
        }
        .v2-sd-header__px {
          font-size: var(--font-size-xl); font-weight: 700;
          color: var(--text-primary);
        }
        .v2-sd-header__chg.pos { color: var(--accent-green); }
        .v2-sd-header__chg.neg { color: var(--accent-red); }
        .v2-sd-header__meta {
          display: flex; gap: 8px; align-items: center;
          font-size: 11px;
        }

        .v2-sd-grid {
          display: grid; gap: var(--space-4);
        }
        .v2-sd-grid--5 { grid-template-columns: repeat(5, 1fr); }
        .v2-sd-grid--3 { grid-template-columns: repeat(3, 1fr); }
        .v2-sd-grid--gx-flow {
          grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
          margin-bottom: var(--space-6);
        }
        @media (max-width: 1100px) {
          .v2-sd-grid--5 { grid-template-columns: repeat(2, 1fr); }
          .v2-sd-grid--3 { grid-template-columns: 1fr; }
          .v2-sd-grid--gx-flow { grid-template-columns: 1fr; }
        }

        .v2-sd-intervals { display: flex; gap: 4px; }
        .v2-sd-iv {
          padding: 4px 10px;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-sm);
          color: var(--text-tertiary);
          font-size: 11px;
          font-family: 'JetBrains Mono', monospace;
          cursor: pointer;
        }
        .v2-sd-iv--active {
          background: var(--accent-cyan);
          color: var(--bg-primary);
          border-color: var(--accent-cyan);
          font-weight: 700;
        }
        .v2-sd-cta {
          color: var(--accent-cyan); text-decoration: none;
          font-size: 12px;
        }
        .v2-sd-cta:hover { text-decoration: underline; }

        .v2-sd-card-title {
          color: var(--text-tertiary);
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          margin-bottom: 8px;
        }
        .v2-sd-pillstack {
          display: flex; flex-wrap: wrap; gap: 6px;
        }
        .v2-sd-strat {
          list-style: none; padding: 0; margin: 0;
          display: flex; flex-direction: column; gap: 4px;
          font-size: 11px;
        }
        .v2-sd-strat li {
          padding: 4px 0;
          border-bottom: 1px dashed var(--border-subtle);
        }
        .v2-sd-thesis { font-size: 12px; }
        .v2-sd-go {
          color: var(--accent-cyan); text-decoration: none;
          font-size: 11px;
        }
        .v2-sd-go:hover { text-decoration: underline; }
        .v2-sd-block-summary {
          color: var(--text-secondary); font-size: 12px; margin-bottom: 8px;
        }
        .v2-sd-block-list {
          list-style: none; padding: 0; margin: 0;
          display: flex; flex-direction: column; gap: 4px;
          font-size: 12px;
        }
        .dim { color: var(--text-tertiary); }
      `}</style>
    </div>
  );
}
