/**
 * LiveTapeChart — candle history for any ticker in the scan universe
 * with the bot's own buy/sell markers overlaid.
 *
 * Improvements over the chips-only version:
 *   • Source: /authority/scan-universe (every ticker the bot will scan,
 *     not just ones with recent trades)
 *   • Dropdown selector (scales to 30+ tickers without crowding)
 *   • Period selector (1D / 5D / 1M / 3M / 6M)
 *   • Live price + change % in header (auto-refreshing)
 *   • Better axis labels + grid
 *   • Trade markers (▲ buy, ▼ sell) at exact price/time
 *   • Hover tooltip with date + price
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const PERIODS = [
  { key: '1D', period: '1d',  interval: '5m'  },
  { key: '5D', period: '5d',  interval: '15m' },
  { key: '1M', period: '1mo', interval: '1d'  },
  { key: '3M', period: '3mo', interval: '1d'  },
  { key: '6M', period: '6mo', interval: '1d'  },
];

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(900);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setW(Math.max(280, e.contentRect.width));
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function formatPrice(n) {
  if (n == null) return '—';
  if (n >= 1000) return n.toFixed(0);
  if (n >= 100) return n.toFixed(2);
  return n.toFixed(2);
}

export default function LiveTapeChart() {
  const [universe, setUniverse] = useState([]);
  const [trades, setTrades] = useState([]);
  const [ticker, setTicker] = useState('SPY');
  const [period, setPeriod] = useState(PERIODS[1]);
  const [candles, setCandles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [hover, setHover] = useState(null);
  const wrapRef = useRef(null);
  const width = 900;  // fixed — SVG scales responsively

  // Load scan universe + trade list once.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [u, ts] = await Promise.all([
          api('/authority/scan-universe'),
          api('/trades/list?limit=200'),
        ]);
        if (!active) return;
        const tickerList = u.tickers || [];
        setUniverse(tickerList);
        setTrades(ts || []);
        // Initial pick: most-recently-traded ticker if any, else first
        // ticker in the universe, else SPY.
        const recent = (ts || [])[0]?.ticker;
        if (recent && tickerList.includes(recent)) setTicker(recent);
        else if (tickerList.length) setTicker(tickerList[0]);
      } catch (e) {
        setError(e.message);
      }
    })();
    return () => { active = false; };
  }, []);

  // Reload candles when ticker or period changes.
  useEffect(() => {
    if (!ticker) { setLoading(false); return; }
    let active = true;
    setLoading(true);
    (async () => {
      try {
        const c = await api(`/market/candles/${ticker}?period=${period.period}&interval=${period.interval}`);
        if (!active) return;
        const arr = c?.candles || c || [];
        setCandles(Array.isArray(arr) ? arr : []);
        setError(null);
      } catch (e) {
        setError(e.message);
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [ticker, period]);

  // Live trade refresh — pull trades for this ticker every 8s.
  useEffect(() => {
    const id = setInterval(async () => {
      try { setTrades(await api('/trades/list?limit=200')); }
      catch { /* swallow */ }
    }, 8000);
    return () => clearInterval(id);
  }, []);

  const tradesForTicker = useMemo(
    () => trades.filter((t) => t.ticker === ticker),
    [trades, ticker],
  );

  // Build chart geometry. Fixed viewBox dimensions — SVG scales
  // responsively via `style.width=100%` + preserveAspectRatio. Avoids
  // letting useWidth drive viewBox into oblivion when the column is
  // narrow OR the page reflows.
  const chart = useMemo(() => {
    if (!candles.length) return null;
    const M = { t: 16, r: 14, b: 30, l: 56 };
    const H = 260;
    const W = 900;

    const points = candles.map((c) => {
      const t = c.time || c.t || c.timestamp;
      const close = Number(c.close ?? c.c);
      const open = Number(c.open ?? c.o);
      const high = Number(c.high ?? c.h);
      const low = Number(c.low ?? c.l);
      return { t: new Date(t).getTime(), close, open, high, low };
    }).filter((p) => Number.isFinite(p.close));

    if (!points.length) return null;

    const xs = points.map((p) => p.t);
    const ys = points.map((p) => p.close);
    const ymin = Math.min(...ys) * 0.998;
    const ymax = Math.max(...ys) * 1.002;
    const xmin = Math.min(...xs);
    const xmax = Math.max(...xs);
    const xScale = (x) => M.l + (W - M.l - M.r) * ((x - xmin) / (xmax - xmin || 1));
    const yScale = (y) => H - M.b - (H - M.t - M.b) * ((y - ymin) / (ymax - ymin || 1));

    const path = points.map((p, i) => {
      const x = xScale(p.t); const y = yScale(p.close);
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    // Area fill below the line for richer visual.
    const areaPath = path
      + ` L${xScale(xmax).toFixed(1)},${H - M.b}`
      + ` L${xScale(xmin).toFixed(1)},${H - M.b} Z`;

    const last = points[points.length - 1];
    const first = points[0];
    const changeAbs = last.close - first.close;
    const changePct = (first.close ? changeAbs / first.close : 0) * 100;
    const positive = changeAbs >= 0;

    const markers = tradesForTicker
      .map((t) => {
        const tx = new Date(t.timestamp || t.t).getTime();
        if (!tx || tx < xmin || tx > xmax) return null;
        const side = (t.action || '').toUpperCase().startsWith('BUY')
          ? 'buy' : (t.action || '').toUpperCase().startsWith('SELL') ? 'sell' : 'hold';
        return {
          id: t.id,
          x: xScale(tx),
          y: yScale(Number(t.price)),
          price: Number(t.price),
          side,
          action: t.action,
          ts: t.timestamp,
        };
      })
      .filter(Boolean);

    return {
      W, H, M, ymin, ymax, xmin, xmax,
      points, path, areaPath,
      last: last.close, first: first.close,
      changeAbs, changePct, positive,
      markers,
    };
  }, [candles, tradesForTicker, width]);

  // Hover handler — find closest point.
  const onMouseMove = (e) => {
    if (!chart) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width * chart.W;
    if (x < chart.M.l || x > chart.W - chart.M.r) { setHover(null); return; }
    // Find nearest point.
    let best = null; let bestDist = Infinity;
    for (const p of chart.points) {
      const px = chart.M.l + (chart.W - chart.M.l - chart.M.r) *
                  ((p.t - chart.xmin) / (chart.xmax - chart.xmin || 1));
      const d = Math.abs(px - x);
      if (d < bestDist) { bestDist = d; best = { ...p, x: px }; }
    }
    if (best) {
      const py = chart.H - chart.M.b - (chart.H - chart.M.t - chart.M.b)
                  * ((best.close - chart.ymin) / (chart.ymax - chart.ymin || 1));
      setHover({ ...best, py });
    }
  };

  const buyMarkers = chart?.markers?.filter((m) => m.side === 'buy') || [];
  const sellMarkers = chart?.markers?.filter((m) => m.side === 'sell') || [];

  return (
    <div className="panel panel--markets" ref={wrapRef}>
      <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div style={{ minWidth: 200 }}>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>Live tape · with the bot's trades</div>
          <div className="row" style={{ gap: 12, marginTop: 4, alignItems: 'baseline' }}>
            <h3 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              {ticker || '—'}
            </h3>
            {chart && (
              <>
                <span style={{ fontSize: 17, fontWeight: 600, fontFeatureSettings: '"tnum"' }}>
                  ${formatPrice(chart.last)}
                </span>
                <span className={chart.positive ? 'pill on' : 'pill danger'}>
                  {chart.positive ? '▲' : '▼'} {chart.positive ? '+' : ''}{chart.changePct.toFixed(2)}%
                </span>
              </>
            )}
          </div>
        </div>

        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <select
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            style={{ width: 110, padding: '6px 28px 6px 10px', fontWeight: 600 }}
          >
            {universe.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
            {!universe.includes(ticker) && ticker && (
              <option value={ticker}>{ticker}</option>
            )}
          </select>
          <div className="row" style={{ gap: 2, background: 'var(--panel-2)', borderRadius: 8, padding: 2, border: '1px solid var(--border)' }}>
            {PERIODS.map((p) => (
              <button
                key={p.key}
                onClick={() => setPeriod(p)}
                className="btn small ghost"
                style={{
                  padding: '4px 9px', fontSize: 11, fontWeight: 600,
                  background: period.key === p.key ? 'var(--accent)' : 'transparent',
                  color: period.key === p.key ? '#02160e' : 'var(--text-soft)',
                  border: 'none', boxShadow: 'none', borderRadius: 6,
                }}
              >
                {p.key}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="accent-bear" style={{ fontSize: 12, marginBottom: 8 }}>
          {error}
        </div>
      )}

      {loading && !chart && (
        <div className="empty"><div className="title">Loading candles…</div></div>
      )}

      {!loading && !chart && (
        <div className="empty">
          <div className="title">No candle data for {ticker}</div>
          <div className="hint">Try a different period or ticker.</div>
        </div>
      )}

      {chart && (
        <div style={{ width: '100%', position: 'relative' }}>
          <svg
            viewBox={`0 0 ${chart.W} ${chart.H}`}
            preserveAspectRatio="none"
            style={{ width: '100%', height: 260, display: 'block', cursor: 'crosshair' }}
            onMouseMove={onMouseMove}
            onMouseLeave={() => setHover(null)}
          >
            <defs>
              <linearGradient id="livetape-area" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={chart.positive ? 'var(--accent)' : 'var(--danger)'}
                      stopOpacity="0.30" />
                <stop offset="100%" stopColor={chart.positive ? 'var(--accent)' : 'var(--danger)'}
                      stopOpacity="0.02" />
              </linearGradient>
            </defs>

            {/* horizontal gridlines */}
            {[0, 0.25, 0.5, 0.75, 1].map((r) => {
              const y = chart.M.t + (chart.H - chart.M.t - chart.M.b) * r;
              return (
                <line key={r} x1={chart.M.l} x2={chart.W - chart.M.r}
                      y1={y} y2={y}
                      stroke="var(--border)" strokeWidth="0.5"
                      strokeDasharray={r === 0 || r === 1 ? '0' : '2,3'} />
              );
            })}

            {/* y-axis labels (4 levels) */}
            {[0, 0.33, 0.66, 1].map((r) => {
              const v = chart.ymax - (chart.ymax - chart.ymin) * r;
              const y = chart.M.t + (chart.H - chart.M.t - chart.M.b) * r;
              return (
                <text key={r} x={chart.M.l - 8} y={y + 4}
                      textAnchor="end" fontSize="10.5"
                      fill="var(--muted)" fontFamily="ui-monospace, SFMono-Regular, monospace">
                  ${formatPrice(v)}
                </text>
              );
            })}

            {/* x-axis labels (first / mid / last) */}
            {[0, 0.5, 1].map((r) => {
              const x = chart.M.l + (chart.W - chart.M.l - chart.M.r) * r;
              const t = chart.xmin + (chart.xmax - chart.xmin) * r;
              const d = new Date(t);
              const label = period.key === '1D'
                ? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
                : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
              return (
                <text key={r} x={x} y={chart.H - 8}
                      textAnchor="middle" fontSize="10.5"
                      fill="var(--muted)">
                  {label}
                </text>
              );
            })}

            {/* area fill */}
            <path d={chart.areaPath} fill="url(#livetape-area)" />

            {/* price line */}
            <path d={chart.path} fill="none"
                  stroke={chart.positive ? 'var(--accent-2)' : 'var(--danger-2)'}
                  strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />

            {/* trade markers */}
            {chart.markers.map((m) => {
              const fill = m.side === 'buy'
                ? 'var(--accent)' : m.side === 'sell' ? 'var(--danger)' : 'var(--muted)';
              const arrow = m.side === 'buy'
                ? `M${m.x - 6},${m.y + 14} L${m.x + 6},${m.y + 14} L${m.x},${m.y + 4} Z`
                : `M${m.x - 6},${m.y - 14} L${m.x + 6},${m.y - 14} L${m.x},${m.y - 4} Z`;
              return (
                <g key={m.id}>
                  <line x1={m.x} x2={m.x}
                        y1={chart.M.t} y2={chart.H - chart.M.b}
                        stroke={fill} strokeOpacity="0.16" strokeWidth="1"
                        strokeDasharray="2,2" />
                  <circle cx={m.x} cy={m.y} r="4.5" fill={fill}
                          stroke="var(--panel)" strokeWidth="2" />
                  <path d={arrow} fill={fill} />
                  <title>{`#${m.id} ${m.action} @ $${m.price?.toFixed(2)} · ${m.ts}`}</title>
                </g>
              );
            })}

            {/* hover crosshair */}
            {hover && (
              <g pointerEvents="none">
                <line x1={hover.x} x2={hover.x}
                      y1={chart.M.t} y2={chart.H - chart.M.b}
                      stroke="var(--text-soft)" strokeOpacity="0.35"
                      strokeWidth="1" strokeDasharray="3,3" />
                <circle cx={hover.x} cy={hover.py} r="4"
                        fill={chart.positive ? 'var(--accent-2)' : 'var(--danger-2)'}
                        stroke="var(--panel)" strokeWidth="2" />
              </g>
            )}
          </svg>

          {/* hover tooltip — positioned absolutely above the SVG */}
          {hover && (
            <div style={{
              position: 'absolute',
              left: Math.min(width - 130, Math.max(8, (hover.x / chart.W) * width + 10)),
              top: 10,
              background: 'var(--panel-3)',
              border: '1px solid var(--border-strong)',
              borderRadius: 8,
              padding: '6px 10px',
              fontSize: 11.5,
              fontFamily: 'ui-monospace, SFMono-Regular, monospace',
              color: 'var(--text)',
              pointerEvents: 'none',
              boxShadow: 'var(--shadow-md)',
            }}>
              <div style={{ color: 'var(--muted)', fontSize: 10 }}>
                {new Date(hover.t).toLocaleString(undefined, {
                  month: 'short', day: 'numeric',
                  hour: 'numeric', minute: '2-digit',
                })}
              </div>
              <div style={{ fontWeight: 600, marginTop: 2 }}>
                ${formatPrice(hover.close)}
              </div>
            </div>
          )}
        </div>
      )}

      {chart && (
        <div className="row" style={{
          marginTop: 10, fontSize: 11.5, color: 'var(--muted)',
          gap: 16, flexWrap: 'wrap',
        }}>
          {buyMarkers.length > 0 && (
            <span className="row" style={{ gap: 6 }}>
              <span style={{ width: 9, height: 9, borderRadius: '50%', background: 'var(--accent)' }} />
              {buyMarkers.length} bot buy{buyMarkers.length === 1 ? '' : 's'}
            </span>
          )}
          {sellMarkers.length > 0 && (
            <span className="row" style={{ gap: 6 }}>
              <span style={{ width: 9, height: 9, borderRadius: '50%', background: 'var(--danger)' }} />
              {sellMarkers.length} bot sell{sellMarkers.length === 1 ? '' : 's'}
            </span>
          )}
          {!chart.markers.length && (
            <span>No bot trades on {ticker} in this window — markers will appear when the bot acts.</span>
          )}
          <span style={{ marginLeft: 'auto' }}>
            {chart.points.length} candles · {period.interval}
          </span>
        </div>
      )}
    </div>
  );
}
