/* MITS Phase 19 Cluster D — StrategyMatrix v2 (/v2/strategy).
 *
 * Per-ticker strategy browser:
 *   TOP    Ticker selector + query-state chips (regime, IV, pattern hits)
 *   LEFT   16 strategy template cards (from /strategies/catalog)
 *   MAIN   Candidate rankings table — fit / cohort_win / cohort_n / final
 *   DETAIL Selected-candidate panel: requires_passed (green) / failed (red),
 *          supporting patterns, invalidation conditions
 *   CHART  Backtest sparkline (price history from /backtest/{strategy}/{ticker})
 *
 * Endpoints:
 *   GET /strategies/catalog
 *   GET /strategy/matrix/{ticker}
 *   GET /backtest/{strategy_name}/{ticker}
 *   GET /watchlist            for initial-ticker default
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Card, Pill, Section, EmptyState, AlertBanner,
} from '../../design/Components.jsx';
import StrategyTemplateCard from '../components/StrategyTemplateCard.jsx';

const DEFAULT_TICKER = 'AAPL';
const COMMON_TICKERS = ['SPY', 'QQQ', 'AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMD'];

/* ── helpers ────────────────────────────────────────────────────────── */
function fmtPct(v) {
  if (v == null || !isFinite(v)) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}
function fmtN(n, digits = 0) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

/* ── Backtest mini sparkline ────────────────────────────────────────── */
function BacktestSparkline({ strategy, ticker }) {
  const [candles, setCandles] = useState(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let cancelled = false;
    if (!strategy || !ticker) { setCandles(null); return; }
    setCandles(null); setErr(false);
    (async () => {
      try {
        const r = await fetch(`/backtest/${strategy}/${ticker}`);
        if (!r.ok) throw new Error(`${r.status}`);
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('json')) throw new Error('non-JSON');
        const j = await r.json();
        if (!cancelled) setCandles(Array.isArray(j.candles) ? j.candles : []);
      } catch (e) {
        if (!cancelled) { setErr(true); setCandles([]); }
      }
    })();
    return () => { cancelled = true; };
  }, [strategy, ticker]);

  if (candles === null) {
    return <div className="v2-sm-bt__loading">Loading 6-month price history…</div>;
  }
  if (err || candles.length < 2) {
    return <EmptyState icon="📊" message="No backtest history available." />;
  }

  const closes = candles.map(c => c.close);
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const range = max - min || 1;
  const W = 800, H = 140, padT = 8, padB = 18, padL = 36, padR = 8;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const xs = candles.map((_, i) => padL + (i / (candles.length - 1)) * innerW);
  const ys = closes.map(c => padT + (1 - (c - min) / range) * innerH);
  const linePath = candles.map((_, i) => `${i === 0 ? 'M' : 'L'} ${xs[i].toFixed(1)} ${ys[i].toFixed(1)}`).join(' ');
  const first = closes[0], last = closes[closes.length - 1];
  const totalRet = (last - first) / first;
  const ret = totalRet >= 0;

  return (
    <div className="v2-sm-bt">
      <div className="v2-sm-bt__meta">
        <span className="mono">
          {candles[0].t?.slice(0, 10)} → {candles[candles.length - 1].t?.slice(0, 10)}
        </span>
        <span className="mono" style={{ color: ret ? 'var(--accent-green)' : 'var(--accent-red)' }}>
          {ret ? '+' : ''}{(totalRet * 100).toFixed(1)}%
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           style={{ width: '100%', height: H, display: 'block' }}>
        <text x={padL - 4} y={padT + 8} textAnchor="end"
              fontSize="10" fill="var(--text-tertiary)" fontFamily="var(--font-mono)">
          {max.toFixed(0)}
        </text>
        <text x={padL - 4} y={padT + innerH} textAnchor="end"
              fontSize="10" fill="var(--text-tertiary)" fontFamily="var(--font-mono)">
          {min.toFixed(0)}
        </text>
        <path d={linePath}
              fill="none"
              stroke={ret ? 'var(--accent-green)' : 'var(--accent-red)'}
              strokeWidth="1.5"
              strokeLinejoin="round" />
      </svg>
      <div className="v2-sm-bt__note">
        Underlying price history (6mo daily). For strategy P&L curves call the simulator
        — full backtest engine lives in <code className="mono">/backtest/compare/&#123;ticker&#125;</code>.
      </div>
      <style>{`
        .v2-sm-bt__loading {
          font-size: var(--font-size-sm);
          color: var(--text-tertiary);
          text-align: center;
          padding: var(--space-6);
        }
        .v2-sm-bt__meta {
          display: flex; justify-content: space-between;
          font-size: 11px; color: var(--text-tertiary);
          margin-bottom: 4px;
        }
        .v2-sm-bt__note {
          font-size: 11px;
          color: var(--text-tertiary);
          padding-top: 6px;
          line-height: 1.4;
        }
      `}</style>
    </div>
  );
}

