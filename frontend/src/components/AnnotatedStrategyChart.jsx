import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { money, shortDate, shortTime } from '../lib/format.js';
import { useTimelineViewport, useWheelZoom } from '../lib/useTimelineViewport.js';
import { useLivePrice } from '../lib/useLivePrice.js';
import { pickLiveBadge } from '../lib/liveBadge.js';

const PRESETS = [
  { label: '1D', period: '1d', interval: '5m' },
  { label: '5D', period: '5d', interval: '15m' },
  { label: '1M', period: '1mo', interval: '1d' },
  { label: '3M', period: '3mo', interval: '1d' },
  { label: '6M', period: '6mo', interval: '1d' },
  { label: '1Y', period: '1y', interval: '1d' },
  { label: '5Y', period: '5y', interval: '1wk' },
];
const M = { top: 14, right: 70, bottom: 26, left: 46 };

// In-flight drawing tools for self-analysis.
const DRAW_TOOLS = [
  { k: 'select', icon: '🖱', label: 'Select / pan' },
  { k: 'trendline', icon: '╱', label: 'Trend line' },
  { k: 'hline', icon: '─', label: 'Horizontal line' },
  { k: 'rect', icon: '▭', label: 'Rectangle / zone' },
  { k: 'fib', icon: 'ƒ', label: 'Fibonacci' },
  { k: 'text', icon: 'T', label: 'Text label' },
];
const FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

const THEORY_META = {
  moving_avg: { label: 'Moving Averages', blurb: 'Average price over 50 & 200 bars — shows the trend and acts as support/resistance.' },
  support_resistance: { label: 'Support & Resistance', blurb: 'Price floors (support) and ceilings (resistance) drawn from past turning points.' },
  bollinger: { label: 'Bollinger Bands', blurb: 'A volatility envelope — price often snaps back from the upper/lower edges.' },
  vwap: { label: 'VWAP', blurb: 'Volume-weighted average price — the "fair value" line many pros watch.' },
  fibonacci: { label: 'Fibonacci', blurb: 'Retracement levels (38.2% / 50% / 61.8%) where pullbacks often pause.' },
  waves: { label: 'Elliott / Swing Waves', blurb: 'Maps the up & down swings as a 1-2-3-4-5 then A-B-C structure.' },
  trend_channel: { label: 'Trend Channel', blurb: 'A regression channel — price tends to travel inside this sloped band.' },
};
const THEORY_ORDER = ['moving_avg', 'support_resistance', 'trend_channel', 'bollinger', 'vwap', 'fibonacci', 'waves'];

