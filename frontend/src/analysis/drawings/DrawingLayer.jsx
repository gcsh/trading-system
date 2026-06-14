/**
 * Phase D.3.1 — canvas overlay that lets the operator draw on top
 * of TheoryChart. Talks to lightweight-charts via the price/time
 * scale APIs so shapes stay glued to bars across pan/zoom.
 *
 * Lifecycle:
 *   1. Parent passes chartRefs = {chart, candleSeries, container}
 *      (TheoryChart fires `onReady` once init completes).
 *   2. We size the canvas to the container, subscribe to
 *      visibleLogicalRangeChange so a pan/zoom repaints, and
 *      register pointer handlers.
 *   3. Active tool from the toolbar drives a tiny state machine:
 *        idle → collecting → committed (saved to localStorage)
 *
 * Coordinate space:
 *   We store shape points in DATA space ({time, price}). Every paint
 *   converts to pixel space via timeToCoordinate / priceToCoordinate
 *   for the current viewport. That means a shape drawn on the 1Y view
 *   stays anchored to the same bars on the 5Y view (or fullscreen).
 *
 * Out-of-scope here (intentionally — these slot into D.4):
 *   • Drag-to-move
 *   • Undo/redo
 *   • Magnet snap to OHLC
 *   • Resize handles
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DRAWING_TOOLS, DEFAULT_STYLE } from './tools.js';

// Tools that are wired to the canvas engine. Toolbar buttons NOT in
// this set are visible but non-functional placeholders (we expose them
// so the operator can see what's coming — see D.3.3+ roadmap).
const WIRED_TOOLS = new Set(['trendline', 'horizontal', 'fib', 'rect', 'text']);

function shapeToPixels(shape, chart, candleSeries) {
  const ts = chart.timeScale();
  const out = [];
  for (const p of shape.points) {
    const x = ts.timeToCoordinate(p.time);
    const y = candleSeries.priceToCoordinate(p.price);
    if (x == null || y == null) return null;  // off-screen — caller skips
    out.push({ x, y });
  }
  return out;
}

export default function DrawingLayer({
  chartRefs,            // { chart, candleSeries, container }
  activeTool,           // 'cursor' | 'trendline' | …
  shapes,
  addShape,
  removeShape,
  onToolReset,          // called after a shape commits (toolbar resets to cursor)
}) {
  const canvasRef = useRef(null);
  const [collecting, setCollecting] = useState(null);  // { tool, points: [{time,price}] }
  const [hoverPx, setHoverPx] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  // Promote the latest setSize so the ResizeObserver callback (registered
  // once) always sees the current setter — avoids stale closure.
  const setSizeRef = useRef(setSize);
  setSizeRef.current = setSize;

  const ready = !!(chartRefs && chartRefs.chart && chartRefs.candleSeries
    && chartRefs.container);

  // ── Size canvas to container + listen for resize ────────────────
  useEffect(() => {
    if (!ready) return undefined;
    const el = chartRefs.container;
    const updateSize = () => {
      const r = el.getBoundingClientRect();
      setSizeRef.current({ w: r.width, h: r.height });
    };
    updateSize();
    const ro = new ResizeObserver(updateSize);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ready, chartRefs]);

  // ── Repaint helper ──────────────────────────────────────────────
  const repaint = useCallback(() => {
    const cv = canvasRef.current;
    if (!cv || !ready) return;
    const ctx = cv.getContext('2d');
    const w = cv.width;
    const h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const view = { width: w, height: h };
    // Finalized shapes.
    for (const s of shapes) {
      const tool = DRAWING_TOOLS[s.tool];
      if (!tool) continue;
      const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
      if (!px) continue;
      const style = { ...DEFAULT_STYLE, ...(s.style || {}) };
      // Horizontal lines: pre-format the live price as their label so
      // the right-edge tag stays accurate even after zoom changes the
      // scale label position.
      if (s.tool === 'horizontal' && s.points[0]) {
        style.label = `$${Number(s.points[0].price).toFixed(2)}`;
      }
      tool.draw(ctx, px, style, view);
    }
    // In-progress shape preview.
    if (collecting && collecting.tool && hoverPx) {
      const tool = DRAWING_TOOLS[collecting.tool];
      if (tool) {
        const previewPoints = [];
        for (const p of collecting.points) {
          const x = chartRefs.chart.timeScale().timeToCoordinate(p.time);
          const y = chartRefs.candleSeries.priceToCoordinate(p.price);
          if (x != null && y != null) previewPoints.push({ x, y });
        }
        previewPoints.push(hoverPx);
        const style = { ...DEFAULT_STYLE, color: '#ffd166' };
        if (collecting.tool === 'horizontal' && hoverPx) {
          const price = chartRefs.candleSeries.coordinateToPrice(hoverPx.y);
          if (price != null) {
            style.label = `$${Number(price).toFixed(2)}`;
          }
        }
        tool.draw(ctx, previewPoints, style, view);
      }
    }
  }, [shapes, collecting, hoverPx, chartRefs, ready]);

  // ── Schedule repaints on chart pan/zoom + size + shape changes ──
  useEffect(() => {
    if (!ready) return undefined;
    const ts = chartRefs.chart.timeScale();
    let raf = null;
    const schedule = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { raf = null; repaint(); });
    };
    ts.subscribeVisibleLogicalRangeChange(schedule);
    // Initial paint.
    schedule();
    return () => {
      ts.unsubscribeVisibleLogicalRangeChange(schedule);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [ready, repaint, chartRefs]);

  // Repaint when shapes / collecting / size change.
  useEffect(() => { repaint(); }, [repaint, size]);

  // ── Pointer handlers ────────────────────────────────────────────
  const isWired = WIRED_TOOLS.has(activeTool);

  const pixelToData = useCallback((evt) => {
    const cv = canvasRef.current;
    if (!cv) return null;
    const r = cv.getBoundingClientRect();
    const x = evt.clientX - r.left;
    const y = evt.clientY - r.top;
    const time = chartRefs.chart.timeScale().coordinateToTime(x);
    const price = chartRefs.candleSeries.coordinateToPrice(y);
    if (time == null || price == null) return null;
    return { x, y, time, price };
  }, [chartRefs]);

  const onClick = useCallback((evt) => {
    if (!isWired) return;
    const data = pixelToData(evt);
    if (!data) return;
    const tool = DRAWING_TOOLS[activeTool];
    if (!tool) return;

    // Text tool — prompt for the label, then commit immediately.
    if (activeTool === 'text') {
      // eslint-disable-next-line no-alert
      const txt = window.prompt('Note text:', '');
      if (txt) {
        addShape({
          tool: 'text',
          points: [{ time: data.time, price: data.price }],
          style: { color: DEFAULT_STYLE.color, text: txt, fontSize: 12 },
        });
      }
      if (onToolReset) onToolReset();
      return;
    }

    const next = collecting && collecting.tool === activeTool
      ? { ...collecting, points: [...collecting.points, { time: data.time, price: data.price }] }
      : { tool: activeTool, points: [{ time: data.time, price: data.price }] };

    if (next.points.length >= tool.pointCount) {
      addShape({ tool: next.tool, points: next.points });
      setCollecting(null);
      if (onToolReset) onToolReset();
    } else {
      setCollecting(next);
    }
  }, [activeTool, collecting, isWired, pixelToData, addShape, onToolReset]);

  const onMove = useCallback((evt) => {
    if (!isWired) return;
    const cv = canvasRef.current;
    if (!cv) return;
    const r = cv.getBoundingClientRect();
    setHoverPx({ x: evt.clientX - r.left, y: evt.clientY - r.top });
  }, [isWired]);

  const onLeave = useCallback(() => setHoverPx(null), []);

  // Right-click deletes the topmost shape under cursor.
  const onContextMenu = useCallback((evt) => {
    evt.preventDefault();
    const cv = canvasRef.current;
    if (!cv) return;
    const r = cv.getBoundingClientRect();
    const x = evt.clientX - r.left;
    const y = evt.clientY - r.top;
    // Iterate top-down so the newest shape wins.
    for (let i = shapes.length - 1; i >= 0; i -= 1) {
      const s = shapes[i];
      const tool = DRAWING_TOOLS[s.tool];
      if (!tool) continue;
      const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
      if (!px) continue;
      const w = cv.width; const h = cv.height;
      if (tool.hitTest(px, x, y, { width: w, height: h }, s.style)) {
        removeShape(s.id);
        return;
      }
    }
  }, [shapes, removeShape, chartRefs]);

  // Esc cancels an in-progress shape.
  useEffect(() => {
    if (!collecting) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') setCollecting(null);
    };
    globalThis.addEventListener('keydown', onKey);
    return () => globalThis.removeEventListener('keydown', onKey);
  }, [collecting]);

  // ── Render ──────────────────────────────────────────────────────
  if (!ready) return null;
  const cursor = isWired ? 'crosshair' : 'default';
  return (
    <canvas
      ref={canvasRef}
      width={Math.max(1, Math.round(size.w))}
      height={Math.max(1, Math.round(size.h))}
      data-testid="analysis-drawing-canvas"
      onClick={onClick}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
      onContextMenu={onContextMenu}
      style={{
        position: 'absolute',
        inset: 0,
        width: '100%',
        height: '100%',
        // Only intercept pointer events when a wired tool is active so
        // the chart's own crosshair/pan still work in 'cursor' mode.
        pointerEvents: isWired ? 'auto' : 'none',
        cursor,
        zIndex: 6,
      }}
    />
  );
}