/* ── Selected-candidate details ─────────────────────────────────────── */
function CandidateDetail({ candidate, query, ticker }) {
  if (!candidate) {
    return <EmptyState icon="🎯" message="Select a strategy template to see fit details." />;
  }
  return (
    <div className="v2-sm-det">
      <div className="v2-sm-det__head">
        <h3 className="v2-pf-h3">{candidate.label}</h3>
        <Pill tone={candidate.direction === 'long' ? 'success'
                  : candidate.direction === 'short' ? 'error'
                  : 'info'}>
          {candidate.direction}
        </Pill>
        <span className="v2-sm-det__rank mono">
          Rank #{candidate.ranked_position}
        </span>
      </div>

      <div className="v2-sm-det__kpi">
        <div className="v2-sm-det__kpi-item">
          <div className="v2-sm-det__kpi-lbl">Fit Score</div>
          <div className="v2-sm-det__kpi-val mono">{fmtPct(candidate.fit_score)}</div>
        </div>
        <div className="v2-sm-det__kpi-item">
          <div className="v2-sm-det__kpi-lbl">Cohort Win Rate</div>
          <div className="v2-sm-det__kpi-val mono">{fmtPct(candidate.cohort_win_rate)}</div>
          {candidate.cohort_ci_lower != null && candidate.cohort_ci_upper != null && (
            <div className="v2-sm-det__kpi-sub mono">
              CI95 [{fmtPct(candidate.cohort_ci_lower)} – {fmtPct(candidate.cohort_ci_upper)}]
            </div>
          )}
        </div>
        <div className="v2-sm-det__kpi-item">
          <div className="v2-sm-det__kpi-lbl">Cohort N</div>
          <div className="v2-sm-det__kpi-val mono">{fmtN(candidate.cohort_n)}</div>
          <div className="v2-sm-det__kpi-sub mono">
            source: {candidate.cohort_source || '—'}
          </div>
        </div>
        <div className="v2-sm-det__kpi-item">
          <div className="v2-sm-det__kpi-lbl">Final Score</div>
          <div className="v2-sm-det__kpi-val mono">{fmtPct(candidate.final_score)}</div>
        </div>
      </div>

      {candidate.supporting_patterns?.length > 0 && (
        <div className="v2-sm-det__block">
          <div className="v2-sm-det__block-title">Supporting patterns</div>
          <div className="v2-sm-det__chips">
            {candidate.supporting_patterns.map((p, i) => (
              <Pill key={i} tone="info">{p}</Pill>
            ))}
          </div>
        </div>
      )}

      <div className="v2-sm-det__block">
        <div className="v2-sm-det__block-title">
          Requirements passed
          <span className="mono" style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>
            ({candidate.requires_passed?.length || 0})
          </span>
        </div>
        {candidate.requires_passed?.length === 0
          ? <div className="v2-sm-det__none">None</div>
          : (
            <div className="v2-sm-det__chips">
              {candidate.requires_passed?.map((r, i) => (
                <Pill key={i} tone="success">✓ {r}</Pill>
              ))}
            </div>
          )}
      </div>

      <div className="v2-sm-det__block">
        <div className="v2-sm-det__block-title">
          Requirements failed
          <span className="mono" style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>
            ({candidate.requires_failed?.length || 0})
          </span>
        </div>
        {(!candidate.requires_failed || candidate.requires_failed.length === 0)
          ? <div className="v2-sm-det__none">None — all gates passed</div>
          : (
            <div className="v2-sm-det__chips">
              {candidate.requires_failed.map((r, i) => (
                <Pill key={i} tone="error">✗ {r}</Pill>
              ))}
            </div>
          )}
      </div>

      {candidate.invalidation?.length > 0 && (
        <div className="v2-sm-det__block">
          <div className="v2-sm-det__block-title">Invalidation conditions</div>
          <ul className="v2-sm-det__list">
            {candidate.invalidation.map((it, i) => <li key={i}>{it}</li>)}
          </ul>
        </div>
      )}

      <div className="v2-sm-det__block">
        <div className="v2-sm-det__block-title">
          Underlying price history for {ticker}
          <span className="mono" title="Why ranked here?" style={{ marginLeft: 8, color: 'var(--text-tertiary)' }}>
            ⓘ ranked by final_score = fit × cohort_win × confidence
          </span>
        </div>
        <BacktestSparkline strategy={candidate.strategy_name} ticker={ticker} />
      </div>

      <style>{`
        .v2-sm-det__head {
          display: flex; align-items: center; gap: 12px;
          margin-bottom: 16px;
        }
        .v2-sm-det__rank {
          font-size: 11px; color: var(--accent-cyan);
          margin-left: auto;
        }
        .v2-sm-det__kpi {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 12px;
          margin-bottom: 20px;
        }
        .v2-sm-det__kpi-item {
          background: var(--bg-secondary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-md);
          padding: 10px 12px;
        }
        .v2-sm-det__kpi-lbl {
          font-size: 10px;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          font-weight: 600;
        }
        .v2-sm-det__kpi-val {
          font-size: var(--font-size-xl);
          color: var(--text-primary);
          font-weight: 700;
          margin-top: 4px;
        }
        .v2-sm-det__kpi-sub {
          font-size: 10px; color: var(--text-tertiary); margin-top: 2px;
        }
        .v2-sm-det__block { margin-bottom: 16px; }
        .v2-sm-det__block-title {
          font-size: var(--font-size-xs);
          font-weight: 700;
          color: var(--text-secondary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin-bottom: 8px;
        }
        .v2-sm-det__chips { display: flex; flex-wrap: wrap; gap: 6px; }
        .v2-sm-det__none { font-size: var(--font-size-sm); color: var(--text-tertiary); padding: 4px 0; }
        .v2-sm-det__list {
          margin: 0;
          padding-left: 18px;
          font-size: var(--font-size-sm);
          color: var(--text-secondary);
        }
        .v2-sm-det__list li { margin-bottom: 4px; }
        @media (max-width: 900px) {
          .v2-sm-det__kpi { grid-template-columns: repeat(2, 1fr); }
        }
      `}</style>
    </div>
  );
}