function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(820);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => { for (const x of e) setW(x.contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export default function AnnotatedStrategyChart({ strategy, ticker, height = 460 }) {
  const [data, setData] = useState(null);
  const [preset, setPreset] = useState(PRESETS[3]);  // default 3M — dense enough to look natural full-width
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [hover, setHover] = useState(null);
  const [theories, setTheories] = useState(new Set(['moving_avg', 'support_resistance']));
  const [xcheck, setXcheck] = useState(null);
  const [xbusy, setXbusy] = useState(false);
  const [tool, setTool] = useState('select');
  const [drawings, setDrawings] = useState([]);
  const [draft, setDraft] = useState(null);
  const stratRef = useRef(null);

  // Drawings persist per ticker (anchored to time + price so they survive
  // pan/zoom/interval changes).
  useEffect(() => {
    if (!ticker) return;
    try {
      const saved = JSON.parse(localStorage.getItem(`tb-draw-${ticker}`));
      setDrawings(Array.isArray(saved) ? saved : []);
    } catch { setDrawings([]); }
    setTool('select'); setDraft(null);
  }, [ticker]);
  const persistDraw = (next) => {
    setDrawings(next);
    if (ticker) localStorage.setItem(`tb-draw-${ticker}`, JSON.stringify(next));
  };
  const [ref, width] = useWidth();
  const svgWrapRef = useRef(null);
  const reqRef = useRef(0);
  const live = useLivePrice(ticker, { enabled: !!ticker, intervalMs: 4000 });

  const loadData = useCallback(async (showLoading = true) => {
    if (!strategy || !ticker) return;
    const myReq = ++reqRef.current;
    if (showLoading) setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`/backtest/${encodeURIComponent(strategy)}/${encodeURIComponent(ticker)}?period=${preset.period}&interval=${preset.interval}`);
      if (!r.ok) {
        if (myReq !== reqRef.current) return;
        // Stream B is migrating /backtest to return HTTP 404 for unknown
        // strategies; keep the legacy {error: ...} body handling below for
        // one release in case the deploy lands out of order.
        if (r.status === 404) {
          setErr('Strategy not registered');
        } else {
          setErr(`Couldn't load chart (HTTP ${r.status})`);
        }
        if (showLoading) setData(null);
        return;
      }
      const body = await r.json();
      if (myReq !== reqRef.current) return; // a newer request superseded this one
      if (body.error) { setErr(body.error); if (showLoading) setData(null); }
      else setData(body);
    } catch (e) { if (myReq === reqRef.current && showLoading) { setErr(e.message); setData(null); } }
    finally { if (myReq === reqRef.current && showLoading) setLoading(false); }
  }, [strategy, ticker, preset]);

  useEffect(() => { loadData(true); }, [loadData]);

  // Independent cross-check vs a second source (on demand, per ticker).
  useEffect(() => { setXcheck(null); }, [ticker]);
  const verify = async () => {
    if (!ticker) return;
    setXbusy(true);
    try {
      const r = await fetch(`/market/validate/${encodeURIComponent(ticker)}`);
      setXcheck(await r.json());
    } catch (e) { setXcheck({ status: 'error', note: e.message }); }
    finally { setXbusy(false); }
  };

  useEffect(() => {
    if (data && data.strategy !== stratRef.current) {
      stratRef.current = data.strategy;
      setTheories(new Set(data.default_theories || ['moving_avg', 'support_resistance']));
    }
  }, [data]);

  const rawCandles = data?.candles || [];
  // Fold the live price into the most recent bar so the current candle "forms"
  // (close/high/low tick) in real time instead of sitting static until the next
  // backtest refetch.
  const candles = useMemo(() => {
    if (!rawCandles.length || !(live && live.price > 0)) return rawCandles;
    const out = rawCandles.slice();
    const last = out[out.length - 1];
    out[out.length - 1] = {
      ...last,
      close: live.price,
      high: Math.max(last.high, live.price),
      low: Math.min(last.low, live.price),
    };
    return out;
  }, [rawCandles, live && live.price]);
  const trades = data?.backtest?.trades || [];
  const ind = data?.indicators || {};
  const ov = data?.overlays || {};
  const has = (t) => theories.has(t);
  const toggle = (t) => setTheories((s) => { const n = new Set(s); n.has(t) ? n.delete(t) : n.add(t); return n; });
  const intraday = preset.interval.endsWith('m') || preset.interval.endsWith('h');
  const xFmt = intraday ? shortTime : shortDate;

  const n = candles.length;
  const vp = useTimelineViewport(n);
  const { vStart, vCount } = vp;

  const geom = useMemo(() => {
    if (!candles.length) return null;
    const vEnd = Math.min(n, vStart + vCount);
    let lo = Infinity, hi = -Infinity;
    for (let i = vStart; i < vEnd; i++) { lo = Math.min(lo, candles[i].low); hi = Math.max(hi, candles[i].high); }
    if (has('support_resistance')) (ov.support_resistance || []).forEach((l) => { lo = Math.min(lo, l.price); hi = Math.max(hi, l.price); });
    if (has('fibonacci')) (ov.fibonacci?.levels || []).forEach((l) => { lo = Math.min(lo, l.price); hi = Math.max(hi, l.price); });
    const pad = (hi - lo) * 0.06 || 1;
    lo -= pad; hi += pad;
    const iw = Math.max(60, width - M.left - M.right);
    const volH = Math.max(44, Math.min(Math.round((height - M.top - M.bottom) * 0.16), 120));
    const ih = Math.max(50, height - M.top - M.bottom - volH - 6);
    const volTop = M.top + ih + 6;
    let maxVol = 0;
    for (let i = vStart; i < vEnd; i++) maxVol = Math.max(maxVol, candles[i].volume || 0);
    const xStep = iw / Math.max(1, vCount);
    // Body keeps a constant ~72% of its slot (TradingView-like) so candles fill
    // the width at any candle count instead of leaving big gaps on wide charts.
    const cw = Math.max(2, Math.min(xStep * 0.72, 50));
    const tToIndex = {};
    candles.forEach((c, i) => { tToIndex[c.t] = i; });
    const x = (i) => M.left + (i - vStart) * xStep + xStep / 2;
    const y = (p) => M.top + ih - ((p - lo) / (hi - lo)) * ih;
    return { lo, hi, iw, ih, xStep, cw, x, y, tToIndex, vEnd, volTop, volH, maxVol };
  }, [candles, trades, width, height, theories, ov, vStart, vCount, n]);

  // Keep latest geometry available to the (non-React) wheel listener.
  const liveRef = useRef(null);
  liveRef.current = geom;
  useWheelZoom(svgWrapRef, (factor) => {
    // Zoom around the hovered candle if any, else the center.
    const g = liveRef.current;
    const frac = hover != null && g ? (hover - vStart) / Math.max(1, vCount) : 0.5;
    vp.zoom(factor, Math.max(0, Math.min(1, frac)));
  }, [hover, vStart, vCount, n]);

  // Auto-refresh for new bars — paused while the user is panned/zoomed in so
  // it never yanks the view out from under them. Intraday refreshes faster.
  const vpRef = useRef(vp); vpRef.current = vp;
  useEffect(() => {
    const ms = intraday ? 15000 : 45000;
    const id = setInterval(() => {
      const v = vpRef.current;
      if (v && !v.isZoomed && !v.isDragging()) loadData(false);
    }, ms);
    return () => clearInterval(id);
  }, [loadData, intraday]);

  // Map a candle timestamp → absolute index (exact, else nearest by date) so
  // drawings stay anchored across pan/zoom/interval changes.
  const indexForTime = (t) => {
    if (geom && t in geom.tToIndex) return geom.tToIndex[t];
    let best = 0, bd = Infinity;
    const target = new Date(t).getTime();
    for (let i = 0; i < n; i++) {
      const d = Math.abs(new Date(candles[i].t).getTime() - target);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  };
  const eventToData = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1, vStart + Math.floor((e.clientX - rect.left - M.left) / geom.xStep)));
    const frac = Math.max(0, Math.min(1, (e.clientY - rect.top - M.top) / geom.ih));
    return { t: candles[i]?.t, price: geom.hi - frac * (geom.hi - geom.lo) };
  };

  const drawingMode = tool !== 'select';
  const handleDown = (e) => {
    if (e.button !== 0 || !geom) return;
    if (!drawingMode) { vp.beginDrag(e.clientX); return; }
    const p = eventToData(e);
    if (tool === 'hline') { persistDraw([...drawings, { id: Date.now(), type: 'hline', a: p }]); return; }
    if (tool === 'text') {
      const txt = window.prompt('Label text:');
      if (txt) persistDraw([...drawings, { id: Date.now(), type: 'text', a: p, text: txt }]);
      setTool('select');
      return;
    }
    setDraft({ id: Date.now(), type: tool, a: p, b: p });
  };
  const handleMove = (e) => {
    if (!geom) return;
    if (drawingMode) { if (draft) setDraft({ ...draft, b: eventToData(e) }); return; }
    if (vp.isDragging()) { vp.dragTo(e.clientX, geom.xStep); setHover(null); return; }
    const rect = e.currentTarget.getBoundingClientRect();
    const i = vStart + Math.floor((e.clientX - rect.left - M.left) / geom.xStep);
    setHover(i >= 0 && i < n ? i : null);
  };
  const handleUp = () => {
    if (drawingMode) { if (draft) { persistDraw([...drawings, draft]); setDraft(null); } return; }
    vp.endDrag();
  };
  const handleLeave = () => { vp.endDrag(); setHover(null); if (draft) setDraft(null); };

  // Render one drawing (or the in-progress draft) as SVG.
  const renderShape = (d, isDraft) => {
    const op = isDraft ? 0.65 : 1;
    if (d.type === 'hline') {
      const y = geom.y(d.a.price);
      return (
        <g key={d.id}>
          <line x1={M.left} x2={width - M.right} y1={y} y2={y} stroke="#06b6d4" strokeWidth={1.3} opacity={op} />
          <text x={M.left + 2} y={y - 3} fontSize="9" fill="#06b6d4">{money(d.a.price).replace(/\.00$/, '')}</text>
        </g>
      );
    }
    const ax = geom.x(indexForTime(d.a.t)), ay = geom.y(d.a.price);
    const bx = geom.x(indexForTime(d.b?.t ?? d.a.t)), by = geom.y(d.b?.price ?? d.a.price);
    if (d.type === 'trendline') {
      return <line key={d.id} x1={ax} y1={ay} x2={bx} y2={by} stroke="var(--warn)" strokeWidth={1.6} opacity={op} />;
    }
    if (d.type === 'rect') {
      return <rect key={d.id} x={Math.min(ax, bx)} y={Math.min(ay, by)} width={Math.abs(bx - ax)} height={Math.abs(by - ay)}
        fill="#6d5efc" fillOpacity={0.12 * op} stroke="#6d5efc" strokeWidth={1} opacity={op} />;
    }
    if (d.type === 'text') {
      return <text key={d.id} x={ax} y={ay} fontSize="12" fontWeight="700" fill="var(--text)" opacity={op}>{d.text}</text>;
    }
    if (d.type === 'fib') {
      const hp = Math.max(d.a.price, d.b?.price ?? d.a.price);
      const lp = Math.min(d.a.price, d.b?.price ?? d.a.price);
      return (
        <g key={d.id} opacity={op}>
          {FIB_RATIOS.map((r) => {
            const price = hp - r * (hp - lp);
            const y = geom.y(price);
            return (
              <g key={r}>
                <line x1={Math.min(ax, bx)} x2={width - M.right} y1={y} y2={y} stroke="var(--warn)" strokeWidth={0.8} strokeDasharray="2 4" />
                <text x={Math.min(ax, bx) + 2} y={y - 2} fontSize="9" fill="var(--warn)">{(r * 100).toFixed(1)}% · {money(price).replace(/\.00$/, '')}</text>
              </g>
            );
          })}
        </g>
      );
    }
    return null;
  };

  const linePath = (series) => {
    if (!geom || !series) return '';
    let d = '';
    for (let i = Math.max(0, vStart - 1); i < Math.min(n, geom.vEnd + 1); i++) {
      const v = series[i];
      if (v == null) { continue; }
      d += `${d ? 'L' : 'M'}${geom.x(i).toFixed(1)} ${geom.y(v).toFixed(1)} `;
    }
    return d;
  };

  return (
    <div ref={ref}>
      <div className="panel-head">
        <div>
          <h2 style={{ margin: 0 }}>{(strategy || '').replace(/_/g, ' ')} on <strong>{ticker}</strong></h2>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            {data?.backtest ? (
              <>
                {data.backtest.num_trades} trades over this window · {data.backtest.total_return_pct >= 0 ? '+' : ''}{data.backtest.total_return_pct}% vs buy & hold {data.backtest.buy_hold_return_pct >= 0 ? '+' : ''}{data.backtest.buy_hold_return_pct}%
                {data.backtest.win_rate != null && data.backtest.num_trades > 0 && (
                  <> · <strong style={{ color: 'var(--text-soft)' }}>{Math.round(data.backtest.win_rate * 100)}% win rate</strong> in this trend</>
                )}
              </>
            ) : ''}
          </div>
        </div>
        <div className="row">
          {PRESETS.map((p) => (
            <button key={p.label} className={`btn small ${p === preset ? 'primary' : ''}`} onClick={() => setPreset(p)} title={`${p.period} · ${p.interval} candles`}>{p.label}</button>
          ))}
        </div>
      </div>

      <div style={{ marginBottom: 8 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 5 }}>Overlay theories (click to study them on this chart):</div>
        <div className="row" style={{ gap: 6 }}>
          {THEORY_ORDER.map((t) => (
            <span key={t} onClick={() => toggle(t)} className={`pill ${has(t) ? 'on' : 'off'}`} style={{ cursor: 'pointer' }} title={THEORY_META[t].blurb}>
              {THEORY_META[t].label}
            </span>
          ))}
        </div>
      </div>

      {err && <div className="empty"><div className="title" style={{ color: 'var(--danger)' }}>Couldn't load</div><div className="hint">{err}</div></div>}
      {loading && !data && <div className="empty">Running backtest…</div>}

      {data && geom && (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
            <span>{drawingMode
              ? `✏️ Drawing: ${(DRAW_TOOLS.find((t) => t.k === tool) || {}).label} — click/drag on the chart · pick the cursor to pan`
              : `🖐️ Drag to pan · +/− or Shift/pinch-scroll to zoom · ✏️ tools on the left${intraday ? ' · intraday' : ''}`}</span>
            <span className="row" style={{ gap: 10 }}>
              {data?.data_quality && (
                <span
                  onClick={verify}
                  title={`${data.data_quality.bars} bars checked — ${data.data_quality.note}. Source: ${data.data_quality.source}${data.data_quality.adjusted ? ', split/dividend-adjusted' : ''}. Click to cross-check against an independent source (Nasdaq).`}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: data.data_quality.ok ? 'var(--accent)' : 'var(--warn)', fontWeight: 600, cursor: 'pointer' }}
                >
                  {data.data_quality.ok ? '✓' : '⚠'} data checked · Yahoo{data.data_quality.adjusted ? ' (adj)' : ''}
                  {xbusy ? ' · verifying…'
                    : xcheck ? (
                      xcheck.status === 'ok' ? ` · ✓ confirmed by ${xcheck.agree_count}/${xcheck.checked} source${xcheck.checked === 1 ? '' : 's'} (${xcheck.sources.map((s) => s.name).join(', ')})`
                        : xcheck.status === 'no_reference' ? ' · no 2nd source — add a free key'
                          : ` · ${xcheck.note || xcheck.status}`)
                      : ' · click to cross-check'}
                </span>
              )}
              {live && live.price > 0 && (() => {
                const badge = pickLiveBadge(live);
                const color = {
                  success: 'var(--accent)', warning: '#ffd166',
                  danger: '#e8606e', muted: 'var(--muted)',
                }[badge.tone] || 'var(--muted)';
                return (
                  <span title={badge.title} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color, fontWeight: 600 }}>
                    <span className="dot pulse" style={{ width: 6, height: 6, borderRadius: '50%', background: color, display: 'inline-block' }} />
                    {badge.label}
                    <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· {live.source}</span>
                  </span>
                );
              })()}
              <span className="row" style={{ gap: 4 }}>
                <button className="btn small ghost" style={{ padding: '2px 9px', fontWeight: 700 }} title="Zoom out — show more candles" onClick={() => vp.zoom(1.4, 0.5)}>−</button>
                <button className="btn small ghost" style={{ padding: '2px 9px', fontWeight: 700 }} title="Zoom in — fewer candles" onClick={() => vp.zoom(0.7, 0.5)}>+</button>
                {vp.isZoomed && <button className="btn small ghost" style={{ padding: '2px 8px' }} onClick={vp.reset}>Reset</button>}
              </span>
            </span>
          </div>
          <div ref={svgWrapRef} style={{ position: 'relative', width: '100%', overflow: 'hidden' }}>
            {/* in-flight drawing tool palette */}
            <div style={{ position: 'absolute', left: 4, top: 6, zIndex: 3, display: 'flex', flexDirection: 'column', gap: 3,
              background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, padding: 3, boxShadow: 'var(--shadow-sm)' }}>
              {DRAW_TOOLS.map((t) => (
                <button key={t.k} title={t.label} onClick={() => setTool(t.k)}
                  style={{ width: 30, height: 30, display: 'grid', placeItems: 'center', borderRadius: 6, fontSize: 14, cursor: 'pointer',
                    border: '1px solid ' + (tool === t.k ? 'var(--accent)' : 'transparent'),
                    background: tool === t.k ? 'var(--accent-soft)' : 'transparent',
                    color: tool === t.k ? 'var(--accent)' : 'var(--text-soft)' }}>{t.icon}</button>
              ))}
              {drawings.length > 0 && (
                <button title="Clear all drawings" onClick={() => persistDraw([])}
                  style={{ width: 30, height: 30, display: 'grid', placeItems: 'center', borderRadius: 6, fontSize: 14, cursor: 'pointer',
                    border: '1px solid transparent', background: 'transparent', color: 'var(--danger)', borderTop: '1px solid var(--border)' }}>🗑</button>
              )}
            </div>
            <svg
              width={width} height={height}
              onMouseDown={handleDown} onMouseMove={handleMove} onMouseUp={handleUp} onMouseLeave={handleLeave}
              style={{ display: 'block', maxWidth: '100%', cursor: drawingMode ? 'crosshair' : (vp.isDragging() ? 'grabbing' : 'grab'), userSelect: 'none' }}
            >
              <defs>
                <clipPath id="plotClip"><rect x={M.left} y={M.top} width={geom.iw} height={geom.ih} /></clipPath>
              </defs>
              {Array.from({ length: 5 }, (_, k) => {
                const p = geom.lo + ((geom.hi - geom.lo) * k) / 4;
                const yy = geom.y(p);
                return (
                  <g key={k}>
                    <line x1={M.left} x2={width - M.right} y1={yy} y2={yy} stroke="var(--border)" />
                    <text x={width - M.right + 4} y={yy + 3} fontSize="10" fill="var(--muted)">{money(p).replace(/\.00$/, '')}</text>
                  </g>
                );
              })}

              <g clipPath="url(#plotClip)">
                {has('trend_channel') && ov.trend_channel && (() => {
                  const { slope, intercept, half_width, n: cn } = ov.trend_channel;
                  const x0 = geom.x(0), x1 = geom.x(cn - 1);
                  const yMid0 = geom.y(intercept), yMid1 = geom.y(slope * (cn - 1) + intercept);
                  const yU0 = geom.y(intercept + half_width), yU1 = geom.y(slope * (cn - 1) + intercept + half_width);
                  const yL0 = geom.y(intercept - half_width), yL1 = geom.y(slope * (cn - 1) + intercept - half_width);
                  return (
                    <g>
                      <polygon points={`${x0},${yU0} ${x1},${yU1} ${x1},${yL1} ${x0},${yL0}`} fill="var(--info)" fillOpacity={0.06} />
                      <line x1={x0} y1={yU0} x2={x1} y2={yU1} stroke="var(--info)" strokeWidth={1} strokeDasharray="3 3" opacity={0.7} />
                      <line x1={x0} y1={yMid0} x2={x1} y2={yMid1} stroke="var(--info)" strokeWidth={1} opacity={0.5} />
                      <line x1={x0} y1={yL0} x2={x1} y2={yL1} stroke="var(--info)" strokeWidth={1} strokeDasharray="3 3" opacity={0.7} />
                    </g>
                  );
                })()}

                {has('bollinger') && ov.bollinger && (
                  <g>
                    <path d={linePath(ov.bollinger.upper)} fill="none" stroke="var(--purple)" strokeWidth={1} opacity={0.6} />
                    <path d={linePath(ov.bollinger.mid)} fill="none" stroke="var(--purple)" strokeWidth={0.8} strokeDasharray="3 3" opacity={0.5} />
                    <path d={linePath(ov.bollinger.lower)} fill="none" stroke="var(--purple)" strokeWidth={1} opacity={0.6} />
                  </g>
                )}

                {has('fibonacci') && ov.fibonacci && ov.fibonacci.levels.map((l) => (
                  <g key={`fib-${l.ratio}`}>
                    <line x1={M.left} x2={width - M.right} y1={geom.y(l.price)} y2={geom.y(l.price)} stroke="var(--warn)" strokeWidth={0.8} strokeDasharray="2 4" opacity={0.55} />
                    <text x={M.left + 2} y={geom.y(l.price) - 2} fontSize="9" fill="var(--warn)" opacity={0.9}>{(l.ratio * 100).toFixed(1)}%</text>
                  </g>
                ))}

                {has('support_resistance') && (ov.support_resistance || []).map((l, k) => {
                  const col = l.kind === 'resistance' ? 'var(--danger)' : 'var(--info)';
                  return (
                    <g key={`sr-${k}`}>
                      <line x1={M.left} x2={width - M.right} y1={geom.y(l.price)} y2={geom.y(l.price)} stroke={col} strokeWidth={1} strokeDasharray="6 4" opacity={0.5} />
                      <text x={M.left + 2} y={geom.y(l.price) + (l.kind === 'resistance' ? -3 : 11)} fontSize="9" fill={col} opacity={0.9}>{l.kind === 'resistance' ? 'R' : 'S'} {money(l.price).replace(/\.00$/, '')}</text>
                    </g>
                  );
                })}

                {has('vwap') && ov.vwap && <path d={linePath(ov.vwap)} fill="none" stroke="#06b6d4" strokeWidth={1.3} opacity={0.85} />}

                {has('moving_avg') && (
                  <g>
                    <path d={linePath(ind.ma50)} fill="none" stroke="var(--warn)" strokeWidth={1.2} opacity={0.85} />
                    <path d={linePath(ind.ma200)} fill="none" stroke="var(--purple)" strokeWidth={1.2} opacity={0.85} />
                  </g>
                )}

                {trades.map((t, k) => {
                  const i1 = geom.tToIndex[t.entry_t];
                  const i2 = geom.tToIndex[t.exit_t];
                  if (i1 == null || i2 == null || i1 === i2) return null;  // skip zero-duration
                  const x1 = geom.x(i1); const x2 = geom.x(i2);
                  const isOpen = t.reason === 'open';
                  const win = t.return_pct >= 0;
                  const fill = isOpen ? 'rgba(35,131,226,0.10)' : (win ? 'rgba(14,138,95,0.12)' : 'rgba(212,72,92,0.12)');
                  return (
                    <g key={`zone-${k}`}>
                      <rect x={x1} y={M.top} width={Math.max(1, x2 - x1)} height={geom.ih} fill={fill} />
                      {t.stop_px != null && <line x1={x1} x2={x2} y1={geom.y(t.stop_px)} y2={geom.y(t.stop_px)} stroke="var(--danger)" strokeDasharray="4 3" strokeWidth={1} />}
                      {t.target_px != null && <line x1={x1} x2={x2} y1={geom.y(t.target_px)} y2={geom.y(t.target_px)} stroke="var(--accent)" strokeDasharray="4 3" strokeWidth={1} />}
                    </g>
                  );
                })}

                {candles.map((c, i) => {
                  if (i < vStart - 1 || i > geom.vEnd) return null;
                  const up = c.close >= c.open;
                  const col = up ? 'var(--accent)' : 'var(--danger)';
                  const cx = geom.x(i);
                  const yO = geom.y(c.open), yC = geom.y(c.close);
                  const top = Math.min(yO, yC), h = Math.max(1, Math.abs(yO - yC));
                  return (
                    <g key={`c-${i}`}>
                      <line x1={cx} x2={cx} y1={geom.y(c.high)} y2={geom.y(c.low)} stroke={col} strokeWidth={Math.max(1, geom.cw * 0.12)} />
                      <rect x={cx - geom.cw / 2} y={top} width={geom.cw} height={h} fill={col} />
                    </g>
                  );
                })}

                {has('waves') && ov.waves && ov.waves.length > 1 && (
                  <g>
                    <polyline points={ov.waves.map((w) => `${geom.x(w.index)},${geom.y(w.price)}`).join(' ')} fill="none" stroke="var(--purple)" strokeWidth={1.4} opacity={0.8} />
                    {ov.waves.map((w, k) => (
                      <g key={`w-${k}`}>
                        <circle cx={geom.x(w.index)} cy={geom.y(w.price)} r={2.5} fill="var(--purple)" />
                        <text x={geom.x(w.index)} y={geom.y(w.price) + (w.kind === 'H' ? -7 : 14)} fontSize="11" fontWeight="700" fill="var(--purple)" textAnchor="middle">{w.label}</text>
                      </g>
                    ))}
                  </g>
                )}

                {trades.map((t, k) => {
                  const i1 = geom.tToIndex[t.entry_t];
                  const i2 = geom.tToIndex[t.exit_t];
                  if (i1 == null || i2 == null || i1 === i2) return null;  // skip zero-duration
                  const x1 = geom.x(i1), x2 = geom.x(i2);
                  const yE = geom.y(t.entry_px), yX = geom.y(t.exit_px);
                  const isOpen = t.reason === 'open';
                  const win = t.return_pct >= 0;
                  return (
                    <g key={`mk-${k}`}>
                      <path d={`M ${x1} ${yE + 16} L ${x1 - 5} ${yE + 26} L ${x1 + 5} ${yE + 26} Z`} fill="var(--accent)" />
                      <g transform={`translate(${x1 - 18}, ${yE + 30})`}>
                        <rect width="58" height="14" rx="3" fill="var(--accent)" opacity="0.92" />
                        <text x="29" y="10" fontSize="9" fill="#fff" textAnchor="middle">BUY {money(t.entry_px).replace(/\.\d+$/, '')}</text>
                      </g>
                      {isOpen ? (
                        // Still holding at the data edge — not a real exit signal.
                        <g transform={`translate(${x2 - 28}, ${yX - 44})`}>
                          <rect width="80" height="14" rx="3" fill="var(--info)" opacity="0.92" />
                          <text x="40" y="10" fontSize="9" fill="#fff" textAnchor="middle">HOLDING {t.return_pct >= 0 ? '+' : ''}{t.return_pct}%</text>
                        </g>
                      ) : (
                        <>
                          <path d={`M ${x2} ${yX - 16} L ${x2 - 5} ${yX - 26} L ${x2 + 5} ${yX - 26} Z`} fill={win ? 'var(--accent)' : 'var(--danger)'} />
                          <g transform={`translate(${x2 - 26}, ${yX - 44})`}>
                            <rect width="74" height="14" rx="3" fill={win ? 'var(--accent)' : 'var(--danger)'} opacity="0.92" />
                            <text x="37" y="10" fontSize="9" fill="#fff" textAnchor="middle">SELL {t.return_pct >= 0 ? '+' : ''}{t.return_pct}%</text>
                          </g>
                        </>
                      )}
                    </g>
                  );
                })}

                {/* user drawings (self-analysis) + in-progress draft */}
                {drawings.map((d) => renderShape(d, false))}
                {draft && renderShape(draft, true)}

                {hover != null && candles[hover] && (
                  <line x1={geom.x(hover)} x2={geom.x(hover)} y1={M.top} y2={M.top + geom.ih} stroke="var(--muted-2)" strokeDasharray="2 3" opacity={0.5} />
                )}
              </g>

              {/* live price line + pulsing marker */}
              {live && live.price > 0 && (() => {
                const y = Math.max(M.top, Math.min(M.top + geom.ih, geom.y(live.price)));
                return (
                  <g key="liveline">
                    <line x1={M.left} x2={width - M.right} y1={y} y2={y} stroke="var(--accent)" strokeWidth={1} strokeDasharray="3 2" opacity={0.85} />
                    <circle cx={width - M.right} cy={y} r={4} fill="var(--accent)" style={{ animation: 'pulse 1.5s ease-in-out infinite' }} />
                    <g transform={`translate(${width - M.right + 2}, ${y})`}>
                      <rect x={0} y={-7} width={58} height={14} rx={3} fill="var(--accent)" />
                      <text x={29} y={3} fontSize="9" fontWeight="700" fill="#fff" textAnchor="middle">{money(live.price).replace(/\.00$/, '')}</text>
                    </g>
                  </g>
                );
              })()}

              {/* volume sub-pane */}
              <text x={M.left} y={geom.volTop - 2} fontSize="9" fill="var(--muted)">Volume</text>
              {candles.map((c, i) => {
                if (i < vStart - 1 || i > geom.vEnd) return null;
                const up = c.close >= c.open;
                const h = geom.maxVol ? (Number(c.volume || 0) / geom.maxVol) * geom.volH : 0;
                return (
                  <rect key={`vol-${i}`} x={geom.x(i) - geom.cw / 2} y={geom.volTop + geom.volH - h}
                    width={geom.cw} height={Math.max(0.5, h)} fill={up ? 'var(--accent)' : 'var(--danger)'} opacity={0.4} />
                );
              })}

              {Array.from({ length: 6 }, (_, k) => {
                const i = Math.round(vStart + (vCount * k) / 5);
                const c = candles[Math.min(i, n - 1)];
                if (!c) return null;
                return <text key={`x-${k}`} x={geom.x(Math.min(i, n - 1))} y={height - 8} fontSize="10" fill="var(--muted)" textAnchor="middle">{xFmt(c.t)}</text>;
              })}
            </svg>

            {hover != null && candles[hover] && (
              <div style={{ position: 'absolute', top: 8, left: 12, background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, padding: '6px 10px', fontSize: 11, boxShadow: 'var(--shadow-md)', pointerEvents: 'none' }}>
                <div style={{ color: 'var(--muted)' }}>{shortDate(candles[hover].t)}{intraday ? ` ${shortTime(candles[hover].t)}` : ''}</div>
                <div>O {money(candles[hover].open)} · H {money(candles[hover].high)} · L {money(candles[hover].low)} · C <strong>{money(candles[hover].close)}</strong></div>
              </div>
            )}
          </div>
        </>
      )}

      {data && (
        <div className="panel" style={{ marginTop: 12, background: 'var(--panel-2)' }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>How this strategy traded</div>
          <div style={{ fontSize: 13.5, lineHeight: 1.5, marginBottom: 8 }}>{data.explanation}</div>
          {trades.length === 0 ? (
            <div style={{ color: 'var(--muted)', fontSize: 13 }}>No trades on {ticker} in this window — the entry conditions never triggered. The overlay theories above still show how the price behaved.</div>
          ) : (
            <div className="scroll" style={{ maxHeight: 200 }}>
              <table>
                <thead><tr><th>Entry</th><th className="num">Entry px</th><th>Exit</th><th className="num">Exit px</th><th className="num">Return</th><th>Why exit</th></tr></thead>
                <tbody>
                  {trades.map((t, i) => (
                    <tr key={i}>
                      <td>{shortDate(t.entry_t)}</td>
                      <td className="num">{money(t.entry_px)}</td>
                      <td>{shortDate(t.exit_t)}</td>
                      <td className="num">{money(t.exit_px)}</td>
                      <td className={`num ${t.return_pct >= 0 ? 'pos' : 'neg'}`}>{t.return_pct >= 0 ? '+' : ''}{t.return_pct}%</td>
                      <td><span className={`pill ${t.reason === 'target' ? 'on' : t.reason === 'stop' ? 'danger' : 'off'}`}>{t.reason}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
