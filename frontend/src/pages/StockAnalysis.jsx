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
// Chart standardization pass (Phase 19.x) — StockAnalysis now renders the
// authoritative TheoryChart (lightweight-charts) instead of the legacy
// SVG `AnnotatedCandleChart`. Detector observations get mapped into
// TheoryChart's per-family `markers` contract by `mapObservationsToAnnotations`.
import TheoryChart from '../components/TheoryChart.jsx';
// F4 — chart improvements: shared timeframe, shared bars hook, fullscreen wrapper.
import ChartFullscreenWrapper from '../components/ChartFullscreenWrapper.jsx';
import TimeframeSelector from '../components/TimeframeSelector.jsx';
import IntervalSelector from '../components/IntervalSelector.jsx';
import useChartTimeframe from '../hooks/useChartTimeframe.js';
import useChartInterval from '../hooks/useChartInterval.js';
import useAnalysisBars from '../hooks/swr/useAnalysisBars.js';
import useTheoryOverlays from '../hooks/swr/useTheoryOverlays.js';
import { THEORY_CATALOG, THEORY_BY_ID, migrateTheoryIds }
  from '../analysis/theoryCatalog.js';
import DrawingToolbar from '../analysis/DrawingToolbar.jsx';
import CommandPalette from '../analysis/CommandPalette.jsx';

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

// Phase 19.x chart standardization — turn the analysis page's
// `observations` array into a TheoryChart annotations dict. Each
// `family` becomes its own "theory" so the chart legend separates them
// and operators can toggle each family group independently (when wired
// into ChartFullscreenWrapper.overlayGroups).
//
// TheoryChart's marker contract (see TheoryChart.jsx around line 520):
//
//   { ts, shape: 'arrow_up'|'arrow_down'|'circle', color, label }
//
// The legacy AnnotatedCandleChart drew a triangle BELOW the candle for
// every observation, regardless of family. We preserve that visual by
// mapping every observation to `arrow_up` (which renders belowBar in
// TheoryChart's native marker layer).
function mapObservationsToAnnotations(observations, familyColors) {
  const out = {};
  for (const obs of observations || []) {
    const fam = obs.family || 'uncategorized';
    if (!out[fam]) out[fam] = { markers: [] };
    const ts = obs.timestamp;
    if (!ts) continue;
    out[fam].markers.push({
      ts,
      shape: 'arrow_up',
      color: familyColors[fam] || '#9aa5b2',
      // Phase C.1: chart-floating text labels removed. Marker arrow
      // still draws at the bar (so timing is visible), but the
      // pattern name now lives in the docked Detector Observatory
      // on the right rail instead of crowding the candles.
      label: '',
    });
  }
  return out;
}

/* Phase C.1 — Detector Observatory.
   Docked legend that replaces the floating chart text. Groups
   observations by family with a color dot, a count, the most-recent
   pattern, and its timestamp. Renders inside the fullscreen right
   rail and below the chart in normal mode. */
