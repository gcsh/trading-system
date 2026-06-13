/**
 * MITS Phase 3 — per-stock analysis page.
 *
 * URL: /analysis/:ticker
 *
 * Layout:
 *   - Top: ticker selector + window toggle (Today / 5d / All).
 *   - Main: annotated candlestick chart (detector hits marked).
 *   - Side: per-pattern thesis cards (posterior, thesis paragraph,
 *     suggested setup gated on posterior > 60% and N >= 30,
 *     invalidation rules, "see similar trades" modal).
 *   - Bottom: AI-composed summary paragraph for the ticker.
 *
 * Nav: from Watchlist row → /analysis/:ticker; from KnowledgeGraph cell
 * drill-down → /analysis/:ticker?pattern=X to pre-focus a card.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import TickerSearch from '../components/TickerSearch.jsx';
import AnnotatedCandleChart from '../components/AnnotatedCandleChart.jsx';

const WINDOWS = [
  { id: 'today', label: 'Today' },
  { id: '5d', label: '5 Days' },
  { id: 'all', label: 'All' },
];

const FAMILY_LABEL = {
  candlesticks: 'Candlesticks',
  price_action: 'Price Action',
  market_structure: 'Market Structure',
  liquidity: 'Liquidity',
  vwap: 'VWAP',
  volume_profile: 'Volume Profile',
  options_intel: 'Options Intel',
  uncategorized: 'Other',
};

const FAMILY_COLORS = {
  candlesticks: '#5b9bd5',
  price_action: '#71c587',
  market_structure: '#a073d4',
  liquidity: '#e89a4c',
  vwap: '#5fc9ce',
  volume_profile: '#e6c95f',
  options_intel: '#e8606e',
  uncategorized: '#9aa5b2',
};

function fmtPct(v, digits = 0) {
  if (v == null || isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

function ConfidenceBar({ band, posterior }) {
  if (!band || band[0] == null || band[1] == null) return null;
  const lo = band[0] * 100;
  const hi = band[1] * 100;
  const post = (posterior || 0) * 100;
  return (
    <div style={{ marginTop: 6 }}>
      <div style={{ fontSize: 10, color: 'var(--muted)' }}>
        Wilson 95% CI [{lo.toFixed(0)}%, {hi.toFixed(0)}%]
      </div>
      <div style={{
        position: 'relative', height: 6, background: 'var(--panel-2)',
        borderRadius: 3, marginTop: 3, overflow: 'hidden',
      }}>
        <div style={{
          position: 'absolute', left: `${lo}%`, width: `${hi - lo}%`,
          height: '100%', background: 'var(--accent)', opacity: 0.4,
        }} />
        <div style={{
          position: 'absolute', left: `${post}%`, top: -2, width: 2,
          height: 10, background: 'var(--text)',
        }} />
      </div>
    </div>
  );
}

function SimilarTradesModal({ pattern, similar, onClose }) {
  if (!similar) return null;
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(13,20,36,0.55)',
        zIndex: 250, display: 'grid', placeItems: 'center', padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--panel)', border: '1px solid var(--border)',
          borderRadius: 14, width: 'min(720px, 96vw)',
          maxHeight: '80vh', padding: 18, overflow: 'auto',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <h3 style={{ margin: 0, flex: 1 }}>Similar past trades · {pattern}</h3>
          <button className="btn small" onClick={onClose}>Close</button>
        </div>
        {similar.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: 13 }}>
            No historical analogs persisted yet.
          </div>
        ) : (
          <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
                <th style={{ padding: '6px 4px' }}>Date</th>
                <th>Regime</th>
                <th>Vol</th>
                <th>Horizon</th>
                <th style={{ textAlign: 'right' }}>Return</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {similar.map((s) => {
                const ret = s.return_pct != null ? `${(s.return_pct * 100).toFixed(1)}%` : '—';
                return (
                  <tr key={s.observation_id} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={{ padding: '6px 4px' }}>{(s.timestamp || '').slice(0, 10)}</td>
                    <td>{s.regime}</td>
                    <td>{s.vol_state}</td>
                    <td>{s.horizon}</td>
                    <td style={{
                      textAlign: 'right',
                      color: s.return_pct >= 0 ? 'var(--accent)' : 'var(--danger)',
                    }}>{ret}</td>
                    <td>{s.was_winner == null ? '—' : (s.was_winner ? 'W' : 'L')}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}


function PatternCard({ pattern, family, knowledge, thesis }) {
  const [showSimilar, setShowSimilar] = useState(false);
  if (!knowledge) {
    return null;
  }
  const color = FAMILY_COLORS[family] || '#9aa5b2';
  const post = knowledge.posterior_win_rate;
  const n = knowledge.sample_size || 0;
  const action = thesis?.suggested_action;
  return (
    <div
      className="panel"
      style={{
        padding: 12, marginBottom: 10,
        borderLeft: `4px solid ${color}`,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
        <strong style={{ fontSize: 14 }}>
          {pattern.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
        </strong>
        <span className="pill" style={{ background: color + '33', color, fontSize: 10 }}>
          {FAMILY_LABEL[family] || family}
        </span>
      </div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 2 }}>
        <span style={{ fontSize: 24, fontWeight: 700, color: post >= 0.6 ? 'var(--accent)' : 'var(--text)' }}>
          {fmtPct(post)}
        </span>
        <span style={{ color: 'var(--muted)', fontSize: 11 }}>
          posterior · N={n} · {knowledge.regime} regime
        </span>
      </div>
      <ConfidenceBar band={knowledge.confidence_band} posterior={post} />
      {thesis?.headline && (
        <div style={{ marginTop: 8, fontWeight: 600, fontSize: 13 }}>
          {thesis.headline}
        </div>
      )}
      {thesis?.grade_explainer && (
        <div style={{
          marginTop: 4, fontSize: 12, color: 'var(--text-soft)',
          fontStyle: 'italic', lineHeight: 1.4,
        }}>
          {thesis.grade_explainer}
        </div>
      )}
      {thesis?.thesis_paragraph && (
        <div style={{ marginTop: 6, fontSize: 12.5, color: 'var(--text-soft)', lineHeight: 1.45 }}>
          {thesis.thesis_paragraph}
        </div>
      )}
      {action && (
        <div style={{
          marginTop: 10, padding: 10, background: 'var(--panel-2)',
          border: '1px dashed ' + color, borderRadius: 8, fontSize: 12,
        }}>
          <div style={{ fontWeight: 700, marginBottom: 4, color }}>
            Suggested setup
          </div>
          <div>
            {action.action} · strike <strong>{action.strike ?? '—'}</strong> · DTE {action.dte}
            {action.strike_source && (
              <span
                title={action.strike_source === 'chain'
                  ? 'Strike read from the listed options chain via ThetaData.'
                  : 'Chain unavailable — strike arithmetic-snapped to the nearest standard increment.'}
                style={{
                  marginLeft: 6, fontSize: 10, opacity: 0.75,
                  color: action.strike_source === 'chain'
                    ? '#5fc9ce' : '#e89a4c',
                }}
              >
                {action.strike_source === 'chain' ? '(from chain)' : '(snap fallback)'}
              </span>
            )}
          </div>
          <div>target +{action.target_premium_pct}% / stop -{action.stop_premium_pct}%</div>
          {action.rationale && (
            <div style={{ marginTop: 4, color: 'var(--muted)' }}>{action.rationale}</div>
          )}
        </div>
      )}
      {thesis?.invalidation?.length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <div style={{ color: 'var(--muted)', fontSize: 11, marginBottom: 3 }}>
            Invalidation
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {thesis.invalidation.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}
      <div style={{ marginTop: 8 }}>
        <button className="btn small ghost" onClick={() => setShowSimilar(true)}>
          See similar trades ({(knowledge.similar_outcomes || []).length})
        </button>
      </div>
      {showSimilar && (
        <SimilarTradesModal
          pattern={pattern}
          similar={knowledge.similar_outcomes || []}
          onClose={() => setShowSimilar(false)}
        />
      )}
    </div>
  );
}


function InsiderActivityPanel({ ticker }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetch(`/analysis/${encodeURIComponent(ticker)}/insider?days=90`)
      .then((r) => r.json()).then((d) => {
        if (alive) { setData(d); setLoading(false); }
      })
      .catch(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [ticker]);
  if (loading) {
    return (
      <div className="panel" style={{ padding: 10, fontSize: 12 }}>
        Loading insider activity…
      </div>
    );
  }
  if (!data || !data.row_count) {
    return (
      <div className="panel" style={{ padding: 10, fontSize: 12,
                                                color: 'var(--muted)' }}>
        No insider Form 4 activity in last 90 days for {ticker}.
      </div>
    );
  }
  const net = data.net_count;
  return (
    <div className="panel" style={{ padding: 12, fontSize: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                          alignItems: 'center', marginBottom: 6 }}>
        <strong>Insider Activity (90d)</strong>
        {data.cluster_buy_30d && (
          <span className="pill" style={{
            fontSize: 10, padding: '2px 6px',
            background: 'rgba(95,201,206,0.18)', color: '#5fc9ce',
          }}>
            Cluster Buy · {data.cluster_distinct_buyers_30d} insiders
          </span>
        )}
      </div>
      <div style={{ display: 'flex', gap: 12, marginBottom: 8 }}>
        <div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>BUYS</div>
          <div style={{ fontWeight: 700, color: '#71c587' }}>
            {data.buys_count}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>SELLS</div>
          <div style={{ fontWeight: 700, color: '#e8606e' }}>
            {data.sells_count}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>NET</div>
          <div style={{ fontWeight: 700,
                              color: net >= 0 ? '#71c587' : '#e8606e' }}>
            {net >= 0 ? '+' : ''}{net}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>NET $</div>
          <div style={{ fontWeight: 700,
                              color: data.net_value_usd >= 0 ? '#71c587' : '#e8606e' }}>
            ${(data.net_value_usd / 1000).toFixed(0)}k
          </div>
        </div>
      </div>
      {(data.top_transactions || []).slice(0, 3).map((tx) => (
        <div key={tx.id} style={{
          fontSize: 11, padding: '4px 0',
          borderTop: '1px solid var(--border, #2a2f37)',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span>{tx.transaction_date}</span>
            <span style={{
              color: tx.transaction_code === 'P' ? '#71c587'
                  : tx.transaction_code === 'S' ? '#e8606e'
                  : 'var(--muted)',
            }}>
              {tx.transaction_code}
              {tx.total_value && ` · $${(Math.abs(tx.total_value) / 1000).toFixed(0)}k`}
            </span>
          </div>
          <div style={{ color: 'var(--text-soft)' }}>
            {tx.insider_name} {tx.insider_role && `· ${tx.insider_role}`}
          </div>
          {tx.source_url && (
            <a href={tx.source_url} target="_blank" rel="noreferrer"
                style={{ fontSize: 10, color: '#5fc9ce' }}>
              EDGAR ↗
            </a>
          )}
        </div>
      ))}
    </div>
  );
}

function SmartMoneyPanel({ ticker }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetch(`/analysis/${encodeURIComponent(ticker)}/13f`)
      .then((r) => r.json()).then((d) => {
        if (alive) { setData(d); setLoading(false); }
      })
      .catch(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [ticker]);
  if (loading) {
    return (
      <div className="panel" style={{ padding: 10, fontSize: 12 }}>
        Loading smart money…
      </div>
    );
  }
  if (!data || !data.latest_quarter) {
    return (
      <div className="panel" style={{ padding: 10, fontSize: 12,
                                                color: 'var(--muted)' }}>
        No 13F coverage for {ticker} yet.
      </div>
    );
  }
  const dirColor = data.smart_money_direction === 'added' ? '#71c587'
    : data.smart_money_direction === 'trimmed' ? '#e8606e' : 'var(--muted)';
  return (
    <div className="panel" style={{ padding: 12, fontSize: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                          alignItems: 'center', marginBottom: 6 }}>
        <strong>Smart Money (13F)</strong>
        <span style={{
          fontSize: 10, padding: '2px 6px',
          background: `${dirColor}22`, color: dirColor,
          borderRadius: 4, textTransform: 'uppercase',
        }}>
          {data.smart_money_direction}
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6 }}>
        Latest quarter: {data.latest_quarter} · {data.fund_count} funds tracked
      </div>
      {(data.top_funds || []).slice(0, 5).map((f) => (
        <div key={f.id} style={{
          padding: '4px 0',
          borderTop: '1px solid var(--border, #2a2f37)',
          fontSize: 11,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <strong>{(f.fund_name || '?').slice(0, 30)}</strong>
            <span>{f.shares?.toLocaleString?.()} sh</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between',
                              color: 'var(--text-soft)' }}>
            <span>${(f.value_usd / 1e6).toFixed(1)}M</span>
            {f.change_from_prior_qtr != null && (
              <span style={{ color: f.change_from_prior_qtr > 0
                  ? '#71c587'
                  : f.change_from_prior_qtr < 0 ? '#e8606e' : 'var(--muted)' }}>
                {f.change_from_prior_qtr > 0 ? '+' : ''}
                {f.change_from_prior_qtr?.toLocaleString?.()}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}


// MITS Phase 14.B — candidate-aware portfolio context chip. Renders
// "Would correlate XX% with HELD" + the worst-correlated peer near the
// suggested-action card so the operator can see at a glance whether a
// new entry would duplicate an existing position's exposure.
function CandidateCorrelationChip({ ticker }) {
  const [ctx, setCtx] = useState(null);
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    fetch(
      `/portfolio/context?candidate=${encodeURIComponent(ticker)}&direction=LONG`,
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => !cancelled && setCtx(body))
      .catch(() => !cancelled && setCtx(null));
    return () => { cancelled = true; };
  }, [ticker]);

  if (!ctx) return null;
  const peer = ctx.candidate_max_correlation_peer;
  const rho = Number(ctx.candidate_max_correlation);
  if (!peer || !Number.isFinite(rho)) return null;
  const danger = Math.abs(rho) >= 0.85;
  return (
    <div
      className={danger ? 'pill danger' : 'pill info'}
      style={{ fontSize: 11, marginBottom: 8, display: 'inline-block' }}
      title={`Pairwise return correlation against open book (last 60 trading days).`}
    >
      Would correlate <strong>{(rho * 100).toFixed(0)}%</strong> with
      open <strong>{peer}</strong>
      {danger && ' — correlation cap would block this entry'}
    </div>
  );
}


export default function StockAnalysis() {
  const { ticker: tickerParam } = useParams();
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const ticker = (tickerParam || 'SPY').toUpperCase();
  const window = sp.get('window') || 'today';
  const preselected = sp.get('pattern');

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true); setError(null);
      try {
        const r = await fetch(
          `/analysis/${encodeURIComponent(ticker)}?window=${encodeURIComponent(window)}`,
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = await r.json();
        if (active) setData(body);
      } catch (e) {
        if (active) setError(e.message);
      } finally {
        if (active) setLoading(false);
      }
    };
    load();
    return () => { active = false; };
  }, [ticker, window]);

  const patternList = useMemo(() => {
    if (!data?.knowledge) return [];
    return Object.keys(data.knowledge).sort();
  }, [data]);

  const goTicker = (next) => {
    if (!next) return;
    navigate(`/analysis/${encodeURIComponent(next.toUpperCase())}?window=${window}`);
  };

  return (
    <div>
      <div className="panel-head" style={{ marginBottom: 8 }}>
        <div>
          <h2 style={{ margin: 0 }}>Analysis · {ticker}</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            Per-stock chart with detector hits + corpus-grounded theses.
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <span style={{ width: 160 }}>
            <TickerSearch onAdd={goTicker} placeholder="change ticker..." />
          </span>
          {WINDOWS.map((w) => (
            <button
              key={w.id}
              className={`btn small ${window === w.id ? 'primary' : ''}`}
              onClick={() => setSp({ window: w.id })}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {loading && <div style={{ padding: 24 }}>Loading {ticker} analysis...</div>}
      {error && <div className="pill warning">Couldn't load analysis: {error}</div>}

      {data && (
        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12, alignItems: 'start' }}>
          <div>
            {data.bar_source && data.bar_source !== 'none' && (
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 4 }}>
                <span
                  className="pill"
                  title={data.bar_source === 'thetadata'
                    ? 'Bars served by ThetaData (authoritative).'
                    : data.bar_source === 'yfinance'
                      ? 'Bars served by yfinance fallback.'
                      : 'Bar source unknown.'}
                  style={{
                    fontSize: 10,
                    background: data.bar_source === 'thetadata'
                      ? 'rgba(95,201,206,0.18)'
                      : 'rgba(232,154,76,0.18)',
                    color: data.bar_source === 'thetadata'
                      ? '#5fc9ce'
                      : '#e89a4c',
                  }}
                >
                  {data.bar_source === 'thetadata' ? 'ThetaData' : data.bar_source === 'yfinance' ? 'yfinance' : data.bar_source}
                </span>
              </div>
            )}
            <AnnotatedCandleChart
              bars={data.bars || []}
              observations={data.observations || []}
              ticker={ticker}
              height={420}
            />
            {data.summary && (
              <div className="panel" style={{ marginTop: 10, padding: 12, fontSize: 13 }}>
                <div style={{ fontSize: 11, letterSpacing: '0.04em', textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 4 }}>
                  AI summary · {ticker}
                </div>
                {data.summary}
              </div>
            )}
          </div>
          <div>
            <CandidateCorrelationChip ticker={ticker} />
            <h3 style={{ margin: '4px 0 8px', fontSize: 14 }}>
              Today's analysis ({patternList.length} pattern{patternList.length === 1 ? '' : 's'})
            </h3>
            {patternList.length === 0 && (
              <div className="panel" style={{ padding: 12, fontSize: 13, color: 'var(--muted)' }}>
                No detector hits in this window, or the corpus has no
                matching cohort cells yet.
              </div>
            )}
            {patternList.map((p) => {
              const k = data.knowledge[p];
              const t = (data.theses || {})[p];
              const fam = (data.observations || []).find((o) => o.pattern === p)?.family || 'uncategorized';
              return (
                <div
                  key={p}
                  id={`pattern-${p}`}
                  style={
                    preselected === p
                      ? { boxShadow: '0 0 0 2px var(--accent)', borderRadius: 10 }
                      : null
                  }
                >
                  <PatternCard pattern={p} family={fam} knowledge={k} thesis={t} />
                </div>
              );
            })}
            <div style={{ marginTop: 12, display: 'grid', gap: 10 }}>
              <InsiderActivityPanel ticker={ticker} />
              <SmartMoneyPanel ticker={ticker} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
