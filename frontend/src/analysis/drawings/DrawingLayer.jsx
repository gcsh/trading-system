/**
 * Drawing overlay — minimal, reliable architecture.
 *
 * What this commit changes (after several broken iterations):
 *
 *   • Click is back to plain `onClick`, not a hand-rolled pointer
 *     state machine. The previous code used onPointerDown / Up with a
 *     "moved < 4px" tap heuristic which was unreliable on touchpads
 *     and after re-renders.
 *
 *   • Tool stays active until the operator picks a different one
 *     (matches TradingView). Each click while a wired tool is active
 *     finishes the current shape OR collects the next point. To stop
 *     drawing the operator picks "Cursor".
 *
 *   • Pointer-events: `auto` when a wired tool is active, otherwise
 *     `none`. No selection/drag in this v1 — that complexity is what
 *     was breaking everything. Right-click anywhere on a shape still
 *     deletes it via a container-level handler (passes through when
 *     the canvas is in `none` mode).
 *
 *   • Persistence + undo/redo + per-ticker storage stay unchanged
 *     (those live in useDrawings.js and weren't the bug).
 *
 * Selection / drag-to-move / handles will land in a follow-up once
 * the basic draw → paint loop is proven reliable.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DRAWING_TOOLS, DEFAULT_STYLE } from './tools.js';

const WIRED_TOOLS = new Set([
  'trendline', 'horizontal', 'fib', 'rect', 'text',
  'ray', 'extended_line', 'vertical', 'channel', 'pitchfork', 'fib_extension',
]);

// lightweight-charts coordinateToTime returns `Time = number |
// BusinessDay | string`. Normalize to unix seconds so the shape
// storage is always numeric and the drag arithmetic (when we re-add
// it) doesn't blow up on string-typed times.
function timeToUnix(t) {
  if (t == null) return 0;
  if (typeof t === 'number') return t;
  if (typeof t === 'string') {
    const ms = Date.parse(t);
    return Number.isFinite(ms) ? Math.floor(ms / 1000) : 0;
  }
  if (typeof t === 'object' && 'year' in t) {
    return Math.floor(
      Date.UTC(t.year, (t.month || 1) - 1, t.day || 1) / 1000);
  }
  return 0;
}

function shapeToPixels(shape, chart, candleSeries) {
  const ts = chart.timeScale();
  const out = [];
  for (const p of shape.points) {
    const x = ts.timeToCoordinate(p.time);
    const y = candleSeries.priceToCoordinate(p.price);
    if (x == null || y == null) return null;
    out.push({ x, y });
  }
  return out;
}

export default function DrawingLayer({
  chartRefs,
  activeTool,
  shapes,
  addShape,
  removeShape,
  undo,
  redo,
}) {
  const canvasRef = useRef(null);
  const containerListenerRef = useRef(null);
  const [collecting, setCollecting] = useState(null);
  const [hoverPx, setHoverPx] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  const ready = !!(chartRefs && chartRefs.chart && chartRefs.candleSeries
    && chartRefs.container);
  const isWired = WIRED_TOOLS.has(activeTool);

  // ── Container size ──────────────────────────────────────────────
  useEffect(() => {
    if (!ready) return undefined;
    const el = chartRefs.container;
    const update = () => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.round(r.width), h: Math.round(r.height) });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ready, chartRefs]);

  // ── Repaint ─────────────────────────────────────────────────────
  const repaint = useCallback(() => {
    const cv = canvasRef.current;
    if (!cv || !ready) return;
    const ctx = cv.getContext('2d');
    const w = cv.width;
    const h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const view = { width: w, height: h };

    for (const s of shapes) {
      const tool = DRAWING_TOOLS[s.tool];
      if (!tool) continue;
      const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
      if (!px) continue;
      const style = { ...DEFAULT_STYLE, ...(s.style || {}) };
      if (s.tool === 'horizontal' && s.points[0]) {
        style.label = `$${Number(s.points[0].price).toFixed(2)}`;
      }
      tool.draw(ctx, px, style, view);
    }

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
        if (collecting.tool === 'horizontal') {
          const price = chartRefs.candleSeries.coordinateToPrice(hoverPx.y);
          if (price != null) style.label = `$${Number(price).toFixed(2)}`;
        }
        tool.draw(ctx, previewPoints, style, view);
      }
    }
  }, [shapes, collecting, hoverPx, chartRefs, ready]);

  useEffect(() => {
    if (!ready) return undefined;
    const ts = chartRefs.chart.timeScale();
    let raf = null;
    const schedule = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { raf = null; repaint(); });
    };
    ts.subscribeVisibleLogicalRangeChange(schedule);
    schedule();
    return () => {
      ts.unsubscribeVisibleLogicalRangeChange(schedule);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [ready, repaint, chartRefs]);

  useEffect(() => { repaint(); }, [repaint, size]);

  // ── Reset in-progress collection on tool change ─────────────────
  useEffect(() => {
    setCollecting((cur) => (cur && cur.tool !== activeTool ? null : cur));
  }, [activeTool]);

  // ── Click → draw ────────────────────────────────────────────────
  const pixelToData = useCallback((clientX, clientY) => {
    const cv = canvasRef.current;
    if (!cv || !ready) return null;
    const r = cv.getBoundingClientRect();
    const x = clientX - r.left;
    const y = clientY - r.top;
    const rawTime = chartRefs.chart.timeScale().coordinateToTime(x);
    const price = chartRefs.candleSeries.coordinateToPrice(y);
    if (rawTime == null || price == null) return null;
    const time = timeToUnix(rawTime);
    if (!Number.isFinite(time) || time <= 0) return null;
    return { x, y, time, price };
  }, [chartRefs, ready]);

  const onClick = useCallback((evt) => {
    if (!isWired) return;
    const data = pixelToData(evt.clientX, evt.clientY);
    if (!data) return;
    const tool = DRAWING_TOOLS[activeTool];
    if (!tool) return;

    if (activeTool === 'text') {
      // eslint-disable-next-line no-alert
      const txt = globalThis.prompt('Note text:', '');
      if (txt) {
        addShape({
          tool: 'text',
          points: [{ time: data.time, price: data.price }],
          style: { color: DEFAULT_STYLE.color, text: txt, fontSize: 12 },
        });
      }
      // Tool stays active so the operator can drop more notes.
      return;
    }

    const next = collecting && collecting.tool === activeTool
      ? { ...collecting, points: [...collecting.points,
          { time: data.time, price: data.price }] }
      : { tool: activeTool, points: [{ time: data.time, price: data.price }] };

    if (next.points.length >= tool.pointCount) {
      addShape({ tool: next.tool, points: next.points });
      setCollecting(null);
      // Tool stays active. Click again to draw another. Pick "Cursor"
      // in the toolbar to stop.
    } else {
      setCollecting(next);
    }
  }, [activeTool, collecting, isWired, pixelToData, addShape]);

  const onMouseMove = useCallback((evt) => {
    if (!isWired) {
      if (hoverPx) setHoverPx(null);
      return;
    }
    const cv = canvasRef.current;
    if (!cv) return;
    const r = cv.getBoundingClientRect();
    setHoverPx({
      x: evt.clientX - r.left,
      y: evt.clientY - r.top,
    });
  }, [isWired, hoverPx]);

  const onMouseLeave = useCallback(() => setHoverPx(null), []);

  // ── Container-level right-click delete (works regardless of
  //    canvas pointer-events) + Esc / undo / redo keyboard ────────
  useEffect(() => {
    if (!ready) return undefined;
    const el = chartRefs.container;
    const onContext = (e) => {
      const cv = canvasRef.current;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      // Iterate newest-first so the topmost hit wins.
      for (let i = shapes.length - 1; i >= 0; i -= 1) {
        const s = shapes[i];
        const tool = DRAWING_TOOLS[s.tool];
        if (!tool) continue;
        const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
        if (!px) continue;
        if (tool.hitTest(px, x, y, { width: cv.width, height: cv.height },
            s.style)) {
          e.preventDefault();
          removeShape(s.id);
          return;
        }
      }
    };
    el.addEventListener('contextmenu', onContext);
    containerListenerRef.current = onContext;
    return () => {
      el.removeEventListener('contextmenu', onContext);
    };
  }, [ready, shapes, chartRefs, removeShape]);

  useEffect(() => {
    const onKey = (e) => {
      const t = e.target;
      const tag = (t && t.tagName) || '';
      const isText = tag === 'INPUT' || tag === 'TEXTAREA'
        || (t && t.isContentEditable);
      if (isText) return;

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        redo();
        return;
      }
      if (e.key === 'Escape' && collecting) {
        setCollecting(null);
      }
    };
    globalThis.addEventListener('keydown', onKey);
    return () => globalThis.removeEventListener('keydown', onKey);
  }, [collecting, undo, redo]);

  if (!ready) return null;

  return (
    <canvas
      ref={canvasRef}
      width={Math.max(1, size.w)}
      height={Math.max(1, size.h)}
      data-testid="analysis-drawing-canvas"
      onClick={onClick}
      onMouseMove={onMouseMove}
      onMouseLeave={onMouseLeave}
      style={{
        position: 'absolute',
        inset: 0,
        width: '100%',
        height: '100%',
        // Drawing tool active → intercept clicks. Otherwise pass
        // through so chart pan / zoom work normally.
        pointerEvents: isWired ? 'auto' : 'none',
        cursor: isWired ? 'crosshair' : 'default',
        zIndex: 6,
      }}
    />
  );
}
