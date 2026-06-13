/**
 * MITS Phase 10 — Theory Studio v2.
 *
 * Changes vs Phase 9.6:
 *
 *   - Theory picker is now MULTI-SELECT (chips + [+ Add ▼] picker).
 *     Each theory gets a distinct colour palette.
 *   - Window selector now includes 1m / 3m / 6m / 1y / 2y / 5y / max,
 *     and ``max`` truly returns 1000+ bars (backend mapping fixed).
 *   - Live toggle: when ON, the page polls every 30s during market
 *     hours / 5min off-hours and shows a "● LIVE" badge.
 *   - Signal layer: each theory may emit BUY / SELL / WATCH / option
 *     hints; the chart draws flag markers with hover popovers.
 *   - Theory Primer below the chart now has a 4th column "Latest
 *     signals" with the last 3 signals per active theory.
 *   - Saved-overlay POST / DELETE are still single-theory; multi-select
 *     does NOT participate in the saved-annotation workflow.
 */
import React, { useEffect, useMemo, useState } from 'react';
import TheoryChart from '../components/TheoryChart.jsx';
import { useTheoryMulti, useTheoryRegistry, useQuoteTick } from '../hooks/useTheory.js';
// F4 — shared chart improvements (selector + fullscreen wrap).
import ChartFullscreenWrapper from '../components/ChartFullscreenWrapper.jsx';
import TimeframeSelector from '../components/TimeframeSelector.jsx';
import useChartTimeframe, { TIMEFRAMES as F4_TIMEFRAMES }
  from '../hooks/useChartTimeframe.js';

const WINDOWS = ['1m', '3m', '6m', '1y', '2y', '5y', 'max'];

// F4 — map the canonical 10-value timeframe → /theories/multi `window` arg.
// /theories/multi only supports {1m,3m,6m,1y,2y,5y,max}; map the others
// to the closest available value. 1D / 1W aren't supported by this
// endpoint (it's daily bars) so we snap up to 1m.
const TF_TO_THEORY_WIN = {
  '1D':  '1m',
  '1W':  '1m',
  '1M':  '1m',
  '3M':  '3m',
  '6M':  '6m',
  'YTD': '1y',   // theory backend has no YTD; 1y is the closest cover.
  '1Y':  '1y',
  '3Y':  '2y',   // no 3y; 2y is the closest under, 5y over-fetches a lot.
  '5Y':  '5y',
  'MAX': 'max',
};
const COMMON_TICKERS = ['SPY', 'QQQ', 'AAPL', 'NVDA', 'TSLA', 'MSFT'];
// MITS-P10.3.4 — density levels. Forwarded as a query param so the
// backend can filter line priorities + theories can self-tune their
// emission (pivots/murrey).
const DENSITIES = [
  { value: 'simple',   label: 'Simple — key levels only' },
  { value: 'normal',   label: 'Normal — balanced (default)' },
  { value: 'detailed', label: 'Detailed — every level' },
];

