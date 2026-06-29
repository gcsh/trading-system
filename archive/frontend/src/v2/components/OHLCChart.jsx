/* MITS Phase 19 Stream 1 — OHLC + Volume chart for /v2/stock/:ticker.
 *
 * lightweight-charts v4.2 candlestick + histogram series. Theory
 * overlays + entry/target/stop lines are pushed in by the parent via
 * the ``overlays`` prop so this component stays narrow: render real
 * OHLC, render volume, attach price lines, accept a live tick. No
 * data fetching here — the parent owns /analysis/{ticker}?window= and
 * /quote/{ticker}.
 *
 *   props:
 *     bars     — [{t, open, high, low, close, volume}, ...]  (required)
 *     overlays — {
 *                  trendLines: [{x1, x2, y1, y2, label, color, style}],
 *                  markers:    [{t, price, position, color, text, shape}],
 *                  entryZone:  {y1, y2, color, opacity},
 *                  priceLines: [{price, color, lineStyle, title}],
 *                }
 *     liveTick — { price, ts } (optional; updates the last candle)
 *     height   — number (default 500)
 *     ticker   — string (display only)
 *
 * Visual contract (matches operator's TradingView reference):
 *   • Background: var(--bg-primary) #0a0e1a
 *   • Up candle:  var(--accent-green) #00ff88
 *   • Down candle:var(--accent-red)   #ff3355
 *   • Volume up:  same green @ 55% opacity
 *   • Volume dn:  same red @ 55% opacity
 *   • Grid:       #1e293b
 *   • Live tick price line: cyan #00d4ff
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';

const TOKENS = {
  bg:       '#0a0e1a',
  border:   '#1e293b',
  borderHi: '#334155',
  text:     '#cbd5e1',
  textDim:  '#94a3b8',
  green:    '#00ff88',
  red:      '#ff3355',
  cyan:     '#00d4ff',
  yellow:   '#ffd700',
};

function toUnix(ts) {
  if (typeof ts === 'number') return ts;
  if (!ts) return 0;
  const d = new Date(ts);
  return Math.floor(d.getTime() / 1000);
}

function dedupeAndSort(rows, keyFn) {
  // lightweight-charts requires strictly increasing time. Take the
  // last value when the same key repeats (e.g. aggregated daily bars
  // for the same date).
  const map = new Map();
  for (const r of rows) {
    const k = keyFn(r);
    if (!k || !isFinite(k)) continue;
    map.set(k, r);
  }
  return Array.from(map.values()).sort((a, b) => keyFn(a) - keyFn(b));
}

export default function OHLCChart({
  bars,
  overlays,
  liveTick,
  height = 500,
  ticker = '',
  bgOverride,
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);
  const priceLinesRef = useRef([]);
  const overlaySeriesRef = useRef([]);
  const lcRef = useRef(null);
  const lastBarRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState(null);

  // ── lazy import + create chart ─────────────────────────────────────
  useEffect(() => {
    let disposed = false;
    let cleanup = null;
    (async () => {
      try {
        const lc = await import('lightweight-charts');
        lcRef.current = lc;
        if (disposed || !containerRef.current) return;
        const chart = lc.createChart(containerRef.current, {
          width: containerRef.current.clientWidth,
          height,
          layout: {
            background: { type: 'solid', color: bgOverride || TOKENS.bg },
            textColor:  TOKENS.text,
            fontFamily: 'Inter, system-ui, -apple-system, sans-serif',
          },
          grid: {
            vertLines: { color: TOKENS.border },
            horzLines: { color: TOKENS.border },
          },
          rightPriceScale: {
            borderColor:  TOKENS.borderHi,
            scaleMargins: { top: 0.06, bottom: 0.28 },
          },
          timeScale: {
            borderColor:    TOKENS.borderHi,
            timeVisible:    true,
            secondsVisible: false,
            rightOffset:    8,
          },
          crosshair: {
            mode: lc.CrosshairMode ? lc.CrosshairMode.Normal : 0,
            vertLine: { color: TOKENS.borderHi, width: 1, style: 3 },
            horzLine: { color: TOKENS.borderHi, width: 1, style: 3 },
          },
          watermark: { visible: false, color: 'transparent', text: '' },
          localization: {
            priceFormatter: (p) => {
              if (p == null || !isFinite(p)) return '';
              return '$' + p.toLocaleString(undefined, {
                minimumFractionDigits: 2, maximumFractionDigits: 2,
              });
            },
          },
        });
        chartRef.current = chart;
        candleSeriesRef.current = chart.addCandlestickSeries({
          upColor:        TOKENS.green,
          downColor:      TOKENS.red,
          borderUpColor:  TOKENS.green,
          borderDownColor:TOKENS.red,
          wickUpColor:    TOKENS.green,
          wickDownColor:  TOKENS.red,
          priceFormat:    { type: 'price', precision: 2, minMove: 0.01 },
        });
        volumeSeriesRef.current = chart.addHistogramSeries({
          color:       TOKENS.textDim,
          priceFormat: { type: 'volume' },
          priceScaleId:'vol',
          scaleMargins:{ top: 0.78, bottom: 0 },
        });
        chart.priceScale('vol').applyOptions({
          scaleMargins: { top: 0.78, bottom: 0 },
          borderColor:  TOKENS.borderHi,
        });

        chart.subscribeCrosshairMove((p) => {
          if (!p || !p.time || !p.seriesData) {
            setHover(null);
            return;
          }
          const c = p.seriesData.get(candleSeriesRef.current);
          const v = p.seriesData.get(volumeSeriesRef.current);
          setHover(c ? {
            time: p.time, candle: c, volume: v?.value || 0,
          } : null);
        });

        const resize = () => {
          if (containerRef.current && chartRef.current) {
            chartRef.current.applyOptions({
              width:  containerRef.current.clientWidth,
              height,
            });
          }
        };
        window.addEventListener('resize', resize);
        cleanup = () => {
          window.removeEventListener('resize', resize);
          try { chart.remove(); } catch (_) { /* gone */ }
        };
        setReady(true);
      } catch (e) {
        setError(e?.message || 'chart load failed');
      }
    })();
    return () => {
      disposed = true;
      if (cleanup) cleanup();
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height, bgOverride]);

  // ── set candle + volume data ───────────────────────────────────────
  useEffect(() => {
    if (!ready || !candleSeriesRef.current || !volumeSeriesRef.current) return;
    if (!Array.isArray(bars) || !bars.length) {
      try { candleSeriesRef.current.setData([]); } catch (_) {}
      try { volumeSeriesRef.current.setData([]); } catch (_) {}
      lastBarRef.current = null;
      return;
    }
    const candles = dedupeAndSort(
      bars.map((b) => ({
        time:  toUnix(b.t || b.timestamp),
        open:  Number(b.open),
        high:  Number(b.high),
        low:   Number(b.low),
        close: Number(b.close),
      })),
      (r) => r.time,
    );
    const volumes = dedupeAndSort(
      bars.map((b) => {
        const isUp = Number(b.close) >= Number(b.open);
        return {
          time:  toUnix(b.t || b.timestamp),
          value: Number(b.volume || 0),
          color: (isUp ? TOKENS.green : TOKENS.red) + '8c',
        };
      }),
      (r) => r.time,
    );
    try { candleSeriesRef.current.setData(candles); } catch (e) { /* tolerate */ }
    try { volumeSeriesRef.current.setData(volumes); } catch (_) {}
    if (candles.length) {
      lastBarRef.current = { ...candles[candles.length - 1] };
    }
    // Fit content so the operator sees every candle on first paint.
    try { chartRef.current?.timeScale().fitContent(); } catch (_) {}
  }, [bars, ready]);

  // ── live tick → forming candle update ──────────────────────────────
  useEffect(() => {
    if (!liveTick || !candleSeriesRef.current || !lastBarRef.current) return;
    const px = Number(liveTick.price);
    if (!isFinite(px) || px <= 0) return;
    const lb = lastBarRef.current;
    lb.close = px;
    if (px > lb.high) lb.high = px;
    if (px < lb.low || lb.low <= 0) lb.low = px;
    try { candleSeriesRef.current.update(lb); } catch (_) { /* fine */ }
  }, [liveTick]);

  // ── apply overlays (priceLines, trendLines, markers, entryZone) ────
  useEffect(() => {
    const chart = chartRef.current;
    const lc = lcRef.current;
    const candle = candleSeriesRef.current;
    if (!chart || !lc || !candle) return;

    // Clear prior overlays.
    for (const pl of priceLinesRef.current) {
      try { pl.series.removePriceLine(pl.line); } catch (_) {}
    }
    priceLinesRef.current = [];
    for (const s of overlaySeriesRef.current) {
      try { chart.removeSeries(s); } catch (_) {}
    }
    overlaySeriesRef.current = [];

    if (!overlays) return;
    const lcStyle = lc.LineStyle || { Solid: 0, Dotted: 1, Dashed: 2 };

    // 1) horizontal price lines (entry / target / stop)
    for (const ln of (overlays.priceLines || [])) {
      try {
        const ref = candle.createPriceLine({
          price:      Number(ln.price),
          color:      ln.color || TOKENS.cyan,
          lineWidth:  ln.lineWidth || 1,
          lineStyle:  ln.lineStyle === 'dashed' ? lcStyle.Dashed
                       : (ln.lineStyle === 'dotted' ? lcStyle.Dotted : lcStyle.Solid),
          axisLabelVisible: true,
          title:      ln.title || '',
        });
        priceLinesRef.current.push({ series: candle, line: ref });
      } catch (_) {}
    }

    // 2) diagonal trendlines as 2-point line series
    for (const tl of (overlays.trendLines || [])) {
      try {
        const s = chart.addLineSeries({
          color: tl.color || TOKENS.cyan,
          lineWidth: tl.lineWidth || 1,
          lineStyle: tl.style === 'dashed' ? lcStyle.Dashed
                       : (tl.style === 'dotted' ? lcStyle.Dotted : lcStyle.Solid),
          priceLineVisible:     false,
          lastValueVisible:     false,
          crosshairMarkerVisible: false,
        });
        const pts = dedupeAndSort([
          { time: toUnix(tl.x1), value: Number(tl.y1) },
          { time: toUnix(tl.x2), value: Number(tl.y2) },
        ], (r) => r.time);
        if (pts.length >= 2) {
          s.setData(pts);
          overlaySeriesRef.current.push(s);
        }
      } catch (_) {}
    }

    // 3) entry zone — translucent box via two boundary lines.
    if (overlays.entryZone && overlays.entryZone.y1 != null && overlays.entryZone.y2 != null) {
      const z = overlays.entryZone;
      const col = z.color || '#8b5e3c';
      const opacity = z.opacity != null ? z.opacity : 0.18;
      const alpha = Math.round(opacity * 255).toString(16).padStart(2, '0');
      // Pick zone range — full visible (lightweight-charts has no native rect).
      const lastT = lastBarRef.current?.time || Math.floor(Date.now() / 1000);
      const firstT = (Array.isArray(bars) && bars.length)
        ? toUnix(bars[0].t || bars[0].timestamp) : (lastT - 3600);
      try {
        const top = chart.addLineSeries({
          color: col + alpha, lineWidth: 1, lineStyle: lcStyle.Solid,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        const bot = chart.addLineSeries({
          color: col + alpha, lineWidth: 1, lineStyle: lcStyle.Solid,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        const pts1 = dedupeAndSort([
          { time: firstT, value: Number(z.y1) },
          { time: lastT,  value: Number(z.y1) },
        ], (r) => r.time);
        const pts2 = dedupeAndSort([
          { time: firstT, value: Number(z.y2) },
          { time: lastT,  value: Number(z.y2) },
        ], (r) => r.time);
        if (pts1.length >= 2) top.setData(pts1);
        if (pts2.length >= 2) bot.setData(pts2);
        overlaySeriesRef.current.push(top, bot);
      } catch (_) {}
    }

    // 4) text markers on the candle series
    if (Array.isArray(overlays.markers) && overlays.markers.length) {
      try {
        const ms = overlays.markers
          .map((m) => ({
            time:     toUnix(m.t),
            position: m.position || 'aboveBar',
            color:    m.color || '#ffffff',
            shape:    m.shape || 'circle',
            text:     m.text || '',
          }))
          .filter((m) => m.time > 0)
          .sort((a, b) => a.time - b.time);
        candle.setMarkers(ms);
      } catch (_) {}
    } else {
      try { candle.setMarkers([]); } catch (_) {}
    }
  }, [overlays, bars]);

  // ── crosshair tooltip ──────────────────────────────────────────────
  const tooltip = useMemo(() => {
    if (!hover?.candle) return null;
    const c = hover.candle;
    return (
      <div className="v2-chart__tt mono">
        <div className="v2-chart__tt-date">
          {new Date(hover.time * 1000).toLocaleString(undefined, {
            month: 'short', day: 'numeric', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
          })}
        </div>
        <div>
          <span className="dim">O</span> <b>${c.open?.toFixed(2)}</b>{' '}
          <span className="dim">H</span> <b>${c.high?.toFixed(2)}</b>{' '}
          <span className="dim">L</span> <b>${c.low?.toFixed(2)}</b>{' '}
          <span className="dim">C</span> <b>${c.close?.toFixed(2)}</b>
        </div>
        <div>
          <span className="dim">Vol</span>{' '}
          <b>{Math.round(hover.volume || 0).toLocaleString()}</b>
        </div>
      </div>
    );
  }, [hover]);

  return (
    <div className="v2-chart-wrap" style={{ width: '100%', position: 'relative' }}>
      <div
        ref={containerRef}
        className="v2-chart-canvas"
        style={{
          width: '100%', height,
          border: `1px solid ${TOKENS.borderHi}`,
          borderRadius: 8,
          background: bgOverride || TOKENS.bg,
          position: 'relative',
        }}
      />
      {tooltip}
      {error && (
        <div className="v2-chart__err">
          chart unavailable: {error}
        </div>
      )}
      {!ready && !error && (
        <div className="v2-chart__loading" data-testid="ohlc-chart-loading">
          Loading chart…
        </div>
      )}
      {ticker && (
        <div className="v2-chart__watermark">{ticker}</div>
      )}
      <style>{`
        .v2-chart__tt {
          position: absolute; top: 8px; left: 12px;
          background: rgba(10,14,26,0.92);
          border: 1px solid ${TOKENS.borderHi};
          border-radius: 6px; padding: 6px 10px;
          font-size: 11px; color: ${TOKENS.text};
          z-index: 5; pointer-events: none;
          min-width: 240px;
        }
        .v2-chart__tt .dim  { color: ${TOKENS.textDim}; }
        .v2-chart__tt-date  { color: ${TOKENS.textDim}; font-size: 10px; margin-bottom: 3px; }
        .v2-chart__err {
          position: absolute; bottom: 10px; left: 12px;
          background: rgba(255,51,85,0.15);
          border: 1px solid ${TOKENS.red};
          border-radius: 6px; padding: 4px 10px;
          font-size: 11px; color: #ffb3b5; z-index: 5;
        }
        .v2-chart__loading {
          position: absolute; inset: 0;
          display: flex; align-items: center; justify-content: center;
          color: ${TOKENS.textDim}; font-size: 12px;
          pointer-events: none;
        }
        .v2-chart__watermark {
          position: absolute; bottom: 60px; right: 24px;
          font-family: 'JetBrains Mono', monospace;
          font-size: 56px; font-weight: 800;
          color: rgba(148, 163, 184, 0.06);
          letter-spacing: 0.04em;
          pointer-events: none; z-index: 1;
        }
      `}</style>
    </div>
  );
}