/* ── Candidates table (main pane) ───────────────────────────────────── */
function CandidatesTable({ candidates, selected, onSelect }) {
  if (!Array.isArray(candidates) || candidates.length === 0) {
    return <EmptyState icon="📋" message="No strategy candidates for this ticker." />;
  }
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="v2-table v2-table--striped">
        <thead>
          <tr>
            <th>#</th>
            <th>Strategy</th>
            <th>Dir</th>
            <th style={{ textAlign: 'right' }}>Fit</th>
            <th>Fit Bar</th>
            <th style={{ textAlign: 'right' }}>Cohort Win</th>
            <th style={{ textAlign: 'right' }}>N</th>
            <th style={{ textAlign: 'right' }}>Final</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map(c => {
            const isSel = selected === c.strategy_name;
            return (
              <tr key={c.strategy_name}
                  onClick={() => onSelect(c.strategy_name)}
                  style={{
                    cursor: 'pointer',
                    background: isSel ? 'rgba(0, 212, 255, 0.06)' : undefined,
                  }}>
                <td className="mono">{c.ranked_position}</td>
                <td>
                  <span style={{ fontWeight: isSel ? 700 : 500 }}>{c.label}</span>
                </td>
                <td>
                  <Pill tone={c.direction === 'long' ? 'success'
                            : c.direction === 'short' ? 'error'
                            : 'info'}>{c.direction}</Pill>
                </td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtPct(c.fit_score)}</td>
                <td style={{ width: 100 }}>
                  <div className="v2-sm-mt-bar">
                    <div className="v2-sm-mt-bar-fill"
                         style={{
                           width: `${Math.max(0, Math.min(100, (c.fit_score || 0) * 100))}%`,
                           background: 'var(--accent-cyan)',
                         }} />
                  </div>
                </td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtPct(c.cohort_win_rate)}</td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtN(c.cohort_n)}</td>
                <td style={{ textAlign: 'right' }} className="mono">{fmtPct(c.final_score)}</td>
                <td>
                  <Pill tone="neutral">{c.cohort_source || '—'}</Pill>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <style>{`
        .v2-sm-mt-bar {
          width: 80px; height: 6px;
          background: var(--bg-primary);
          border-radius: 3px;
          overflow: hidden;
        }
        .v2-sm-mt-bar-fill { height: 100%; border-radius: 3px; }
      `}</style>
    </div>
  );
}