// Per-theory colour palette. The chart renderer reaches into this to
// recolour neutral-coloured backend lines so each overlay is
// distinguishable when 3+ theories are selected.
export const THEORY_PALETTES = {
  pivots:            { primary: '#ffd166', secondary: '#ff9f1c', tertiary: '#ff5a5f' },
  fibonacci:         { primary: '#36c26b', secondary: '#1f6feb', tertiary: '#ff9f1c' },
  gann:              { primary: '#7a85ff', secondary: '#d63a3a', tertiary: '#1f6feb' },
  ichimoku:          { primary: '#3fb6e3', secondary: '#36c26b', tertiary: '#ff5a5f' },
  price_action:      { primary: '#ff8a3d', secondary: '#36c26b', tertiary: '#ff5a5f' },
  bollinger:         { primary: '#b87cff', secondary: '#36c26b', tertiary: '#ff5a5f' },
  donchian:          { primary: '#26d07c', secondary: '#9aa4b2', tertiary: '#ff5a5f' },
  keltner:           { primary: '#ff5a8f', secondary: '#36c26b', tertiary: '#ff5a5f' },
  ma_ribbon:         { primary: '#ffd166', secondary: '#3fb6e3', tertiary: '#b87cff' },
  avwap:             { primary: '#9be38e', secondary: '#ffd166', tertiary: '#9aa4b2' },
  rsi_divergence:    { primary: '#36c26b', secondary: '#ff5a5f', tertiary: '#7fc8a9' },
  macd_signal:       { primary: '#26d07c', secondary: '#ff5a5f', tertiary: '#9aa4b2' },
  stochastic:        { primary: '#1f6feb', secondary: '#ff9f1c', tertiary: '#9aa4b2' },
  atr_bands:         { primary: '#36c26b', secondary: '#ff5a5f', tertiary: '#9aa4b2' },
  murrey_math:       { primary: '#ffd166', secondary: '#1f6feb', tertiary: '#ff5a5f' },
  andrews_pitchfork: { primary: '#ffd166', secondary: '#36c26b', tertiary: '#ff5a5f' },
  square_of_9:       { primary: '#ffd166', secondary: '#1f6feb', tertiary: '#ff5a5f' },
  volume_profile:    { primary: '#ffd166', secondary: '#36c26b', tertiary: '#ff5a5f' },
  harmonic_patterns: { primary: '#b87cff', secondary: '#ffd166', tertiary: '#ff5a5f' },
  elliott_wave:      { primary: '#36c26b', secondary: '#ff5a5f', tertiary: '#ffd166' },
  wyckoff_phases:    { primary: '#1f6feb', secondary: '#36c26b', tertiary: '#ff5a5f' },
  smc_order_blocks:  { primary: '#36c26b', secondary: '#ff5a5f', tertiary: '#9aa4b2' },
  fair_value_gaps:   { primary: '#36c26b', secondary: '#ff5a5f', tertiary: '#ffd166' },
};

function paletteFor(theory) {
  return THEORY_PALETTES[theory] ||
    { primary: '#9aa4b2', secondary: '#ffd166', tertiary: '#ff5a5f' };
}

// ──────────────────────────────────────────────────────────────────────


function TheoryChip({ name, label, color, onRemove }) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      padding: '4px 8px',
      background: 'var(--panel-2, rgba(255,255,255,0.04))',
      border: `1.5px solid ${color}`,
      borderRadius: 999, fontSize: 12,
    }}>
      <span style={{
        display: 'inline-block', width: 8, height: 8,
        borderRadius: '50%', background: color,
      }} />
      <span style={{ color: 'var(--text)' }}>{label}</span>
      <button
        onClick={onRemove}
        style={{
          background: 'transparent', border: 'none',
          color: 'var(--muted)', cursor: 'pointer', fontSize: 13,
          padding: 0, marginLeft: 2,
        }}
        title="Remove theory"
      >×</button>
    </div>
  );
}

function AddTheoryPicker({ registry, selected, onAdd }) {
  const [open, setOpen] = useState(false);
  const available = useMemo(() => {
    if (!registry?.theories) return [];
    return registry.theories.filter((t) => !selected.includes(t.name));
  }, [registry, selected]);

  return (
    <div style={{ position: 'relative' }}>
      <button
        className="btn small"
        onClick={() => setOpen((x) => !x)}
        disabled={available.length === 0}
        style={{ padding: '4px 10px' }}
      >+ Add theory ▼</button>
      {open && available.length > 0 && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, zIndex: 30,
          marginTop: 4,
          background: 'var(--panel, #131a2c)',
          border: '1px solid var(--border, #2a3349)',
          borderRadius: 6,
          padding: 4,
          minWidth: 240,
          maxHeight: 360, overflowY: 'auto',
          boxShadow: '0 10px 32px rgba(0,0,0,0.4)',
        }}>
          {available.map((t) => {
            const p = paletteFor(t.name);
            return (
              <button key={t.name}
                      onClick={() => { onAdd(t.name); setOpen(false); }}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 8,
                        width: '100%',
                        background: 'transparent', border: 'none',
                        color: 'var(--text)', textAlign: 'left',
                        padding: '6px 8px', borderRadius: 4,
                        fontSize: 12, cursor: 'pointer',
                      }}
                      onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(255,255,255,0.05)'}
                      onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
                <span style={{
                  display: 'inline-block', width: 8, height: 8,
                  borderRadius: '50%', background: p.primary, flexShrink: 0,
                }} />
                <span>{t.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}


function LiveBadge({ live, lastTs, lastPrice, source }) {
  if (!live) return null;
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      padding: '3px 8px', borderRadius: 999,
      background: 'rgba(255, 90, 95, 0.18)',
      border: '1px solid #ff5a5f', color: '#ffb3b5',
      fontSize: 11, fontWeight: 700, letterSpacing: 0.3,
    }}>
      <span style={{
        display: 'inline-block', width: 8, height: 8,
        borderRadius: '50%', background: '#ff5a5f',
        boxShadow: '0 0 8px #ff5a5f',
        animation: 'pulse 1.4s infinite',
      }} />
      LIVE
      {lastPrice != null && lastPrice > 0 && (
        <span style={{ color: '#ffffff', fontWeight: 700, fontSize: 11 }}>
          ${Number(lastPrice).toFixed(2)}
        </span>
      )}
      {lastTs && (
        <span style={{ color: '#8593b0', fontWeight: 400, fontSize: 10 }}>
          {new Date(lastTs).toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
          })}
        </span>
      )}
      {source && (
        <span style={{ color: '#8593b0', fontWeight: 400, fontSize: 10 }}>
          ({source})
        </span>
      )}
    </div>
  );
}


