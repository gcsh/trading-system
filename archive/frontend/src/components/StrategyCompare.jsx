import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ResponsiveContainer, ComposedChart, Line, XAxis, YAxis,
  Tooltip, CartesianGrid, Legend,
} from 'recharts';
import { money, num, pct, shortDate } from '../lib/format.js';
import { useTimelineViewport, useWheelZoom } from '../lib/useTimelineViewport.js';
import { useStrategies } from '../hooks/useStrategies.js';

const PRESETS = [
  { label: '3M', period: '3mo', interval: '1d' },
  { label: '6M', period: '6mo', interval: '1d' },
  { label: '1Y', period: '1y', interval: '1d' },
  { label: '5Y', period: '5y', interval: '1wk' },
];

const M = { top: 14, right: 70, bottom: 26, left: 10 };

function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(900);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => { for (const x of e) setW(x.contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/**
 * Annotated candlestick chart that overlays every selected strategy's
 * buy/sell signals (color-coded) on the same price action, plus support /
 * resistance levels and moving averages. When a single strategy is selected
 * we also shade its trades and draw stop-loss / take-profit levels.
 */
function CompareCandles({ candles, indicators, strategies, height = 420 }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  const ind = indicators || {};
  const svgWrapRef = useRef(null);

  const n = candles.length;
  const vp = useTimelineViewport(n);
  const { vStart, vCount } = vp;

  const geom = useMemo(() => {
    if (!candles.length) return null;
    const vEnd = Math.min(n, vStart + vCount);
    let lo = Infinity, hi = -Infinity;
    for (let i = vStart; i < vEnd; i++) { lo = Math.min(lo, candles[i].low); hi = Math.max(hi, candles[i].high); }
    strategies.forEach((s) => (s.trades || []).forEach((t) => {
      [t.stop_px, t.target_px].forEach((v) => {
        if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      });
    }));
    const pad = (hi - lo) * 0.07 || 1;
    lo -= pad; hi += pad;
    const iw = Math.max(60, width - M.left - M.right);
    const ih = Math.max(60, height - M.top - M.bottom);
    const xStep = iw / Math.max(1, vCount);
    const cw = Math.max(2, Math.min(xStep * 0.72, 50));
    const tToIndex = {};
    candles.forEach((c, i) => { tToIndex[c.t] = i; });
    const x = (i) => M.left + (i - vStart) * xStep + xStep / 2;
    const y = (p) => M.top + ih - ((p - lo) / (hi - lo)) * ih;
    return { lo, hi, iw, ih, xStep, cw, x, y, tToIndex, vEnd };
  }, [candles, strategies, width, height, vStart, vCount, n]);

  const liveRef = useRef(null);
  liveRef.current = geom;
  useWheelZoom(svgWrapRef, (factor) => {
    const frac = hover != null ? (hover - vStart) / Math.max(1, vCount) : 0.5;
    vp.zoom(factor, Math.max(0, Math.min(1, frac)));
  }, [hover, vStart, vCount, n]);

  // Support / resistance from the recent range (last ~60 bars).
  const sr = useMemo(() => {
    if (!candles.length) return null;
    const recent = candles.slice(-Math.min(60, candles.length));
    let res = -Infinity, sup = Infinity;
    for (const c of recent) { res = Math.max(res, c.high); sup = Math.min(sup, c.low); }
    return { res, sup };
  }, [candles]);

  if (!candles.length || !geom) {
    return <div ref={ref} style={{ height }} />;
  }

  const maPath = (series) => {
    if (!series) return '';
    let d = '';
    series.forEach((v, i) => {
      if (v == null) return;
      d += `${d ? 'L' : 'M'}${geom.x(i).toFixed(1)} ${geom.y(v).toFixed(1)} `;
    });
    return d;
  };

  const single = strategies.length === 1 ? strategies[0] : null;
  const hoveredCandle = hover != null ? candles[hover] : null;

  const handleDown = (e) => { if (e.button === 0) vp.beginDrag(e.clientX); };
  const handleMove = (e) => {
    if (vp.isDragging()) { vp.dragTo(e.clientX, geom.xStep); setHover(null); return; }
    const rect = e.currentTarget.getBoundingClientRect();
    const i = vStart + Math.floor((e.clientX - rect.left - M.left) / geom.xStep);
    setHover(i >= 0 && i < n ? i : null);
  };
  const handleUp = () => vp.endDrag();
  const handleLeave = () => { vp.endDrag(); setHover(null); };

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
        <span>🖐️ Drag to pan · Shift+scroll to zoom</span>
        {vp.isZoomed && <button className="btn small ghost" style={{ padding: '2px 8px' }} onClick={vp.reset}>Reset view</button>}
      </div>
      <div ref={svgWrapRef} style={{ position: 'relative', width: '100%', overflow: 'hidden' }}>
      <svg
        width={width} height={height}
        onMouseDown={handleDown} onMouseMove={handleMove} onMouseUp={handleUp} onMouseLeave={handleLeave}
        style={{ display: 'block', maxWidth: '100%', cursor: vp.isDragging() ? 'grabbing' : 'grab', userSelect: 'none' }}
      >
        <defs><clipPath id="cmpClip"><rect x={M.left} y={M.top} width={geom.iw} height={geom.ih} /></clipPath></defs>
        {/* price grid + labels */}
        {Array.from({ length: 5 }, (_, k) => {
          const p = geom.lo + ((geom.hi - geom.lo) * k) / 4;
          const yy = geom.y(p);
          return (
            <g key={`grid-${k}`}>
              <line x1={M.left} x2={width - M.right} y1={yy} y2={yy} stroke="var(--border)" />
              <text x={width - M.right + 4} y={yy + 3} fontSize="10" fill="var(--muted)">{money(p).replace(/\.00$/, '')}</text>
            </g>
          );
        })}

        {/* support / resistance levels */}
        {sr && (
          <g>
            <line x1={M.left} x2={width - M.right} y1={geom.y(sr.res)} y2={geom.y(sr.res)} stroke="var(--danger)" strokeWidth={1} strokeDasharray="6 4" opacity={0.55} />
            <text x={M.left + 4} y={geom.y(sr.res) - 4} fontSize="10" fill="var(--danger)" opacity={0.9}>Resistance {money(sr.res).replace(/\.00$/, '')}</text>
            <line x1={M.left} x2={width - M.right} y1={geom.y(sr.sup)} y2={geom.y(sr.sup)} stroke="var(--info)" strokeWidth={1} strokeDasharray="6 4" opacity={0.55} />
            <text x={M.left + 4} y={geom.y(sr.sup) + 12} fontSize="10" fill="var(--info)" opacity={0.9}>Support {money(sr.sup).replace(/\.00$/, '')}</text>
          </g>
        )}

        <g clipPath="url(#cmpClip)">
        {/* single-strategy trade zones + stop / target */}
        {single && (single.trades || []).map((t, k) => {
          const i1 = geom.tToIndex[t.entry_t];
          const i2 = geom.tToIndex[t.exit_t];
          if (i1 == null || i2 == null) return null;
          const x1 = geom.x(i1), x2 = geom.x(i2);
          const win = t.return_pct >= 0;
          const fill = win ? 'rgba(14,138,95,0.10)' : 'rgba(212,72,92,0.10)';
          return (
            <g key={`zone-${k}`}>
              <rect x={x1} y={M.top} width={Math.max(1, x2 - x1)} height={geom.ih} fill={fill} />
              {t.stop_px != null && (
                <line x1={x1} x2={x2} y1={geom.y(t.stop_px)} y2={geom.y(t.stop_px)} stroke="var(--danger)" strokeDasharray="4 3" strokeWidth={1} />
              )}
              {t.target_px != null && (
                <line x1={x1} x2={x2} y1={geom.y(t.target_px)} y2={geom.y(t.target_px)} stroke="var(--accent)" strokeDasharray="4 3" strokeWidth={1} />
              )}
            </g>
          );
        })}

        {/* moving averages */}
        <path d={maPath(ind.ma50)} fill="none" stroke="var(--warn)" strokeWidth={1.2} opacity={0.8} />
        <path d={maPath(ind.ma200)} fill="none" stroke="var(--purple)" strokeWidth={1.2} opacity={0.8} />

        {/* candlesticks */}
        {candles.map((c, i) => {
          const up = c.close >= c.open;
          const col = up ? 'var(--accent)' : 'var(--danger)';
          const cx = geom.x(i);
          const yO = geom.y(c.open), yC = geom.y(c.close);
          const top = Math.min(yO, yC), h = Math.max(1, Math.abs(yO - yC));
          return (
            <g key={`c-${i}`} opacity={hover === i ? 1 : 0.95}>
              <line x1={cx} x2={cx} y1={geom.y(c.high)} y2={geom.y(c.low)} stroke={col} strokeWidth={1} />
              <rect x={cx - geom.cw / 2} y={top} width={geom.cw} height={h} fill={col} />
            </g>
          );
        })}

        {/* per-strategy buy / sell markers (color-coded, staggered) */}
        {strategies.map((s, si) => (
          <g key={`mk-${s.strategy}`}>
            {(s.markers || []).map((m, k) => {
              const i = geom.tToIndex[m.t];
              if (i == null) return null;
              const cx = geom.x(i);
              const c = candles[i];
              const buy = m.action.startsWith('BUY');
              const off = 10 + si * 11;
              if (buy) {
                const yb = geom.y(c.low) + off;
                return <path key={`b-${si}-${k}`} d={`M ${cx} ${yb} L ${cx - 5} ${yb + 9} L ${cx + 5} ${yb + 9} Z`} fill={s.color} stroke="#fff" strokeWidth={0.5} />;
              }
              const yt = geom.y(c.high) - off;
              return <path key={`s-${si}-${k}`} d={`M ${cx} ${yt} L ${cx - 5} ${yt - 9} L ${cx + 5} ${yt - 9} Z`} fill={s.color} stroke="#fff" strokeWidth={0.5} />;
            })}
          </g>
        ))}

        {/* hover crosshair */}
        {hover != null && candles[hover] && (
          <line x1={geom.x(hover)} x2={geom.x(hover)} y1={M.top} y2={M.top + geom.ih} stroke="var(--muted-2)" strokeDasharray="2 3" opacity={0.5} />
        )}
        </g>

        {/* x-axis dates (within the visible window) */}
        {Array.from({ length: 6 }, (_, k) => {
          const i = Math.min(n - 1, Math.round(vStart + (vCount * k) / 5));
          const c = candles[i];
          if (!c) return null;
          return <text key={`x-${k}`} x={geom.x(i)} y={height - 8} fontSize="10" fill="var(--muted)" textAnchor="middle">{shortDate(c.t)}</text>;
        })}
      </svg>

      {hoveredCandle && (
        <div style={{ position: 'absolute', top: 8, left: 12, background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, padding: '6px 10px', fontSize: 11, boxShadow: 'var(--shadow-md)', pointerEvents: 'none' }}>
          <div style={{ color: 'var(--muted)' }}>{shortDate(hoveredCandle.t)}</div>
          <div>O {money(hoveredCandle.open)} · H {money(hoveredCandle.high)} · L {money(hoveredCandle.low)} · C <strong>{money(hoveredCandle.close)}</strong></div>
        </div>
      )}
      </div>
    </div>
  );
}

export default function StrategyCompare({ ticker }) {
  const strategies = useStrategies();
  const [selected, setSelected] = useState(['macd_momentum', 'rsi_mean_reversion', 'vwap_reversion']);
  const [preset, setPreset] = useState(PRESETS[0]);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const toggle = (s) =>
    setSelected((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));

  const runCompare = async () => {
    if (!ticker || selected.length === 0) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`/backtest/compare/${encodeURIComponent(ticker)}?strategies=${selected.join(',')}&period=${preset.period}&interval=${preset.interval}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      if (body.error) { setErr(body.error); setData(null); }
      else setData(body);
    } catch (e) {
      setErr(e.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  // Auto-run when ticker / preset / selection changes.
  useEffect(() => {
    if (ticker && selected.length) runCompare();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, preset, selected]);

  // Only chart the strategies the user has currently selected.
  const visibleStrategies = useMemo(
    () => (data?.strategies || []).filter((s) => selected.includes(s.strategy)),
    [data, selected],
  );

  const ranked = data
    ? [...data.strategies].sort((a, b) => {
        const sa = a.num_trades ? a.total_return_pct * 100 + (a.sharpe || 0) : -1e9;
        const sb = b.num_trades ? b.total_return_pct * 100 + (b.sharpe || 0) : -1e9;
        return sb - sa;
      })
    : [];

  // Equity-curve rows: one column per strategy + buy&hold benchmark.
  const equityRows = useMemo(() => {
    if (!data) return [];
    const curveByStrat = {};
    visibleStrategies.forEach((s) => {
      const byT = {};
      (s.equity_curve || []).forEach((p) => { byT[p.t] = p.equity; });
      curveByStrat[s.strategy] = byT;
    });
    const candles = data.candles || [];
    const startPx = candles.length ? candles[0].close : 1;
    return candles.map((c) => {
      const row = { t: c.t, bh: startPx ? Math.round((c.close / startPx) * 10000) : null };
      visibleStrategies.forEach((s) => {
        row[`eq_${s.strategy}`] = curveByStrat[s.strategy][c.t] ?? null;
      });
      return row;
    });
  }, [data, visibleStrategies]);

  return (
    <div className="panel col-12">
      <div className="panel-head">
        <h2>Compare strategies on <strong>{ticker || '—'}</strong></h2>
        <div className="row">
          {PRESETS.map((p) => (
            <button key={p.label} className={`btn small ${p === preset ? 'primary' : ''}`} onClick={() => setPreset(p)}>{p.label}</button>
          ))}
          <button className="btn small primary" disabled={loading || !selected.length} onClick={runCompare}>
            {loading ? 'Running…' : 'Compare'}
          </button>
        </div>
      </div>

      {/* Strategy multi-select chips */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>
          Select strategies to overlay &amp; compare ({selected.length} selected):
        </div>
        <div className="row" style={{ gap: 6 }}>
          {strategies.map(({ slug, label }) => {
            const on = selected.includes(slug);
            const color = data?.strategies?.find((x) => x.strategy === slug)?.color;
            return (
              <span
                key={slug}
                onClick={() => toggle(slug)}
                className={`pill ${on ? 'on' : 'off'}`}
                style={{ cursor: 'pointer', borderColor: on && color ? color : undefined, color: on && color ? color : undefined }}
              >
                {on && color && <span className="dot" style={{ background: color }} />}
                {label}
              </span>
            );
          })}
        </div>
      </div>

      {err && <div className="empty"><div className="title" style={{ color: 'var(--danger)' }}>Couldn't compare</div><div className="hint">{err}</div></div>}
      {loading && !data && <div className="empty">Running backtest…</div>}

      {/* Proactive suggestion */}
      {data?.suggestion && (
        <div
          className="panel"
          style={{ background: 'var(--accent-soft)', borderColor: '#c4e3d4', marginBottom: 12 }}
        >
          <div style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--accent)', fontWeight: 700, marginBottom: 4 }}>
            🤖 Bot suggestion
          </div>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{data.suggestion.headline}</div>
          <div style={{ fontSize: 13, color: 'var(--text-soft)', lineHeight: 1.5 }}>
            {data.suggestion.detail.split('**').map((part, i) => i % 2 === 1 ? <strong key={i}>{part}</strong> : part)}
          </div>
        </div>
      )}

      {/* Chart legend / key */}
      {data && visibleStrategies.length > 0 && (
        <div className="row" style={{ gap: 14, fontSize: 11, color: 'var(--muted)', margin: '4px 0 6px', flexWrap: 'wrap' }}>
          <span>▲ Buy signal</span>
          <span>▼ Sell signal</span>
          <span><span style={{ color: 'var(--warn)' }}>—</span> MA50</span>
          <span><span style={{ color: 'var(--purple)' }}>—</span> MA200</span>
          <span><span style={{ borderTop: '2px dashed var(--danger)', display: 'inline-block', width: 14, verticalAlign: 'middle' }} /> Resistance</span>
          <span><span style={{ borderTop: '2px dashed var(--info)', display: 'inline-block', width: 14, verticalAlign: 'middle' }} /> Support</span>
          <span style={{ borderLeft: '1px solid var(--border)', paddingLeft: 14 }}>
            {visibleStrategies.map((s) => (
              <span key={s.strategy} style={{ marginRight: 10, color: s.color, fontWeight: 600 }}>
                <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: s.color, marginRight: 4 }} />
                {s.strategy.replace(/_/g, ' ')}
              </span>
            ))}
          </span>
        </div>
      )}

      {/* Annotated candlestick chart with per-strategy markers */}
      {data && data.candles && data.candles.length > 1 && (
        <CompareCandles
          candles={data.candles}
          indicators={data.indicators}
          strategies={visibleStrategies}
          height={420}
        />
      )}

      {/* Equity curves: each strategy's $10k grown vs buy & hold */}
      {data && equityRows.length > 1 && visibleStrategies.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>
            Backtest equity — $10,000 run through each strategy vs buy &amp; hold (dashed)
          </div>
          <div style={{ height: 280 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={equityRows} margin={{ top: 8, right: 20, left: 8, bottom: 0 }}>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                <XAxis dataKey="t" tickFormatter={shortDate} tick={{ fontSize: 11, fill: 'var(--muted)' }} stroke="var(--border)" minTickGap={50} />
                <YAxis tick={{ fontSize: 11, fill: 'var(--muted)' }} tickFormatter={(v) => `$${(v / 1000).toFixed(1)}k`} width={56} orientation="right" stroke="var(--border)" />
                <Tooltip
                  contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
                  labelFormatter={shortDate}
                  formatter={(v, name) => [money(v), name === 'bh' ? 'buy & hold' : name.replace('eq_', '').replace(/_/g, ' ')]}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line type="monotone" dataKey="bh" name="buy & hold" stroke="var(--muted-2)" strokeWidth={1.4} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
                {visibleStrategies.map((s) => (
                  <Line key={s.strategy} type="monotone" dataKey={`eq_${s.strategy}`} name={s.strategy.replace(/_/g, ' ')} stroke={s.color} strokeWidth={1.6} dot={false} isAnimationActive={false} connectNulls />
                ))}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Comparison ranking table — real backtest P&L */}
      {data && (
        <div style={{ marginTop: 12 }}>
          <table>
            <thead>
              <tr>
                <th>Rank</th><th>Strategy</th><th className="num">Trades</th>
                <th className="num">Return</th><th className="num">vs B&amp;H</th>
                <th className="num">Win rate</th><th className="num">Profit factor</th>
                <th className="num">Max DD</th><th className="num">Sharpe</th>
              </tr>
            </thead>
            <tbody>
              {ranked.map((s, i) => {
                const ret = num(s.total_return_pct);
                const alpha = num(s.alpha_pct);
                return (
                  <tr key={s.strategy}>
                    <td>{s.num_trades ? `#${i + 1}` : '—'}</td>
                    <td>
                      <span className="dot" style={{ background: s.color, display: 'inline-block', width: 8, height: 8, borderRadius: '50%', marginRight: 6 }} />
                      <strong style={{ textTransform: 'capitalize' }}>{s.strategy.replace(/_/g, ' ')}</strong>
                    </td>
                    <td className="num">{s.num_trades}</td>
                    <td className={`num ${ret >= 0 ? 'pos' : 'neg'}`}>{s.num_trades ? pct(ret, 1, { showSign: true }) : '—'}</td>
                    <td className={`num ${alpha >= 0 ? 'pos' : 'neg'}`}>{s.num_trades ? pct(alpha, 1, { showSign: true }) : '—'}</td>
                    <td className="num">{s.bt_win_rate == null ? '—' : `${Math.round(s.bt_win_rate * 100)}%`}</td>
                    <td className="num">{s.num_trades ? num(s.profit_factor).toFixed(2) : '—'}</td>
                    <td className="num neg">{s.num_trades ? `${num(s.max_drawdown_pct).toFixed(1)}%` : '—'}</td>
                    <td className="num">{s.num_trades ? num(s.sharpe).toFixed(2) : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
            Real long/flat backtest: $10k start, all-in on a BUY signal, exit on SELL / stop / target, 2bps commission, close-to-close fills. "vs B&amp;H" is alpha over buy-and-hold. Not options-aware (uses the underlying) and no slippage modeling — directional edge, not a guarantee.
          </div>
        </div>
      )}
    </div>
  );
}