/* ── Page ───────────────────────────────────────────────────────────── */
export default function StrategyMatrix() {
  const [ticker, setTicker] = useState(DEFAULT_TICKER);
  const [tickerInput, setTickerInput] = useState(DEFAULT_TICKER);
  const [catalog, setCatalog] = useState([]);
  const [matrix, setMatrix] = useState(null);
  const [selectedSlug, setSelectedSlug] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  // Initial: catalog (once) + matrix.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch('/strategies/catalog');
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled && Array.isArray(j)) setCatalog(j);
      } catch (e) {
        if (!cancelled) setCatalog([]);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    (async () => {
      try {
        const r = await fetch(`/strategy/matrix/${encodeURIComponent(ticker)}`);
        if (!r.ok) throw new Error(`${r.status}`);
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('json')) throw new Error('non-JSON');
        const j = await r.json();
        if (!cancelled) {
          setMatrix(j);
          // Auto-select top-ranked candidate.
          if (Array.isArray(j.candidates) && j.candidates.length > 0) {
            setSelectedSlug(j.candidates[0].strategy_name);
          } else {
            setSelectedSlug(null);
          }
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) { setMatrix(null); setErr(`No strategy matrix for ${ticker}.`); }
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => { cancelled = true; };
  }, [ticker]);

  function submitTicker(e) {
    e.preventDefault();
    const t = tickerInput.trim().toUpperCase();
    if (t) setTicker(t);
  }

  // Build a slug → candidate map for easy lookup.
  const byName = useMemo(() => {
    const m = {};
    for (const c of matrix?.candidates || []) m[c.strategy_name] = c;
    return m;
  }, [matrix]);

  const selectedCandidate = selectedSlug ? byName[selectedSlug] : null;

  // Query state chips.
  const qs = matrix?.query_state || {};
  const chips = useMemo(() => {
    const out = [];
    if (qs.trend) out.push({ label: `trend: ${qs.trend}`, tone: qs.trend === 'bullish' ? 'success' : qs.trend === 'bearish' ? 'error' : 'neutral' });
    if (qs.volatility_state) out.push({ label: `vol: ${qs.volatility_state}`, tone: 'info' });
    if (qs.iv_regime) out.push({ label: `iv: ${qs.iv_regime}`, tone: 'info' });
    if (qs.intraday_regime) out.push({ label: `intraday: ${qs.intraday_regime}`, tone: 'info' });
    if (qs.gamma_state) out.push({ label: `gamma: ${qs.gamma_state}`, tone: 'info' });
    if (qs.macro_regime) out.push({ label: `macro: ${qs.macro_regime}`, tone: qs.macro_regime === 'defensive' ? 'warning' : 'info' });
    if (qs.analog_cohort_size != null) out.push({ label: `analog n: ${qs.analog_cohort_size}`, tone: 'neutral' });
    return out;
  }, [qs]);

  // De-duplicate pattern_hits.
  const patternHits = useMemo(() => {
    if (!Array.isArray(qs.pattern_hits)) return [];
    const seen = new Map();
    for (const p of qs.pattern_hits) {
      seen.set(p, (seen.get(p) || 0) + 1);
    }
    return [...seen.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20);
  }, [qs.pattern_hits]);

  return (
    <div className="v2-root v2-sm">
      <Section title="Strategy Matrix"
               subtitle={busy ? `Loading ${ticker}…` : matrix ? `${matrix.candidates?.length || 0} candidates for ${ticker}` : ''}
               actions={
                 <form onSubmit={submitTicker} className="v2-sm-ticker">
                   <input type="text"
                          value={tickerInput}
                          onChange={e => setTickerInput(e.target.value)}
                          placeholder="Ticker"
                          maxLength={10}
                          className="v2-sm-ticker__input"
                          aria-label="Ticker symbol" />
                   <button type="submit" className="v2-sm-ticker__btn">Load</button>
                   <div className="v2-sm-ticker__quick">
                     {COMMON_TICKERS.map(t => (
                       <button key={t} type="button"
                               onClick={() => { setTickerInput(t); setTicker(t); }}
                               className={`v2-sm-ticker__quick-btn ${t === ticker ? 'v2-sm-ticker__quick-btn--active' : ''}`}>
                         {t}
                       </button>
                     ))}
                   </div>
                 </form>
               }>
        {err && <AlertBanner severity="warning">{err}</AlertBanner>}

        {/* Query state chips */}
        {matrix && (
          <Card>
            <div className="v2-sm-qs">
              <div className="v2-sm-qs__title">Query State for {ticker}</div>
              <div className="v2-sm-qs__chips">
                {chips.length === 0
                  ? <span style={{ color: 'var(--text-tertiary)' }}>No regime data.</span>
                  : chips.map((c, i) => <Pill key={i} tone={c.tone} size="md">{c.label}</Pill>)}
              </div>
              {patternHits.length > 0 && (
                <div className="v2-sm-qs__patterns">
                  <span className="v2-sm-qs__patterns-lbl">Pattern hits:</span>
                  {patternHits.map(([p, n], i) => (
                    <Pill key={i} tone="info">
                      {p}{n > 1 ? ` ×${n}` : ''}
                    </Pill>
                  ))}
                </div>
              )}
            </div>
          </Card>
        )}

        <div className="v2-sm-grid">
          {/* Left rail — template catalog */}
          <Card>
            <h3 className="v2-pf-h3">
              Template Catalog
              <span className="mono" style={{ marginLeft: 8, color: 'var(--text-tertiary)', fontSize: 11 }}>
                ({catalog.length})
              </span>
            </h3>
            {catalog.length === 0
              ? <EmptyState icon="📚" message="Catalog endpoint unavailable." />
              : (
                <div className="v2-sm-tmpl-grid">
                  {catalog.map(tmpl => (
                    <StrategyTemplateCard
                      key={tmpl.slug}
                      tmpl={tmpl}
                      candidate={byName[tmpl.slug]}
                      selected={selectedSlug === tmpl.slug}
                      onClick={(slug) => byName[slug] ? setSelectedSlug(slug) : null}
                    />
                  ))}
                </div>
              )}
          </Card>

          {/* Main pane — candidates + detail */}
          <div className="v2-sm-main">
            <Card>
              <h3 className="v2-pf-h3">Ranked Candidates</h3>
              <CandidatesTable
                candidates={matrix?.candidates}
                selected={selectedSlug}
                onSelect={setSelectedSlug}
              />
            </Card>
            <Card>
              <CandidateDetail
                candidate={selectedCandidate}
                query={qs}
                ticker={ticker}
              />
            </Card>
          </div>
        </div>

        <div style={{ marginTop: 16, textAlign: 'center' }}>
          <Link to={`/v2/stock/${ticker}`} className="v2-sm-link">
            → Full stock detail for {ticker}
          </Link>
        </div>
      </Section>

      <style>{`
        .v2-sm { padding: var(--space-4) var(--space-6); }
        .v2-sm-ticker {
          display: flex; align-items: center; gap: 6px;
          flex-wrap: wrap;
        }
        .v2-sm-ticker__input {
          background: var(--bg-primary);
          border: 1px solid var(--border-default);
          color: var(--text-primary);
          border-radius: var(--radius-md);
          padding: 6px 10px;
          font-family: var(--font-mono);
          font-size: var(--font-size-sm);
          width: 100px;
          text-transform: uppercase;
        }
        .v2-sm-ticker__input:focus { outline: none; border-color: var(--accent-cyan); }
        .v2-sm-ticker__btn {
          background: var(--accent-cyan);
          color: var(--bg-primary);
          border: none;
          border-radius: var(--radius-md);
          padding: 6px 14px;
          font-weight: 700;
          font-size: var(--font-size-sm);
          cursor: pointer;
        }
        .v2-sm-ticker__btn:hover { background: var(--accent-cyan-dim); color: var(--text-primary); }
        .v2-sm-ticker__quick { display: flex; gap: 4px; margin-left: 8px; flex-wrap: wrap; }
        .v2-sm-ticker__quick-btn {
          background: transparent;
          border: 1px solid var(--border-subtle);
          color: var(--text-secondary);
          border-radius: var(--radius-md);
          padding: 4px 8px;
          font-size: 11px;
          font-family: var(--font-mono);
          cursor: pointer;
        }
        .v2-sm-ticker__quick-btn:hover { background: var(--bg-elevated); color: var(--text-primary); }
        .v2-sm-ticker__quick-btn--active {
          border-color: var(--accent-cyan);
          color: var(--accent-cyan);
        }
        .v2-sm-qs__title {
          font-size: var(--font-size-xs);
          font-weight: 700;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          margin-bottom: 8px;
        }
        .v2-sm-qs__chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
        .v2-sm-qs__patterns {
          display: flex; flex-wrap: wrap; gap: 4px; align-items: center;
          padding-top: 8px; border-top: 1px dashed var(--border-subtle);
        }
        .v2-sm-qs__patterns-lbl {
          font-size: 11px;
          color: var(--text-tertiary);
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin-right: 4px;
        }
        .v2-sm-grid {
          display: grid;
          grid-template-columns: 300px 1fr;
          gap: var(--space-4);
          margin-top: var(--space-4);
        }
        .v2-sm-tmpl-grid {
          display: flex; flex-direction: column; gap: 8px;
          max-height: 1100px;
          overflow-y: auto;
        }
        .v2-sm-main {
          display: flex; flex-direction: column; gap: var(--space-4);
        }
        .v2-pf-h3 {
          font-size: var(--font-size-base);
          font-weight: 700;
          color: var(--text-primary);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin: 0 0 var(--space-3);
        }
        .v2-sm-link {
          color: var(--accent-cyan);
          text-decoration: none;
          font-size: var(--font-size-sm);
        }
        .v2-sm-link:hover { text-decoration: underline; }
        @media (max-width: 900px) {
          .v2-sm-grid { grid-template-columns: 1fr; }
          .v2-sm-tmpl-grid { max-height: 400px; }
        }
      `}</style>
    </div>
  );
}