function LatestSignalsColumn({ annotations, palettes, windowKey }) {
  // MITS-P10.3.3 — per-theory row with either signals OR a "no signals
  // in this window" callout. Operator gets clear, theory-specific
  // remediation hint instead of a generic blank.
  const rows = useMemo(() => {
    const out = [];
    for (const [theory, ann] of Object.entries(annotations || {})) {
      const sigs = (ann?.signals || []).slice(-3).reverse();
      out.push({ theory, palette: palettes[theory], signals: sigs, ann });
    }
    return out;
  }, [annotations, palettes]);

  if (!rows.length) {
    return (
      <div style={{ fontSize: 12.5, lineHeight: 1.55, color: 'var(--text-soft)' }}>
        No theories selected.
      </div>
    );
  }
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      {rows.map((r) => (
        <div key={r.theory}>
          <div style={{
            fontSize: 11, fontWeight: 700,
            color: (r.palette && r.palette.primary) || '#9aa4b2',
            marginBottom: 3,
            textTransform: 'capitalize',
          }}>
            {r.theory.replaceAll('_', ' ')}
          </div>
          {r.signals.length === 0 ? (
            <div style={{
              fontSize: 12, color: 'var(--muted)',
              padding: '6px 8px', marginLeft: 4,
              background: 'rgba(255,255,255,0.02)',
              border: '1px dashed rgba(255,255,255,0.08)',
              borderRadius: 4, lineHeight: 1.4,
            }}>
              No actionable signals from {r.theory.replaceAll('_', ' ')} in
              the {windowKey || '1y'} window. Try a shorter window
              (e.g. <b>1m</b> for Pivots) or add more theories.
            </div>
          ) : r.signals.map((s, i) => (
            <div key={i} style={{
              fontSize: 12, color: 'var(--text-soft)', marginLeft: 4,
              padding: '3px 0',
              borderTop: i ? '1px solid rgba(255,255,255,0.03)' : 'none',
            }}>
              <b style={{ color: ({
                BUY: '#26d07c', BUY_CALL: '#26d07c', BUY_VERTICAL_CALL: '#26d07c',
                SELL: '#ff5a5f', BUY_PUT: '#ff5a5f', BUY_VERTICAL_PUT: '#ff5a5f',
                IRON_CONDOR: '#b87cff', STRADDLE: '#b87cff',
                WATCH: '#ffd166',
                EXIT_LONG: '#9aa4b2', EXIT_SHORT: '#9aa4b2',
              }[s.action]) || '#9aa4b2' }}>{s.action}</b>
              {' '}@ ${(s.price || 0).toFixed(2)}
              {s.target_price != null && (
                <span style={{ color: 'var(--muted)' }}>
                  {' '}→ ${s.target_price.toFixed(2)}
                </span>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────


export default function TheoryStudio() {
  const { registry } = useTheoryRegistry();
  const [ticker, setTicker] = useState('SPY');
  const [selected, setSelected] = useState(['pivots']);  // default: just pivots.
  const [windowKey, setWindowKey] = useState('1y');

  // F4 — canonical 10-value timeframe driving the chart. Maps onto the
  // existing TheoryStudio windowKey via TF_TO_THEORY_WIN. We keep
  // windowKey as the on-the-wire source so existing flow + cache keys
  // stay valid.
  const { timeframe: f4Timeframe, setTimeframe: setF4Timeframe } =
    useChartTimeframe(ticker, '1Y');
  useEffect(() => {
    const mapped = TF_TO_THEORY_WIN[f4Timeframe] || '1y';
    if (mapped !== windowKey) setWindowKey(mapped);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [f4Timeframe]);

  // F4 — per-theory overlay toggle for the fullscreen wrapper.
  const [hiddenOverlays, setHiddenOverlays] = useState({});
  const toggleOverlay = (id) =>
    setHiddenOverlays((m) => ({ ...m, [id]: !m[id] }));
  const [density, setDensity] = useState('normal');     // MITS-P10.3.4
  const [live, setLive] = useState(false);
  const [citationOpen, setCitationOpen] = useState(false);
  const [primaryFocus, setPrimaryFocus] = useState('pivots');

  // MITS-P10.3.4 — forward density to backend so theories can filter
  // their own line ladder (pivots / murrey) and the route's universal
  // priority post-filter drops noise from the other 21 theories.
  const params = useMemo(() => ({ density }), [density]);
  const { payload, loading, error, refresh } = useTheoryMulti(
    ticker, selected, windowKey, params, 0, live,
  );

  // MITS Phase 10.1 — fast 1-second price tick for the LIVE indicator.
  // The heavier multi-theory analysis stays on a 30s timer above; this
  // hook just gives the chart's forming candle a real-time pulse.
  const { tick: liveTick } = useQuoteTick(ticker, live);

  // Per-theory palettes, keyed by selected theory name.
  const palettes = useMemo(() => {
    const out = {};
    selected.forEach((s) => { out[s] = paletteFor(s); });
    return out;
  }, [selected]);

  const annotations = payload?.annotations || {};
  const bars = payload?.bars;
  const barCount = payload?.bar_count ?? (bars?.length || 0);
  const minBars = payload?.min_bars_expected;

  const spot = useMemo(() => {
    if (!bars || !bars.length) return null;
    return bars[bars.length - 1]?.close ?? null;
  }, [bars]);

  // The "primary" theory drives the right-rail single-theory card.
  useEffect(() => {
    if (!selected.length) return;
    if (!selected.includes(primaryFocus)) setPrimaryFocus(selected[0]);
  }, [selected, primaryFocus]);

  const primaryAnn = annotations[primaryFocus];
  const primaryLabel = useMemo(() => {
    if (!registry?.theories) return primaryFocus;
    return registry.theories.find((t) => t.name === primaryFocus)?.label || primaryFocus;
  }, [registry, primaryFocus]);

  const addTheory = (name) => setSelected((s) => [...s, name]);
  const removeTheory = (name) => setSelected((s) => s.filter((x) => x !== name));

  return (
    <div>
      <style>{`@keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.35; }
      }`}</style>
      <div className="panel-head" style={{ marginBottom: 8 }}>
        <div>
          <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
            Theory Studio v2
            <LiveBadge live={live} lastTs={liveTick?.ts || payload?.server_ts}
                       lastPrice={liveTick?.price} source={liveTick?.source} />
          </h3>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            Pick one or more theories. Each gets a distinct colour. The
            chart draws BUY/SELL flags for any theory that emits an
            actionable signal.
          </div>
        </div>
      </div>

      {/* F4 — canonical 10-button timeframe row. The legacy 7-window
          <select> below remains as a power-user fallback (mapped 1:1 by
          the useEffect that watches f4Timeframe). */}
      <div style={{ marginBottom: 8 }}>
        <TimeframeSelector value={f4Timeframe} onChange={setF4Timeframe} />
      </div>

      {/* Controls strip. */}
      <div className="row" style={{ gap: 10, marginBottom: 12, flexWrap: 'wrap',
                                       alignItems: 'flex-end' }}>
        <label style={{ display: 'flex', flexDirection: 'column', fontSize: 11 }}>
          <span style={{ color: 'var(--muted)' }}>Ticker</span>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker((e.target.value || '').toUpperCase().trim())}
            list="theory-quick-tickers"
            style={{ width: 120 }}
          />
          <datalist id="theory-quick-tickers">
            {COMMON_TICKERS.map((t) => <option key={t} value={t} />)}
          </datalist>
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', fontSize: 11 }}>
          <span style={{ color: 'var(--muted)' }}>Window</span>
          <select value={windowKey} onChange={(e) => setWindowKey(e.target.value)}>
            {WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', fontSize: 11 }}
               title="Controls line ladder density: Simple = key levels only, Detailed = everything.">
          <span style={{ color: 'var(--muted)' }}>Density</span>
          <select value={density} onChange={(e) => setDensity(e.target.value)}>
            {DENSITIES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
          </select>
        </label>
        <button className="btn small" onClick={() => refresh()}
                disabled={loading}>{loading ? 'Loading…' : 'Reload'}</button>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6,
                        fontSize: 12 }}>
          <input type="checkbox" checked={live}
                 onChange={(e) => setLive(e.target.checked)} />
          Live
        </label>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>
          {barCount} bars
          {minBars && barCount < minBars && (
            <span style={{ color: '#ff9f1c', marginLeft: 6 }}>
              (expected ≥{minBars})
            </span>
          )}
        </span>
      </div>

      {/* Selected-theory chips. */}
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center',
        marginBottom: 12, padding: '8px 10px',
        background: 'var(--panel, rgba(255,255,255,0.02))',
        border: '1px solid var(--border, #2a3349)', borderRadius: 6,
      }}>
        {selected.length === 0 && (
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            No theories selected — pick at least one from the picker →
          </span>
        )}
        {selected.map((name) => {
          const label = registry?.theories?.find((t) => t.name === name)?.label || name;
          return (
            <TheoryChip
              key={name}
              name={name}
              label={label}
              color={paletteFor(name).primary}
              onRemove={() => removeTheory(name)}
            />
          );
        })}
        <AddTheoryPicker registry={registry} selected={selected} onAdd={addTheory} />
        {selected.length > 1 && (
          <select
            value={primaryFocus}
            onChange={(e) => setPrimaryFocus(e.target.value)}
            style={{ fontSize: 11, marginLeft: 'auto' }}
            title="Primary theory drives the right-rail and primer columns"
          >
            {selected.map((s) => {
              const lbl = registry?.theories?.find((t) => t.name === s)?.label || s;
              return <option key={s} value={s}>Focus: {lbl}</option>;
            })}
          </select>
        )}
      </div>

      {error && <div className="panel" style={{ color: 'var(--danger)', padding: 8 }}>
        {error}
      </div>}

      {/* Main row. */}
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 300px',
                    gap: 12 }}>
        <div className="theory-chart" style={{ minWidth: 0, position: 'relative' }}>
          {/* F4 — Fullscreen wrapper. Overlay chips = the selected
              theories; toggling a chip hides that theory's annotation
              set client-side. The chart's bars + annotation contract
              is preserved because we only filter the annotations dict
              passed in — TheoryChart re-renders cleanly. */}
          <ChartFullscreenWrapper
            ticker={ticker}
            overlays={selected.map((name) => ({
              id: name,
              label: registry?.theories?.find((t) => t.name === name)?.label || name,
              color: paletteFor(name).primary,
              visible: !hiddenOverlays[name],
            }))}
            onToggleOverlay={toggleOverlay}
          >
            <TheoryChart
              bars={bars}
              annotations={Object.fromEntries(
                Object.entries(annotations).filter(([k]) => !hiddenOverlays[k]),
              )}
              palettes={palettes}
              primaryTheory={primaryFocus}
              liveTick={live ? liveTick : null}
            />
          </ChartFullscreenWrapper>
          {/* Citation footer (driven by the primary theory). */}
          <div style={{
            fontSize: 11, color: 'var(--muted)', marginTop: 6,
            display: 'flex', alignItems: 'center', gap: 6,
          }}>
            <span
              onClick={() => setCitationOpen((x) => !x)}
              style={{
                cursor: 'pointer', userSelect: 'none',
                background: 'var(--panel-2)',
                border: '1px solid var(--border, #2a3349)',
                borderRadius: 999, padding: '0 6px', lineHeight: '16px',
              }}
              title="Click for math source"
            >ⓘ</span>
            {primaryAnn?.citation && (
              <span>
                {citationOpen
                  ? <>Math source: <b style={{ color: 'var(--text-soft)' }}>{primaryAnn.citation}</b></>
                  : 'Math source available — click ⓘ for citation.'}
              </span>
            )}
          </div>
        </div>

        <div style={{ minWidth: 0 }}>
          <div className="panel" style={{ padding: 10 }}>
            <div style={{
              fontSize: 11, color: 'var(--muted)',
              textTransform: 'uppercase', letterSpacing: 0.5,
            }}>Primary theory</div>
            <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 6,
                          color: paletteFor(primaryFocus).primary }}>
              {primaryLabel}
            </div>
            <div style={{ fontSize: 12 }}>
              <div><b>Pattern:</b> {primaryAnn?.pattern_name
                ? primaryAnn.pattern_name.replaceAll('_', ' ')
                : '—'}</div>
              <div><b>Confidence:</b>{' '}
                {primaryAnn?.confidence != null
                  ? `${Math.round(primaryAnn.confidence * 100)}%` : '—'}</div>
              <div><b>Bars analysed:</b> {barCount}{' '}
                <span style={{ color: 'var(--muted)' }}>
                  ({payload?.bar_source || '—'})
                </span>
              </div>
              {spot != null && (
                <div><b>Spot:</b> ${spot.toFixed(2)}</div>
              )}
              <div><b>Active theories:</b> {selected.length}</div>
              <div><b>Signals emitted:</b>{' '}
                {Object.values(annotations).reduce(
                  (n, a) => n + (a?.signals?.length || 0), 0)}
              </div>
            </div>
          </div>

          {Object.keys(annotations).length > 0 && (
            <div className="panel" style={{ padding: 10, marginTop: 8 }}>
              <div style={{
                fontSize: 11, color: 'var(--muted)',
                textTransform: 'uppercase', letterSpacing: 0.5,
                marginBottom: 6,
              }}>Latest signals</div>
              <LatestSignalsColumn annotations={annotations} palettes={palettes} windowKey={windowKey} />
            </div>
          )}
        </div>
      </div>

      {/* Theory Primer — 4-column row. */}
      {primaryAnn?.primer && (primaryAnn.primer.what_it_measures
                              || primaryAnn.primer.how_to_read
                              || primaryAnn.primer.key_levels_now) && (
        <div className="panel" style={{ marginTop: 14, padding: 14 }}>
          <div style={{
            fontSize: 11, color: 'var(--muted)',
            textTransform: 'uppercase', letterSpacing: 0.6,
            marginBottom: 10,
          }}>
            Theory Primer — {primaryLabel}
          </div>
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
            gap: 16,
          }}>
            <div>
              <h4 style={{ margin: '0 0 6px', fontSize: 13, color: 'var(--text)' }}>
                What this theory measures
              </h4>
              <div style={{ fontSize: 12.5, lineHeight: 1.55, color: 'var(--text-soft)' }}>
                {primaryAnn.primer.what_it_measures || '—'}
              </div>
            </div>
            <div>
              <h4 style={{ margin: '0 0 6px', fontSize: 13, color: 'var(--text)' }}>
                How to read this chart
              </h4>
              <div style={{ fontSize: 12.5, lineHeight: 1.55, color: 'var(--text-soft)' }}>
                {primaryAnn.primer.how_to_read || '—'}
              </div>
            </div>
            <div>
              <h4 style={{ margin: '0 0 6px', fontSize: 13, color: 'var(--text)' }}>
                Key levels right now
              </h4>
              <div style={{ fontSize: 12.5, lineHeight: 1.55, color: 'var(--text-soft)' }}>
                {primaryAnn.primer.key_levels_now || '—'}
              </div>
            </div>
            <div>
              <h4 style={{ margin: '0 0 6px', fontSize: 13, color: 'var(--text)' }}>
                Latest signals (all active theories)
              </h4>
              <LatestSignalsColumn annotations={annotations} palettes={palettes} windowKey={windowKey} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