function DetectorObservatory({ observations, familyLabels, familyColors }) {
  const obs = observations || [];
  if (!obs.length) {
    return (
      <div className="panel" style={{
        padding: 10, fontSize: 12, color: 'var(--muted)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 6,
      }}>
        ∅ No detector observations in window.
      </div>
    );
  }
  const byFam = {};
  for (const o of obs) {
    const fam = o.family || 'uncategorized';
    if (!byFam[fam]) byFam[fam] = [];
    byFam[fam].push(o);
  }
  const fams = Object.keys(byFam).sort();
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 6,
      fontSize: 12,
    }}>
      <div style={{
        fontSize: 10, letterSpacing: '0.08em',
        textTransform: 'uppercase', color: 'var(--muted)',
        fontWeight: 700,
      }}>
        Detector hits · {obs.length}
      </div>
      {fams.map((fam) => {
        const rows = byFam[fam];
        // Newest first.
        rows.sort((a, b) => String(b.timestamp || '').localeCompare(String(a.timestamp || '')));
        const top = rows[0];
        return (
          <div key={fam} style={{
            display: 'flex', justifyContent: 'space-between',
            alignItems: 'center', gap: 8,
            padding: '6px 8px',
            background: 'rgba(255, 255, 255, 0.02)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 4,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
              <span style={{
                display: 'inline-block', width: 8, height: 8,
                borderRadius: '50%',
                background: familyColors[fam] || '#9aa5b2',
                flexShrink: 0,
              }} />
              <span style={{
                fontWeight: 600, color: 'var(--text-primary)',
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                {familyLabels[fam] || fam}
              </span>
              <span style={{ color: 'var(--muted)', fontSize: 11 }}>
                ({rows.length})
              </span>
            </div>
            <div style={{
              fontFamily: 'var(--font-mono)', fontSize: 10,
              color: 'var(--muted)', textAlign: 'right',
              whiteSpace: 'nowrap', overflow: 'hidden',
              textOverflow: 'ellipsis', maxWidth: 140,
            }}
                 title={`${top.pattern} · ${top.timestamp}`}>
              {top.pattern}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* Phase C.1 — Thesis Context accordion.
   Replaces the prior flat list (just InsiderActivityPanel +
   SmartMoneyPanel). Sections are collapsible so the operator can
   focus on the one that matters; the first section starts open by
   default. */
function ThesisAccordion({ sections }) {
  const [openIdx, setOpenIdx] = useState(0);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {sections.map((s, i) => {
        const open = i === openIdx;
        return (
          <div key={s.id}
               style={{
                 border: '1px solid var(--border-subtle)',
                 borderRadius: 6,
                 background: open ? 'var(--bg-secondary)' : 'transparent',
                 // overflow MUST be visible so the TheorySelector
                 // dropdown (and any future absolutely-positioned
                 // children) can extend past the section's bottom
                 // edge. Earlier `overflow: hidden` was clipping the
                 // dropdown to "nothing visible to select".
                 overflow: 'visible',
                 position: 'relative',
               }}>
            <button type="button"
                    onClick={() => setOpenIdx(open ? -1 : i)}
                    style={{
                      width: '100%', display: 'flex',
                      justifyContent: 'space-between', alignItems: 'center',
                      padding: '8px 10px',
                      background: 'transparent',
                      border: 'none',
                      color: 'var(--text-primary)',
                      cursor: 'pointer',
                      fontSize: 11, fontWeight: 700,
                      letterSpacing: '0.06em',
                      textTransform: 'uppercase',
                    }}>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <span aria-hidden="true">{s.icon}</span>
                {s.title}
                {s.badge != null && (
                  <span style={{
                    background: 'var(--border-subtle)',
                    color: 'var(--muted)',
                    fontSize: 10, fontWeight: 600,
                    padding: '1px 6px', borderRadius: 999,
                  }}>{s.badge}</span>
                )}
              </span>
              <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                {open ? '−' : '+'}
              </span>
            </button>
            {open && (
              <div style={{ padding: '4px 10px 10px' }}>
                {s.content}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* Phase C.2 — Theory selector dropdown (full wire-up).
   The inline picker exposes the six tier-1 theories that operators
   reach for most; the Cmd-K palette (Phase C.4) surfaces all 23
   from the shared THEORY_CATALOG. Both surfaces toggle the same
   `selectedTheories` array on the parent. */
const TIER1_THEORIES = THEORY_CATALOG.filter((t) => t.tier === 1);

function TheorySelector({ ticker, selected, onChange }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ position: 'relative' }}>
      <button type="button"
              onClick={() => setOpen((o) => !o)}
              style={{
                width: '100%', padding: '8px 10px',
                background: 'var(--bg-secondary)',
                border: '1px solid var(--border-subtle)',
                borderRadius: 6,
                color: 'var(--text-primary)',
                fontSize: 12, cursor: 'pointer',
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center',
              }}>
        <span>
          <span style={{
            fontSize: 10, letterSpacing: '0.06em',
            textTransform: 'uppercase', color: 'var(--muted)',
            marginRight: 6, fontWeight: 700,
          }}>
            Theories
          </span>
          {selected.length === 0
            ? <span style={{ color: 'var(--muted)' }}>none selected</span>
            : <span style={{ color: 'var(--text-primary)' }}>
                {selected.length} active
              </span>}
        </span>
        <span style={{ fontSize: 10, color: 'var(--muted)' }}>{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, right: 0,
          marginTop: 4, padding: 6,
          background: 'var(--bg-secondary)',
          border: '1px solid var(--border-subtle)',
          borderRadius: 6,
          // z-index has to clear (a) sibling accordion sections that
          // render below this one in DOM order, and (b) any chart
          // canvases the wrapper might layer on top. 50 is well
          // above the SPA's typical surface stack.
          zIndex: 50,
          boxShadow: '0 4px 12px rgba(0, 0, 0, 0.3)',
        }}>
          {TIER1_THEORIES.map((t) => {
            const on = selected.includes(t.id);
            return (
              <label key={t.id}
                     style={{
                       display: 'flex', alignItems: 'center', gap: 8,
                       padding: '5px 6px', cursor: 'pointer',
                       borderRadius: 4,
                       background: on ? 'rgba(95, 201, 206, 0.06)' : 'transparent',
                       fontSize: 12, color: 'var(--text-primary)',
                     }}>
                <input type="checkbox" checked={on}
                       onChange={(e) => {
                         const next = e.target.checked
                           ? [...selected, t.id]
                           : selected.filter((id) => id !== t.id);
                         onChange(next);
                       }} />
                <span style={{
                  display: 'inline-block', width: 8, height: 8,
                  borderRadius: '50%', background: t.color,
                }} />
                <span>{t.label}</span>
              </label>
            );
          })}
          <div style={{
            marginTop: 6, padding: 6,
            borderTop: '1px solid var(--border-subtle)',
            fontSize: 10, color: 'var(--muted)',
          }}>
            More via <kbd style={{
              fontFamily: 'var(--font-mono, monospace)',
              padding: '1px 4px',
              border: '1px solid var(--border-subtle)',
              borderRadius: 3,
            }}>⌘ K</kbd> palette ({THEORY_CATALOG.length} theories total)
          </div>
        </div>
      )}
    </div>
  );
}

function buildAnnotationPalettes(families, familyColors) {
  const palettes = {};
  for (const fam of families) {
    const c = familyColors[fam] || '#9aa5b2';
    palettes[fam] = { primary: c, secondary: c, tertiary: c };
  }
  return palettes;
}

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

  // F4 — 10-timeframe selector backed by canonical hook + bars trim.
  const { timeframe, setTimeframe, backendWindow, trimDays } =
    useChartTimeframe(ticker, '1D');

  // Candle interval (1m..W) with Auto = backend default for current window.
  const { interval, setInterval, backendInterval, aggregate } =
    useChartInterval(ticker, 'auto');

  // F4 — fullscreen overlay toggle state.
  const [hiddenOverlays, setHiddenOverlays] = useState({});
  const toggleOverlay = (id) => setHiddenOverlays((m) => ({ ...m, [id]: !m[id] }));

  // Phase C.2 — selected theory overlays, persisted per-ticker to
  // localStorage so navigation between tickers preserves intent.
  // migrateTheoryIds maps the old scaffold IDs (`macd`, `rsi_div`,
  // `fib`) onto the backend-canonical names the multi-overlay fetch
  // expects.
  const theoryStorageKey = `tb.analysis.theories.${ticker || 'SPY'}`;
  const [selectedTheories, setSelectedTheoriesRaw] = useState(() => {
    try {
      const v = globalThis.localStorage.getItem(theoryStorageKey);
      return migrateTheoryIds(v ? JSON.parse(v) : []);
    } catch (_) {
      return [];
    }
  });
  const setSelectedTheories = (next) => {
    const cleaned = migrateTheoryIds(next);
    setSelectedTheoriesRaw(cleaned);
    try {
      globalThis.localStorage.setItem(theoryStorageKey, JSON.stringify(cleaned));
    } catch (_) { /* localStorage disabled — fine */ }
  };
  const toggleSelectedTheory = (id) => {
    setSelectedTheories(
      selectedTheories.includes(id)
        ? selectedTheories.filter((x) => x !== id)
        : [...selectedTheories, id],
    );
  };
  // When the ticker changes, re-hydrate from the new ticker's key.
  useEffect(() => {
    try {
      const v = globalThis.localStorage.getItem(theoryStorageKey);
      setSelectedTheoriesRaw(migrateTheoryIds(v ? JSON.parse(v) : []));
    } catch (_) {
      setSelectedTheoriesRaw([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker]);

  // Phase C.3 — active drawing tool (cursor is the only wired one
  // until the canvas overlay engine lands).
  const [drawingTool, setDrawingTool] = useState('cursor');

  // Phase C.4 — Cmd-K command palette.
  const [cmdOpen, setCmdOpen] = useState(false);
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target;
      const tag = (t && t.tagName) || '';
      const isText = tag === 'INPUT' || tag === 'TEXTAREA'
        || (t && t.isContentEditable);
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setCmdOpen((v) => !v);
      } else if (!isText && e.key === '/' && !cmdOpen) {
        e.preventDefault();
        setCmdOpen(true);
      }
    };
    globalThis.addEventListener('keydown', onKey);
    return () => globalThis.removeEventListener('keydown', onKey);
  }, [cmdOpen]);

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

  // F4 — when the timeframe widget asks for a different backend window
  // than the URL has, update the URL so the existing fetch above
  // re-runs. This keeps the legacy `window` URL contract intact AND
  // routes all 10 timeframes through the same canonical hook.
  useEffect(() => {
    if (backendWindow && backendWindow !== window) {
      setSp({ window: backendWindow }, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendWindow]);

  // F4 — canonical bars (also used for client-side trim). Read through
  // the shared hook so cross-page consistency holds: any other page
  // calling useAnalysisBars(ticker, backendWindow, trimDays) gets the
  // SAME SWR cache entry and SAME trimmed bars for the same ticker.
  const { bars: canonicalBars } = useAnalysisBars(
    ticker, backendWindow, trimDays,
    { interval: backendInterval, aggregate },
  );
  const barsForChart = canonicalBars && canonicalBars.length
    ? canonicalBars
    : (data?.bars || []);

  // Phase C.2 — multi-theory overlay fetch. Returns annotation dicts
  // keyed by backend theory name. Skipped (no fetch) when nothing is
  // selected so a cold page burns zero requests on theories.
  const { annotations: theoryAnnotations } = useTheoryOverlays(
    ticker, selectedTheories, backendWindow,
  );

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
        <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          {/* F4 — 10-button timeframe row. Replaces the legacy 3-window
              row above. The selector maps each UI timeframe to the
              correct backend window + client trim via useChartTimeframe. */}
          <TimeframeSelector value={timeframe} onChange={setTimeframe} />
          <IntervalSelector value={interval} onChange={setInterval} />
          {/* Phase C.4 — Cmd-K palette opener. Visible affordance so
              new operators discover the keyboard shortcut. */}
          <button
            type="button"
            className="btn small"
            data-testid="analysis-cmdk-open"
            onClick={() => setCmdOpen(true)}
            title="Open command palette (⌘K or /)"
            style={{
              fontSize: 11,
              padding: '3px 9px',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
            }}
          >
            ⌘ K
          </button>
        </div>
      </div>

      {/* Phase 19.x — prominent ticker picker bar. The deep-link route
          (/analysis/:ticker) falls back to SPY when no ticker is in the
          URL; this picker is the primary surface to swap symbols. When
          the URL has no :ticker, the input autoFocuses so an operator
          who lands on /analysis can start typing immediately. */}
      <div className="ticker-picker-bar">
        <label htmlFor="ticker-search">Analyze</label>
        <div style={{ flex: 1, maxWidth: 480 }}>
          <TickerSearch
            id="ticker-search"
            onAdd={goTicker}
            placeholder="Type ticker (SPY, AAPL, NVDA, ...)"
            autoFocus={!tickerParam}
          />
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
            {/* Phase 19.x — Fullscreen wrapper now wires the new
                grouped overlay rail. The flat `overlays` chips stay
                visible in non-fullscreen mode (one chip per detector
                family); `overlayGroups` powers the right-rail accordion
                + Solo buttons when the operator expands. Thesis cards
                (correlation, patterns, insider, 13F) feed the right-rail
                bottom slot via `thesisCards`. */}
            {(() => {
              const fams = Array.from(new Set(
                (data.observations || []).map((o) => o.family).filter(Boolean),
              ));
              const flatOverlays = fams.map((fam) => ({
                id: fam,
                label: FAMILY_LABEL[fam] || fam,
                color: FAMILY_COLORS[fam] || '#9aa5b2',
                visible: !hiddenOverlays[fam],
              }));
              // For the grouped accordion: bucket families by axis so
              // each accordion section maps to operator-meaningful
              // groups (Price Action, Volume, Options, Structure, Other).
              const GROUP_MAP = {
                price_action: 'Price Action',
                candlesticks: 'Price Action',
                market_structure: 'Structure',
                liquidity: 'Structure',
                vwap: 'Volume',
                volume_profile: 'Volume',
                options_intel: 'Options',
              };
              const overlayGroups = {};
              for (const fam of fams) {
                const groupName = GROUP_MAP[fam] || 'Other';
                if (!overlayGroups[groupName]) overlayGroups[groupName] = [];
                overlayGroups[groupName].push({
                  key: fam,
                  label: FAMILY_LABEL[fam] || fam,
                  color: FAMILY_COLORS[fam] || '#9aa5b2',
                  enabled: !hiddenOverlays[fam],
                });
              }
              const onToggleGroupOverlay = (_group, key) => toggleOverlay(key);
              const onSoloGroupOverlay = (group, key) => {
                // Hide every other overlay in the same group.
                setHiddenOverlays((prev) => {
                  const next = { ...prev };
                  for (const item of (overlayGroups[group] || [])) {
                    next[item.key] = (item.key !== key);
                  }
                  return next;
                });
              };
              const visibleObs = (data.observations || []).filter(
                (o) => !hiddenOverlays[o.family],
              );
              const visibleFams = Array.from(new Set(
                visibleObs.map((o) => o.family).filter(Boolean),
              ));
              const detectorAnnotations = mapObservationsToAnnotations(
                visibleObs, FAMILY_COLORS,
              );
              // Phase C.2 — merge detector annotations with the
              // backend theory overlays (Bollinger / MACD / RSI div /
              // …). TheoryChart renders each key as its own theory.
              const annotations = {
                ...detectorAnnotations,
                ...(theoryAnnotations || {}),
              };
              const palettes = buildAnnotationPalettes(visibleFams, FAMILY_COLORS);
              for (const tid of Object.keys(theoryAnnotations || {})) {
                const meta = THEORY_BY_ID[tid];
                const c = meta?.color || '#9aa5b2';
                palettes[tid] = { primary: c, secondary: c, tertiary: c };
              }

              // Phase C.1 — Thesis Context accordion. Replaces the
              // prior flat stack with collapsible sections so the
              // fullscreen rail isn't an unbroken column of cards.
              const setupSection = (
                <div style={{ display: 'grid', gap: 6 }}>
                  <CandidateCorrelationChip ticker={ticker} />
                  {patternList.slice(0, 2).map((p) => {
                    const k = data.knowledge[p];
                    const t = (data.theses || {})[p];
                    const fam = (data.observations || []).find((o) => o.pattern === p)?.family || 'uncategorized';
                    return (
                      <PatternCard
                        key={`fs-${p}`}
                        pattern={p}
                        family={fam}
                        knowledge={k}
                        thesis={t}
                      />
                    );
                  })}
                  {patternList.length === 0 && (
                    <div style={{
                      padding: 10, fontSize: 12, color: 'var(--muted)',
                      border: '1px solid var(--border-subtle)',
                      borderRadius: 6,
                    }}>
                      ∅ No patterns matched the cohort grid yet.
                    </div>
                  )}
                </div>
              );
              const detectorSection = (
                <DetectorObservatory
                  observations={data.observations || []}
                  familyLabels={FAMILY_LABEL}
                  familyColors={FAMILY_COLORS}
                />
              );
              const insiderSection = (
                <div style={{ display: 'grid', gap: 8 }}>
                  <InsiderActivityPanel ticker={ticker} />
                  <SmartMoneyPanel ticker={ticker} />
                </div>
              );
              const theorySection = (
                <TheorySelector ticker={ticker}
                                selected={selectedTheories}
                                onChange={setSelectedTheories} />
              );
              const thesisCards = (
                <ThesisAccordion sections={[
                  { id: 'theories', icon: '⚙', title: 'Theories on chart',
                    badge: selectedTheories.length || null,
                    content: theorySection },
                  { id: 'setup', icon: '◎', title: 'Setup',
                    badge: patternList.length || null,
                    content: setupSection },
                  { id: 'detectors', icon: '▣', title: 'Detector hits',
                    badge: (data.observations || []).length || null,
                    content: detectorSection },
                  { id: 'insider', icon: '◆', title: 'Insider · 13F',
                    content: insiderSection },
                ]} />
              );

              // Surface timeframe + interval controls IN the wrapper so
              // they're reachable in fullscreen too — operators were
              // blind to them once the chart took over the viewport.
              const chartToolbarLeft = (
                <div style={{
                  display: 'flex', gap: 10, flexWrap: 'wrap',
                  alignItems: 'center',
                }}>
                  <TimeframeSelector value={timeframe} onChange={setTimeframe} compact />
                  <IntervalSelector value={interval} onChange={setInterval} compact />
                </div>
              );
              return (
                <ChartFullscreenWrapper
                  ticker={ticker}
                  overlays={flatOverlays}
                  onToggleOverlay={toggleOverlay}
                  overlayGroups={overlayGroups}
                  onToggleGroupOverlay={onToggleGroupOverlay}
                  onSoloGroupOverlay={onSoloGroupOverlay}
                  thesisCards={thesisCards}
                  toolbarLeft={chartToolbarLeft}
                  height={460}
                >
                  <div style={{
                    flex: 1, minHeight: 460, height: '100%',
                    display: 'flex', flexDirection: 'row',
                    gap: 6,
                  }}>
                    {/* Phase C.3 — TradingView-style left toolbar.
                        Cursor is wired; freehand drawing primitives
                        scaffold here pending the canvas overlay engine. */}
                    <DrawingToolbar
                      active={drawingTool}
                      onSelect={setDrawingTool}
                    />
                    <div style={{
                      flex: 1, minWidth: 0, height: '100%',
                      display: 'flex', flexDirection: 'column',
                    }}>
                    {barsForChart && barsForChart.length > 0 ? (
                      <TheoryChart
                        bars={barsForChart}
                        annotations={annotations}
                        palettes={palettes}
                        primaryTheory={Object.keys(annotations)[0] || null}
                      />
                    ) : (
                      <div className="panel" style={{
                        padding: 20, fontSize: 13, color: 'var(--muted)',
                      }}>
                        No bars available for {ticker}.
                      </div>
                    )}
                    </div>
                  </div>
                </ChartFullscreenWrapper>
              );
            })()}
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

      {/* Phase C.4 — Cmd-K command palette. Single surface for
          toggling any of the 23 theory overlays plus quick actions
          (clear overlays for now; more land here as they earn it). */}
      <CommandPalette
        open={cmdOpen}
        onClose={() => setCmdOpen(false)}
        selectedTheories={selectedTheories}
        onToggleTheory={(id) => toggleSelectedTheory(id)}
        actions={[
          {
            id: 'clear-theories',
            label: 'Clear all chart overlays',
            color: '#e8606e',
            onPick: () => { setSelectedTheories([]); setCmdOpen(false); },
          },
        ]}
      />
    </div>
  );
}
