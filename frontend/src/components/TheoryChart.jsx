/**
 * MITS Phase 10 — Theory Studio chart (multi-overlay).
 *
 * Renders an arbitrary set of theory annotations on a single price
 * scaffold. Each theory's lines, zones, markers, and signals get the
 * palette colours allocated by the parent page.
 *
 *   props:
 *     bars            — list of OHLCV bars (the price scaffold).
 *     annotations     — { theory_name: annotation_dict }.
 *     palettes        — { theory_name: {primary, secondary, tertiary} }.
 *     primaryTheory   — name of the theory whose hover-popover wins.
 *
 * Renderer scaffold:
 *
 *   - Horizontal price levels  → native ``createPriceLine`` (axis label).
 *   - Trendlines / fan rays   → lightweight-charts line series.
 *   - Vertical Gann markers   → overlay canvas (no native primitive).
 *   - Zones (Kumo, OB, FVG)   → translucent line-series pair.
 *   - Signals (BUY / SELL …)  → overlay canvas flag markers with hover
 *     popovers. Action → colour:
 *         BUY / BUY_CALL / BUY_VERTICAL_CALL  = green up-flag
 *         SELL / BUY_PUT / BUY_VERTICAL_PUT   = red down-flag
 *         WATCH                                = yellow square
 *         IRON_CONDOR / STRADDLE              = purple "OPT" badge
 *         EXIT_LONG / EXIT_SHORT              = grey arrow
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';

const CHART_HEIGHT = 560;
const CHART_HEIGHT_TABLET = 420;


function toUnix(ts) {
  if (typeof ts === 'number') return ts;
  if (!ts) return 0;
  const d = new Date(ts);
  return Math.floor(d.getTime() / 1000);
}


function actionStyle(action) {
  switch (action) {
    case 'BUY':
    case 'BUY_CALL':
    case 'BUY_VERTICAL_CALL':
      return { color: '#26d07c', shape: 'up_flag', glyph: '▲', short: 'BUY',
               label: action.replaceAll('_', ' ') };
    case 'SELL':
    case 'BUY_PUT':
    case 'BUY_VERTICAL_PUT':
    case 'SELL_CALL':
    case 'SELL_PUT':
      return { color: '#ff5a5f', shape: 'down_flag', glyph: '▼', short: 'SELL',
               label: action.replaceAll('_', ' ') };
    case 'WATCH':
      return { color: '#ffd166', shape: 'square', glyph: '◆', short: 'WATCH',
               label: 'WATCH' };
    case 'IRON_CONDOR':
    case 'STRADDLE':
      return { color: '#b87cff', shape: 'opt_badge', glyph: 'OPT', short: 'OPT',
               label: action };
    case 'EXIT_LONG':
    case 'EXIT_SHORT':
      return { color: '#9aa4b2', shape: 'x_glyph', glyph: '✕', short: 'EXIT',
               label: action.replaceAll('_', ' ') };
    default:
      return { color: '#9aa4b2', shape: 'circle', glyph: '●', short: '?',
               label: action };
  }
}


// MITS Phase 10.1 — hard ceiling on price-lines (axis-label markers).
// Beyond this we drop with a console warning and let the right-rail
// surface a "Some annotations truncated for performance" hint.
const MAX_PRICE_LINES_PER_CHART = 50;


function scheduleIdle(cb) {
  if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
    return window.requestIdleCallback(cb, { timeout: 250 });
  }
  return setTimeout(cb, 0);
}


export default function TheoryChart({
  bars, annotations, palettes, primaryTheory, liveTick,
  hideLegend = false,
  onReady,
}) {
  const containerRef = useRef(null);
  const overlayRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);
  const volumeMaSeriesRef = useRef(null);
  const overlaySeriesRef = useRef([]);
  const priceLinesRef = useRef([]);
  const signalRectsRef = useRef([]);   // {x,y,w,h,signal,theory}
  const lcRef = useRef(null);
  const [hover, setHover] = useState(null);
  const [signalHover, setSignalHover] = useState(null);  // {x,y,signal,theory}
  const [chartHeight, setChartHeight] = useState(CHART_HEIGHT);
  // Set to a new object once the async chart init finishes. Lets the
  // dependent useEffects (candles, overlays, markers) re-run after the
  // chart is actually ready. Without this, if bars arrive BEFORE the
  // chart finishes mounting (lightweight-charts is dynamically
  // imported), the first run of the candles effect sees a null
  // series ref, early-returns, and never re-fires — the chart paints
  // empty even though data exists.
  const [chartReady, setChartReady] = useState(false);

  const allAnnotations = annotations || {};
  const palettesMap = palettes || {};

  // ── Lazy-import lightweight-charts ────────────────────────────────
  useEffect(() => {
    let disposed = false;
    let cleanup = null;
    (async () => {
      const lc = await import('lightweight-charts');
      lcRef.current = lc;
      if (disposed || !containerRef.current) return;
      const isTablet = window.innerWidth < 1100;
      const h = isTablet ? CHART_HEIGHT_TABLET : CHART_HEIGHT;
      setChartHeight(h);
      const chart = lc.createChart(containerRef.current, {
        width: containerRef.current.clientWidth,
        height: h,
        layout: {
          background: { type: 'solid', color: '#0a0e1a' },
          textColor: '#e6edf3',
          fontFamily: 'Inter, system-ui, -apple-system, sans-serif',
        },
        grid: {
          vertLines: { color: '#1e2638' },
          horzLines: { color: '#1e2638' },
        },
        rightPriceScale: {
          borderColor: '#2a3349',
          scaleMargins: { top: 0.08, bottom: 0.28 },
        },
        timeScale: {
          borderColor: '#2a3349',
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 12,
        },
        crosshair: {
          mode: lc.CrosshairMode ? lc.CrosshairMode.Normal : 0,
          vertLine: { color: '#5b6985', width: 1, style: 3 },
          horzLine: { color: '#5b6985', width: 1, style: 3 },
        },
        watermark: { visible: false, color: 'transparent', text: '' },
        localization: {
          priceFormatter: (p) => {
            if (p == null || isNaN(p)) return '';
            return '$' + p.toLocaleString(undefined, {
              minimumFractionDigits: 2, maximumFractionDigits: 2,
            });
          },
        },
      });
      chartRef.current = chart;
      candleSeriesRef.current = chart.addCandlestickSeries({
        upColor: '#26d07c', downColor: '#ff5a5f',
        borderUpColor: '#26d07c', borderDownColor: '#ff5a5f',
        wickUpColor: '#26d07c', wickDownColor: '#ff5a5f',
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      });
      volumeSeriesRef.current = chart.addHistogramSeries({
        color: '#465062', priceFormat: { type: 'volume' },
        priceScaleId: 'vol',
        scaleMargins: { top: 0.78, bottom: 0 },
      });
      chart.priceScale('vol').applyOptions({
        scaleMargins: { top: 0.78, bottom: 0 },
        borderColor: '#2a3349',
      });
      volumeMaSeriesRef.current = chart.addLineSeries({
        color: '#ffd166', lineWidth: 1, priceScaleId: 'vol',
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
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
          x: p.point?.x, y: p.point?.y,
        } : null);
        drawOverlay();
      });

      // MITS Phase 19.x — observe the CHART CONTAINER (not just the
      // browser window). When ChartFullscreenWrapper toggles fullscreen,
      // the container's content-rect changes but the window doesn't —
      // a window-resize listener would miss it entirely, which is the
      // root cause of the "fullscreen chart stays tiny" bug. Switching
      // to ResizeObserver makes the chart fill whatever box the parent
      // gives it (normal layout, fullscreen overlay, side-by-side, etc.).
      const applyResize = (w, h) => {
        if (!chartRef.current) return;
        const wPx = Math.max(60, Math.floor(w));
        const hPx = Math.max(60, Math.floor(h));
        setChartHeight(hPx);
        try {
          chartRef.current.applyOptions({ width: wPx, height: hPx });
        } catch (_) { /* chart torn down */ }
        if (overlayRef.current) {
          overlayRef.current.width = wPx;
          overlayRef.current.height = hPx;
        }
        drawOverlay();
      };

      const ro = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const cr = entry.contentRect;
          if (cr && cr.width > 0 && cr.height > 0) {
            applyResize(cr.width, cr.height);
          } else if (containerRef.current) {
            // contentRect can be (0,0) in some webkit cases — fall
            // back to the container's measured client box.
            applyResize(
              containerRef.current.clientWidth,
              containerRef.current.clientHeight || h,
            );
          }
        }
      });
      ro.observe(containerRef.current);

      chart.timeScale().subscribeVisibleLogicalRangeChange(() => drawOverlay());
      chart.timeScale().subscribeVisibleTimeRangeChange(() => drawOverlay());

      cleanup = () => {
        try { ro.disconnect(); } catch (_) { /* ignore */ }
        chart.remove();
      };

      // Last step of async init — wakes up the candles / overlays
      // effects so they re-run with a now-valid series ref.
      if (!disposed) {
        setChartReady(true);
        // Phase D.3.1 — surface the chart + candle series to a parent
        // drawing layer that needs price/time scale coordinates.
        if (onReady) {
          try {
            onReady({
              chart: chartRef.current,
              candleSeries: candleSeriesRef.current,
              container: containerRef.current,
            });
          } catch (_) { /* swallow — drawing is non-critical */ }
        }
      }
    })();
    return () => {
      disposed = true;
      if (cleanup) cleanup();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Candles + volume scaffold ─────────────────────────────────────
  useEffect(() => {
    if (!chartReady) return;
    if (!candleSeriesRef.current || !volumeSeriesRef.current) return;
    if (!bars || !bars.length) return;
    const candles = bars.map((b) => ({
      time: toUnix(b.t || b.timestamp),
      open: b.open, high: b.high, low: b.low, close: b.close,
    })).filter((c) => c.time > 0).sort((a, b) => a.time - b.time);
    candleSeriesRef.current.setData(candles);

    const vols = bars.map((b) => b.volume || 0);
    const W = 20;
    const ma = vols.map((_, i) => {
      const lo = Math.max(0, i - W + 1);
      const slice = vols.slice(lo, i + 1);
      return slice.reduce((s, x) => s + x, 0) / Math.max(1, slice.length);
    });
    const volumes = bars.map((b, i) => {
      const t = toUnix(b.t || b.timestamp);
      const breakout = (b.volume || 0) >= 1.5 * (ma[i] || 0) && (b.volume || 0) > 0;
      const isUp = b.close >= b.open;
      const base = isUp ? '#26d07c' : '#ff5a5f';
      return {
        time: t,
        value: b.volume || 0,
        color: breakout ? '#ffd166' : (base + '88'),
      };
    }).filter((v) => v.time > 0).sort((a, b) => a.time - b.time);
    volumeSeriesRef.current.setData(volumes);

    const maPts = bars.map((b, i) => ({
      time: toUnix(b.t || b.timestamp), value: ma[i],
    })).filter((p) => p.time > 0).sort((a, b) => a.time - b.time);
    if (volumeMaSeriesRef.current) volumeMaSeriesRef.current.setData(maPts);
    setTimeout(() => drawOverlay(), 0);
  }, [bars, chartReady]);

  // ── MITS Phase 10.1 — live tick → update the rightmost forming candle.
  //
  // The chart already has a complete set of bars (from the multi-theory
  // fetch on a 30s cadence). When a 1s tick lands, we don't re-fetch —
  // we just mutate the last candle's close (and high/low if exceeded)
  // and call candlestickSeries.update(lastBar). lightweight-charts is
  // built around this API; it's the supported real-time path.
  const lastBarRef = useRef(null);
  useEffect(() => {
    if (!bars || !bars.length) {
      lastBarRef.current = null;
      return;
    }
    const last = bars[bars.length - 1];
    lastBarRef.current = {
      time: toUnix(last.t || last.timestamp),
      open: Number(last.open),
      high: Number(last.high),
      low: Number(last.low),
      close: Number(last.close),
    };
  }, [bars]);

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

  // ── Annotation push (re-runs whenever the set of annotations changes) ──
  // MITS Phase 10.1 — debounced + idle-scheduled so rapid window toggles
  // don't pile-up renders on the main thread. Each annotation block is
  // also capped so a runaway theory can't freeze the browser.
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    const chart = chartRef.current;
    const lc = lcRef.current;
    if (!chart || !candleSeriesRef.current || !lc) return undefined;

    let cancelled = false;
    const debounceTimer = setTimeout(() => {
      if (cancelled) return;
      scheduleIdle(() => {
        if (cancelled) return;
        applyAnnotations();
      });
    }, 200);

    function applyAnnotations() {
      // Clear prior overlays.
      for (const s of overlaySeriesRef.current) {
        try { chart.removeSeries(s); } catch (_) { /* gone */ }
      }
      overlaySeriesRef.current = [];
      for (const pl of priceLinesRef.current) {
        try { pl.series.removePriceLine(pl.line); } catch (_) { /* gone */ }
      }
      priceLinesRef.current = [];

      const lcLineStyle = lc.LineStyle || { Solid: 0, Dotted: 1, Dashed: 2 };

      // Combined marker list (for the native candle-series marker layer).
      const allMarkers = [];
      let priceLineCount = 0;
      let truncatedHit = false;

      for (const [theoryName, ann] of Object.entries(allAnnotations)) {
        if (!ann) continue;
        const palette = palettesMap[theoryName] || { primary: '#9aa4b2',
                                                       secondary: '#ffd166',
                                                       tertiary: '#ff5a5f' };

        // 1) Horizontal lines → price-line (axis label). Capped at
        //    MAX_PRICE_LINES_PER_CHART so an over-eager theory can't
        //    freeze the main thread.
        for (const line of (ann.lines || [])) {
          if (line.kind !== 'horizontal') continue;
          if (priceLineCount >= MAX_PRICE_LINES_PER_CHART) {
            truncatedHit = true;
            continue;
          }
          const color = line.color || palette.primary;
          const styleNum = line.style === 'dashed' ? lcLineStyle.Dashed
                            : (line.style === 'dotted' ? lcLineStyle.Dotted : lcLineStyle.Solid);
          try {
            const ref = candleSeriesRef.current.createPriceLine({
              price: line.start.price,
              color,
              lineWidth: Math.max(1, line.width || 1),
              lineStyle: styleNum,
              axisLabelVisible: true,
              title: line.label || '',
            });
            priceLinesRef.current.push({ series: candleSeriesRef.current, line: ref });
            priceLineCount += 1;
          } catch (_) {}
        }

        // 2a) MITS Phase 10.1/10.2 — series + histogram lines: ONE
        //     addLineSeries() / addHistogramSeries() with setData(points)
        //     for the whole curve. Sub-panel hint via meta.panel
        //     ("macd" / "rsi" / "stoch") stacks the panels at the bottom
        //     of the chart so the price scale stays clean. If multiple
        //     sub-panels appear, we stack them vertically:
        //
        //         price candles : top 60%
        //         macd panel    : 60-74%
        //         rsi panel     : 74-87%
        //         stoch panel   : 87-100%
        //
        //     Panel order is fixed (macd, rsi, stoch) so panel placement
        //     stays stable across multi-theory selections.
        //
        // First pass: collect the set of unique panel IDs in this annotation
        // set so we can compute their scale margins consistently.
        const ALL_PANELS = ['macd', 'rsi', 'stoch'];
        const panelSet = new Set();
        for (const [, a] of Object.entries(allAnnotations)) {
          for (const ln of ((a || {}).lines || [])) {
            const p = ln.meta && ln.meta.panel;
            if (p && ALL_PANELS.includes(p)) panelSet.add(p);
          }
        }
        const activePanels = ALL_PANELS.filter((p) => panelSet.has(p));
        // Reserve bottom 40% of chart for sub-panels when any exist; else
        // keep price candles taking the full panel.
        const priceBottomMargin = activePanels.length > 0 ? 0.40 : 0.28;
        // Re-apply price scale margins to lift the candles up off the bottom.
        try {
          chart.priceScale('right').applyOptions({
            scaleMargins: { top: 0.05, bottom: priceBottomMargin },
          });
        } catch (_) {}
        try {
          chart.priceScale('vol').applyOptions({
            scaleMargins: {
              top: 0.60 - (activePanels.length > 0 ? 0.05 : 0),
              bottom: priceBottomMargin,
            },
          });
        } catch (_) {}
        // Compute each panel's scale margin (top/bottom fractions of chart).
        const panelHeight = activePanels.length > 0 ? (0.40 / activePanels.length) : 0;
        const panelMargins = {};
        activePanels.forEach((panel, idx) => {
          // First panel starts at 60% from top; each successive panel is
          // panelHeight further down.
          const topFrac = 0.60 + idx * panelHeight;
          const botFrac = 1.0 - (topFrac + panelHeight);
          panelMargins[panel] = {
            top: topFrac,
            bottom: Math.max(0, botFrac),
          };
        });

        for (const line of (ann.lines || [])) {
          if (line.kind !== 'series' && line.kind !== 'histogram') continue;
          const panelScale = (line.meta && line.meta.panel) || undefined;
          const useHist = (line.kind === 'histogram');
          let s;
          if (useHist) {
            const histOpts = {
              priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
              priceLineVisible: false, lastValueVisible: false,
            };
            if (panelScale) histOpts.priceScaleId = panelScale;
            s = chart.addHistogramSeries(histOpts);
          } else {
            const seriesOpts = {
              color: line.color || palette.primary,
              lineWidth: Math.max(1, line.width || 1),
              lineStyle: line.style === 'dashed' ? 2
                          : (line.style === 'dotted' ? 1 : 0),
              priceLineVisible: false, lastValueVisible: false,
              crosshairMarkerVisible: false,
            };
            if (panelScale) seriesOpts.priceScaleId = panelScale;
            s = chart.addLineSeries(seriesOpts);
          }
          const pts = (line.points || [])
            .map((p) => ({ time: toUnix(p.ts), value: p.price }))
            .filter((p) => p.time > 0 && isFinite(p.value))
            .sort((a, b) => a.time - b.time);
          if (useHist) {
            // Colour each bar green (>0) or red (<0).
            const colored = pts.map((p) => ({
              time: p.time, value: p.value,
              color: p.value >= 0 ? '#26d07c88' : '#ff5a5f88',
            }));
            try { s.setData(colored); } catch (_) {}
          } else {
            try { s.setData(pts); } catch (_) {}
          }
          overlaySeriesRef.current.push(s);
          // Pin sub-panel scale to its computed slot.
          if (panelScale && panelMargins[panelScale]) {
            try {
              chart.priceScale(panelScale).applyOptions({
                scaleMargins: panelMargins[panelScale],
                visible: true,
                borderColor: '#2a3349',
              });
            } catch (_) {}
          }
        }

      // 2) Trendlines → line series (kept for non-series theories).
      for (const line of (ann.lines || [])) {
        if (line.kind !== 'trendline') continue;
        const s = chart.addLineSeries({
          color: line.color || palette.primary,
          lineWidth: Math.max(1, line.width || 1),
          lineStyle: line.style === 'dashed' ? 2
                      : (line.style === 'dotted' ? 1 : 0),
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        const pts = [
          { time: toUnix(line.start.ts), value: line.start.price },
          { time: toUnix(line.end.ts),   value: line.end.price },
        ].filter((p) => p.time > 0).sort((a, b) => a.time - b.time);
        try { s.setData(pts); } catch (_) {}
        overlaySeriesRef.current.push(s);
      }

      // 3) Zones → boundary line pair.
      for (const zone of (ann.zones || [])) {
        const colorBase = (zone.color || palette.primary);
        const opacity = Math.min(1, Math.max(0.05, zone.opacity || 0.15));
        const alpha = Math.round(opacity * 255).toString(16).padStart(2, '0');
        const col = colorBase + alpha;
        const top = chart.addLineSeries({
          color: col, lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        const bot = chart.addLineSeries({
          color: col, lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        try {
          top.setData([
            { time: toUnix(zone.x1), value: zone.y1 },
            { time: toUnix(zone.x2), value: zone.y1 },
          ].filter((p) => p.time > 0).sort((a, b) => a.time - b.time));
          bot.setData([
            { time: toUnix(zone.x1), value: zone.y2 },
            { time: toUnix(zone.x2), value: zone.y2 },
          ].filter((p) => p.time > 0).sort((a, b) => a.time - b.time));
        } catch (_) {}
        overlaySeriesRef.current.push(top);
        overlaySeriesRef.current.push(bot);
      }

      // 4) Markers (native candle-series marker layer).
      for (const m of (ann.markers || [])) {
        const t = toUnix(m.ts);
        if (t <= 0) continue;
        allMarkers.push({
          time: t,
          position: m.shape === 'arrow_down' ? 'aboveBar'
                    : (m.shape === 'arrow_up' ? 'belowBar' : 'inBar'),
          color: m.color || palette.primary,
          shape: m.shape === 'arrow_down' ? 'arrowDown'
                  : (m.shape === 'arrow_up' ? 'arrowUp' : 'circle'),
          text: m.label || '',
        });
      }
      }   // end for (theoryName, ann)

      if (candleSeriesRef.current) {
        allMarkers.sort((a, b) => a.time - b.time);
        // De-duplicate by (time, text, position) — multi-theory overlap.
        const seen = new Set();
        const filtered = [];
        for (const m of allMarkers) {
          const k = `${m.time}|${m.position}|${m.text}`;
          if (seen.has(k)) continue;
          seen.add(k);
          filtered.push(m);
        }
        try { candleSeriesRef.current.setMarkers(filtered); } catch (_) {}
      }
      setTruncated(truncatedHit);
      setTimeout(() => drawOverlay(), 0);
    }   // end applyAnnotations

    return () => {
      cancelled = true;
      clearTimeout(debounceTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(Object.keys(allAnnotations)), allAnnotations, bars]);

  // ──────────────────────────────────────────────────────────────────
  function drawOverlay() {
    const canvas = overlayRef.current;
    const chart = chartRef.current;
    const series = candleSeriesRef.current;
    signalRectsRef.current = [];
    if (!canvas || !chart || !series) {
      if (canvas) {
        const ctx = canvas.getContext('2d');
        if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
      return;
    }
    const w = containerRef.current ? containerRef.current.clientWidth : 0;
    const h = chartHeight;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    const timeScale = chart.timeScale();

    function tsToX(ts) {
      const u = toUnix(ts);
      if (u <= 0) return null;
      const x = timeScale.timeToCoordinate(u);
      return (x == null || Number.isNaN(x)) ? null : x;
    }
    function priceToY(p) {
      const y = series.priceToCoordinate(p);
      return (y == null || Number.isNaN(y)) ? null : y;
    }

    // 1) Diagonal Gann fan rays + Andrews median + 2) verticals.
    for (const [theoryName, ann] of Object.entries(allAnnotations)) {
      if (!ann) continue;
      const palette = palettesMap[theoryName] || { primary: '#9aa4b2' };
      for (const line of (ann.lines || [])) {
        if (line.kind !== 'fan' && line.kind !== 'ray') continue;
        const x1 = tsToX(line.start.ts);
        const x2 = tsToX(line.end.ts);
        const y1 = priceToY(line.start.price);
        const y2 = priceToY(line.end.price);
        if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
        ctx.save();
        ctx.strokeStyle = line.color || palette.primary;
        ctx.lineWidth = Math.max(1, line.width || 1);
        ctx.setLineDash(
          line.style === 'dashed' ? [6, 4]
            : (line.style === 'dotted' ? [2, 3] : [])
        );
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.setLineDash([]);
        if (line.label) {
          ctx.font = '11px Inter, system-ui, sans-serif';
          const text = line.label;
          const tw = ctx.measureText(text).width;
          const lx = Math.min(w - tw - 12, x2 + 6);
          const ly = y2;
          ctx.fillStyle = 'rgba(10,14,26,0.85)';
          ctx.fillRect(lx - 4, ly - 8, tw + 8, 16);
          ctx.fillStyle = line.color || palette.primary;
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(text, lx, ly);
        }
        ctx.restore();
      }
      for (const line of (ann.lines || [])) {
        if (line.kind !== 'vertical') continue;
        const x = tsToX(line.start.ts);
        if (x == null) continue;
        ctx.save();
        ctx.strokeStyle = line.color || palette.secondary;
        ctx.lineWidth = Math.max(1, line.width || 1);
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h - 6);
        ctx.stroke();
        ctx.setLineDash([]);
        const text = line.label || '';
        if (text) {
          ctx.translate(x + 4, 8);
          ctx.rotate(Math.PI / 2);
          ctx.fillStyle = line.color || palette.secondary;
          ctx.font = '10px Inter, system-ui, sans-serif';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(text, 0, 0);
        }
        ctx.restore();
      }
    }

    // 3) Signal flag markers — MITS-P10.3.3 prominent (24px arrow + price label).
    //
    // Each marker is now a much larger arrow (16px tall) anchored on the
    // price level, with a pill-shaped badge underneath/above carrying
    // the action verb and the price ("BUY $743"). Hit-boxes are 28×36 so
    // a hover lands easily.
    const labelSlots = new Map();
    for (const [theoryName, ann] of Object.entries(allAnnotations)) {
      if (!ann || !ann.signals || !ann.signals.length) continue;
      for (const s of ann.signals) {
        const x = tsToX(s.ts);
        const y = priceToY(s.price);
        if (x == null || y == null) continue;
        const style = actionStyle(s.action);
        const bucket = Math.floor(x / 36);
        const slot = labelSlots.get(bucket) || 0;
        labelSlots.set(bucket, slot + 1);
        const isDown = style.shape === 'down_flag';
        // Slot offset shifts marker stack vertically to avoid overlap
        // when multiple theories drop signals at the same x-bucket.
        const dir = isDown ? -1 : 1;
        const yOff = dir * (18 + slot * 30);
        const fx = x;
        const fy = y + yOff;

        // Price-label pill ("BUY $743").
        const priceTxt = `${style.short} $${Number(s.price || 0).toFixed(2)}`;
        ctx.save();
        ctx.font = 'bold 11px Inter, system-ui, sans-serif';
        const pillW = Math.max(60, ctx.measureText(priceTxt).width + 14);
        const pillH = 18;
        const pillX = fx - pillW / 2;
        const pillY = fy + (isDown ? -pillH - 22 : 22);
        // Pill background.
        ctx.fillStyle = 'rgba(13, 17, 31, 0.92)';
        ctx.strokeStyle = style.color;
        ctx.lineWidth = 1.5;
        const r = 9;
        ctx.beginPath();
        ctx.moveTo(pillX + r, pillY);
        ctx.lineTo(pillX + pillW - r, pillY);
        ctx.quadraticCurveTo(pillX + pillW, pillY, pillX + pillW, pillY + r);
        ctx.lineTo(pillX + pillW, pillY + pillH - r);
        ctx.quadraticCurveTo(pillX + pillW, pillY + pillH, pillX + pillW - r, pillY + pillH);
        ctx.lineTo(pillX + r, pillY + pillH);
        ctx.quadraticCurveTo(pillX, pillY + pillH, pillX, pillY + pillH - r);
        ctx.lineTo(pillX, pillY + r);
        ctx.quadraticCurveTo(pillX, pillY, pillX + r, pillY);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = style.color;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(priceTxt, pillX + pillW / 2, pillY + pillH / 2 + 0.5);
        ctx.restore();

        // Marker (24px tall arrow / square / OPT badge).
        ctx.save();
        ctx.fillStyle = style.color;
        ctx.strokeStyle = '#0a0e1a';
        ctx.lineWidth = 2;
        if (style.shape === 'up_flag' || style.shape === 'down_flag') {
          const d = isDown ? -1 : 1;
          // Big arrow head (12px wide × 14px tall).
          ctx.beginPath();
          ctx.moveTo(fx, fy);                          // tip touches price.
          ctx.lineTo(fx + 8, fy - 12 * d);
          ctx.lineTo(fx + 3, fy - 12 * d);
          ctx.lineTo(fx + 3, fy - 22 * d);
          ctx.lineTo(fx - 3, fy - 22 * d);
          ctx.lineTo(fx - 3, fy - 12 * d);
          ctx.lineTo(fx - 8, fy - 12 * d);
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
        } else if (style.shape === 'square') {
          ctx.fillRect(fx - 8, fy - 8, 16, 16);
          ctx.strokeRect(fx - 8, fy - 8, 16, 16);
          ctx.fillStyle = '#0a0e1a';
          ctx.font = 'bold 10px Inter, system-ui';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText('!', fx, fy + 1);
        } else if (style.shape === 'opt_badge') {
          ctx.fillRect(fx - 18, fy - 10, 36, 20);
          ctx.strokeRect(fx - 18, fy - 10, 36, 20);
          ctx.fillStyle = '#fff';
          ctx.font = 'bold 11px Inter, system-ui';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText('OPT', fx, fy + 1);
        } else if (style.shape === 'x_glyph') {
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.moveTo(fx - 7, fy - 7); ctx.lineTo(fx + 7, fy + 7);
          ctx.moveTo(fx + 7, fy - 7); ctx.lineTo(fx - 7, fy + 7);
          ctx.stroke();
        } else {
          ctx.beginPath(); ctx.arc(fx, fy, 6, 0, Math.PI * 2);
          ctx.fill(); ctx.stroke();
        }
        ctx.restore();

        // Hit-box covers both marker + pill so hover is forgiving.
        const hitTop = Math.min(fy - 22, pillY);
        const hitBot = Math.max(fy + 4, pillY + pillH);
        signalRectsRef.current.push({
          x: fx - Math.max(14, pillW / 2),
          y: hitTop,
          w: Math.max(28, pillW),
          h: hitBot - hitTop,
          signal: s, theory: theoryName, color: style.color,
        });
      }
    }
  }

  // ── Mouse → signal popover ─────────────────────────────────────────
  const onCanvasMove = (ev) => {
    const canvas = overlayRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    let best = null;
    for (const r of signalRectsRef.current) {
      if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h) {
        best = r;
        break;
      }
    }
    if (best) {
      setSignalHover({ x: mx, y: my, signal: best.signal,
                          theory: best.theory, color: best.color });
    } else {
      setSignalHover(null);
    }
  };

  const onCanvasLeave = () => setSignalHover(null);

  // ── Legend (multi-theory) ─────────────────────────────────────────
  const legendItems = useMemo(() => {
    const out = [];
    for (const [theoryName, ann] of Object.entries(allAnnotations)) {
      if (!ann) continue;
      const palette = palettesMap[theoryName] || { primary: '#9aa4b2' };
      out.push({
        header: true, color: palette.primary,
        label: theoryName.replaceAll('_', ' '),
      });
      // Theory-specific legend entries.
      if (theoryName === 'pivots') {
        out.push({ color: '#ffffff', label: '· PP (equilibrium)' });
        out.push({ color: '#ffd166', label: '· R1 / S1' });
        out.push({ color: '#ff9f1c', label: '· R2 / S2' });
      } else if (theoryName === 'fibonacci') {
        out.push({ color: '#36c26b', label: '· 38.2%' });
        out.push({ color: '#ffd166', label: '· 50% balanced' });
        out.push({ color: '#ff9f1c', label: '· 61.8% golden' });
      } else if (theoryName === 'gann') {
        out.push({ color: palette.primary, label: '· 1×1 trend heartbeat' });
        out.push({ color: '#d63a3a', label: '· Vertical = time cycle' });
      } else if (theoryName === 'ichimoku') {
        out.push({ color: '#1f6feb', label: '· Tenkan / Kijun' });
        out.push({ color: '#36c26b', label: '· Senkou A / Kumo' });
      } else if (theoryName === 'bollinger') {
        out.push({ color: '#ffd166', label: '· Mid SMA-20' });
        out.push({ color: '#36c26b', label: '· Upper +2σ' });
        out.push({ color: '#ff5a5f', label: '· Lower −2σ' });
      } else if (theoryName === 'donchian') {
        out.push({ color: '#36c26b', label: '· Upper-N' });
        out.push({ color: '#ff5a5f', label: '· Lower-N' });
      } else if (theoryName === 'keltner') {
        out.push({ color: '#ffd166', label: '· EMA mid' });
        out.push({ color: '#36c26b', label: '· +ATR upper' });
        out.push({ color: '#ff5a5f', label: '· −ATR lower' });
      } else if (theoryName === 'ma_ribbon') {
        out.push({ color: palette.primary, label: '· 8 Fib EMAs (5..144)' });
      } else if (theoryName === 'avwap') {
        out.push({ color: palette.primary, label: '· Anchored VWAP lines' });
      } else if (theoryName === 'rsi_divergence') {
        out.push({ color: '#36c26b', label: '· Bullish divergence' });
        out.push({ color: '#ff5a5f', label: '· Bearish divergence' });
      } else if (theoryName === 'macd_signal') {
        out.push({ color: '#26d07c', label: '· Bull cross' });
        out.push({ color: '#ff5a5f', label: '· Bear cross' });
      } else if (theoryName === 'stochastic') {
        out.push({ color: '#1f6feb', label: '· %K %D crosses' });
      } else if (theoryName === 'atr_bands') {
        out.push({ color: '#36c26b', label: '· +ATR' });
        out.push({ color: '#ff5a5f', label: '· −ATR' });
      } else if (theoryName === 'murrey_math') {
        out.push({ color: '#ffd166', label: '· 4/8 magnet' });
        out.push({ color: '#ff5a5f', label: '· 0/8 8/8 ultimate S/R' });
      } else if (theoryName === 'andrews_pitchfork') {
        out.push({ color: '#ffd166', label: '· Median line' });
        out.push({ color: '#36c26b', label: '· Upper parallel' });
        out.push({ color: '#ff5a5f', label: '· Lower parallel' });
      } else if (theoryName === 'square_of_9') {
        out.push({ color: '#ffd166', label: '· Up harmonics' });
        out.push({ color: '#1f6feb', label: '· Down harmonics' });
      } else if (theoryName === 'volume_profile') {
        out.push({ color: '#ffd166', label: '· POC' });
        out.push({ color: '#36c26b', label: '· VAH' });
        out.push({ color: '#ff5a5f', label: '· VAL' });
      } else if (theoryName === 'harmonic_patterns') {
        out.push({ color: palette.primary, label: '· XABCD legs + PRZ' });
      } else if (theoryName === 'elliott_wave') {
        out.push({ color: palette.primary, label: '· 5-wave + ABC count' });
      } else if (theoryName === 'wyckoff_phases') {
        out.push({ color: '#ff5a5f', label: '· AR high' });
        out.push({ color: '#36c26b', label: '· SC low' });
        out.push({ color: palette.primary, label: '· Phase tag' });
      } else if (theoryName === 'smc_order_blocks') {
        out.push({ color: '#36c26b', label: '· Bullish OB' });
        out.push({ color: '#ff5a5f', label: '· Bearish OB' });
      } else if (theoryName === 'fair_value_gaps') {
        out.push({ color: '#36c26b', label: '· Bullish FVG' });
        out.push({ color: '#ff5a5f', label: '· Bearish FVG' });
      } else if (theoryName === 'price_action') {
        out.push({ color: palette.primary, label: '· Pattern boundaries' });
      }
    }
    return out;
  }, [allAnnotations, palettesMap]);

  // Pattern badge from the primary theory.
  const primaryAnn = allAnnotations[primaryTheory];
  const patternBadge = primaryAnn?.pattern_name ? (
    <div style={{
      position: 'absolute',
      top: 10, right: 80,
      padding: '6px 12px',
      borderRadius: 6,
      background: 'rgba(255, 90, 95, 0.18)',
      color: '#ffb3b5',
      border: '1px solid #ff5a5f',
      fontSize: 12,
      fontWeight: 700,
      letterSpacing: 0.3,
      zIndex: 5,
      pointerEvents: 'none',
      textTransform: 'uppercase',
    }}>
      {primaryAnn.pattern_name.replaceAll('_', ' ')}
      {primaryAnn.confidence != null
        && ` (${Math.round(primaryAnn.confidence * 100)}%)`}
    </div>
  ) : null;

  const legendHeight = Math.min(220, Math.max(60, legendItems.length * 16 + 12));
  const scrollable = legendItems.length > 12;

  // MITS-P10.3.3 — when an operator hovers a signal, briefly fade the
  // underlying chart/overlay so the BUY/SELL pill stands out. Each
  // child uses a CSS transition so the fade is buttery (~120ms).
  const fadeForHover = signalHover ? 0.32 : 1.0;
  // MITS Phase 19.x — when the parent (e.g. ChartFullscreenWrapper) gives
  // us a flex / 100%-height slot, fill it so ResizeObserver picks up the
  // larger box. When the parent gives us no height at all (legacy callers),
  // fall back to CHART_HEIGHT so we still render. The outer wrapper uses
  // height:100% + minHeight so both cases work.
  return (
    <div style={{
      width: '100%', position: 'relative',
      height: '100%', minHeight: CHART_HEIGHT_TABLET,
      display: 'flex', flexDirection: 'column',
    }}>
      <div ref={containerRef} style={{
        width: '100%',
        height: '100%',
        minHeight: CHART_HEIGHT_TABLET,
        flex: 1,
        border: '1px solid #2a3349', borderRadius: 8,
        background: '#0a0e1a',
        position: 'relative', overflow: 'hidden',
        opacity: fadeForHover,
        transition: 'opacity 130ms ease',
      }} />
      <canvas
        ref={overlayRef}
        style={{
          position: 'absolute', top: 0, left: 0,
          width: '100%', height: chartHeight,
          pointerEvents: 'auto',
        }}
        onMouseMove={onCanvasMove}
        onMouseLeave={onCanvasLeave}
      />
      {patternBadge}

      {/* MITS Phase 10.1 — truncation warning when a theory emitted
          more than MAX_PRICE_LINES_PER_CHART horizontal levels. */}
      {truncated && (
        <div style={{
          position: 'absolute',
          top: 10, left: 10,
          padding: '4px 8px',
          borderRadius: 6,
          background: 'rgba(255, 159, 28, 0.15)',
          color: '#ffd29a',
          border: '1px solid #ff9f1c',
          fontSize: 11,
          fontWeight: 600,
          zIndex: 5,
          pointerEvents: 'none',
        }}>
          Some annotations truncated for performance ({MAX_PRICE_LINES_PER_CHART}+ levels)
        </div>
      )}

      {/* Multi-theory legend bottom-left, scrollable when ≥3 theories.
          StockAnalysis hides this since the right-rail accordion is the
          source of truth there; Theory Studio keeps it. */}
      {!hideLegend && legendItems.length > 0 && (
        <div style={{
          position: 'absolute', bottom: 10, left: 14,
          background: 'rgba(13,17,31,0.88)',
          border: '1px solid #2a3349',
          borderRadius: 6,
          padding: '6px 10px',
          fontSize: 11,
          color: '#c8d2e8',
          display: 'grid',
          gap: 3,
          zIndex: 5,
          pointerEvents: 'auto',
          maxHeight: scrollable ? 220 : 'auto',
          overflowY: scrollable ? 'auto' : 'visible',
          minWidth: 160,
        }}>
          {legendItems.map((it, i) => (
            <div key={i} style={{
              display: 'flex', alignItems: 'center', gap: 6,
              marginTop: it.header ? 4 : 0,
              fontWeight: it.header ? 700 : 400,
              textTransform: it.header ? 'capitalize' : 'none',
              borderTop: it.header && i > 0 ? '1px solid #2a3349' : 'none',
              paddingTop: it.header && i > 0 ? 4 : 0,
            }}>
              <span style={{
                width: 12, height: it.header ? 6 : 2,
                background: it.color, display: 'inline-block',
                borderRadius: 1,
              }} />
              <span>{it.label}</span>
            </div>
          ))}
        </div>
      )}

      {/* Crosshair hover tooltip. */}
      {hover && hover.candle && !signalHover && (
        <div style={{
          position: 'absolute', top: 8, left: 12,
          background: 'rgba(13,17,31,0.92)',
          border: '1px solid #2a3349',
          borderRadius: 6, padding: '6px 10px',
          fontSize: 11, color: '#e6edf3',
          display: 'grid', gap: 2,
          zIndex: 5,
          minWidth: 220,
          pointerEvents: 'none',
        }}>
          <div style={{ color: '#8593b0', fontSize: 10 }}>
            {new Date(hover.time * 1000).toLocaleDateString(undefined, {
              weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
            })}
          </div>
          <div>
            O <b>${hover.candle.open?.toFixed(2)}</b>
            {'  '}H <b>${hover.candle.high?.toFixed(2)}</b>
            {'  '}L <b>${hover.candle.low?.toFixed(2)}</b>
            {'  '}C <b>${hover.candle.close?.toFixed(2)}</b>
          </div>
          <div style={{ color: '#8593b0' }}>
            Vol <b style={{ color: '#e6edf3' }}>
              {Math.round(hover.volume).toLocaleString()}
            </b>
          </div>
        </div>
      )}

      {/* Signal hover popover. */}
      {signalHover && (
        <div style={{
          position: 'absolute',
          left: Math.min(signalHover.x + 18, 9999),
          top: Math.max(20, signalHover.y - 80),
          background: 'rgba(13,17,31,0.96)',
          border: `1.5px solid ${signalHover.color}`,
          borderRadius: 6, padding: '8px 10px',
          fontSize: 11.5, color: '#e6edf3',
          display: 'grid', gap: 3,
          zIndex: 10,
          maxWidth: 320,
          pointerEvents: 'none',
          boxShadow: '0 12px 36px rgba(0,0,0,0.45)',
        }}>
          <div style={{ color: signalHover.color, fontWeight: 700,
                        textTransform: 'uppercase', letterSpacing: 0.4 }}>
            {signalHover.signal.action} — {signalHover.theory.replaceAll('_', ' ')}
          </div>
          <div>Price: <b>${signalHover.signal.price?.toFixed(2)}</b>
            {signalHover.signal.target_price != null && (
              <span style={{ color: '#8593b0' }}>
                {' '}→ target <b style={{ color: '#26d07c' }}>
                  ${signalHover.signal.target_price.toFixed(2)}
                </b>
              </span>
            )}
            {signalHover.signal.stop_loss != null && (
              <span style={{ color: '#8593b0' }}>
                {' · '}stop <b style={{ color: '#ff5a5f' }}>
                  ${signalHover.signal.stop_loss.toFixed(2)}
                </b>
              </span>
            )}
          </div>
          <div style={{ color: '#c8d2e8', lineHeight: 1.4 }}>
            {signalHover.signal.reasoning}
          </div>
          {signalHover.signal.confidence != null && (
            <div style={{ fontSize: 10, color: '#8593b0' }}>
              Confidence: {Math.round(signalHover.signal.confidence * 100)}%
              {signalHover.signal.instrument && signalHover.signal.instrument !== 'stock' && (
                <span> · {signalHover.signal.instrument}</span>
              )}
              {signalHover.signal.dte_target && <span> · {signalHover.signal.dte_target}D</span>}
              {signalHover.signal.strike && <span> · ${signalHover.signal.strike}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
