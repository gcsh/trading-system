/**
 * Phase D.3.1 + D.4 — canvas overlay that lets the operator draw,
 * select, drag, edit, and delete shapes on top of TheoryChart.
 *
 * Talks to lightweight-charts via the price/time scale APIs so
 * shapes stay glued to bars across pan / zoom / timeframe / fullscreen
 * swaps. Coordinates are stored in DATA space ({time, price}) and
 * converted to pixels on every render.
 *
 * Selection model:
 *   • Cursor tool + click on a shape → selects it.
 *   • Selected shape paints handle dots (one per point) + a thin
 *     glow halo so the operator can see what's selected.
 *   • Drag a handle → moves just that point (e.g. one end of a
 *     trendline, one corner of a rect).
 *   • Drag the body of a selected shape → moves all points together.
 *   • A floating action chip near the selection offers Color · Delete ·
 *     Duplicate · Deselect.
 *
 * Keyboard:
 *   • Esc       — cancels in-progress drawing OR deselects.
 *   • Backspace · Delete — removes the selected shape.
 *   • ⌘Z / Ctrl-Z       — undo.
 *   • ⌘⇧Z / Ctrl-Y      — redo.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DRAWING_TOOLS, DEFAULT_STYLE } from './tools.js';

const WIRED_TOOLS = new Set(['trendline', 'horizontal', 'fib', 'rect', 'text']);
const DRAG_THRESHOLD_PX = 4;
const HANDLE_RADIUS = 5;

const COLOR_SWATCHES = [
  '#5fc9ce', '#26d07c', '#ffd166', '#e89a4c', '#e8606e',
  '#a073d4', '#5b9bd5', '#e6edf3',
];

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

function dist(x1, y1, x2, y2) {
  return Math.hypot(x2 - x1, y2 - y1);
}

// Find which handle (point index) is under (x, y), if any.
function hitHandle(shape, pixelPts, x, y) {
  for (let i = 0; i < pixelPts.length; i += 1) {
    if (dist(pixelPts[i].x, pixelPts[i].y, x, y) <= HANDLE_RADIUS + 4) {
      return i;
    }
  }
  return -1;
}

// Find the topmost shape hit by (x,y), iterating newest-first.
function findHitShape(shapes, x, y, chart, candleSeries, viewW, viewH) {
  for (let i = shapes.length - 1; i >= 0; i -= 1) {
    const s = shapes[i];
    const tool = DRAWING_TOOLS[s.tool];
    if (!tool) continue;
    const px = shapeToPixels(s, chart, candleSeries);
    if (!px) continue;
    if (tool.hitTest(px, x, y, { width: viewW, height: viewH }, s.style)) {
      return s;
    }
  }
  return null;
}

export default function DrawingLayer({
  chartRefs,
  activeTool,
  shapes,
  selectedId,
  setSelectedId,
  addShape,
  removeShape,
  updateShape,
  duplicateShape,
  undo,
  redo,
  onToolReset,
}) {
  const canvasRef = useRef(null);
  const [collecting, setCollecting] = useState(null);
  const [hoverPx, setHoverPx] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  // Drag state: { kind: 'point'|'body', shapeId, pointIdx?, originalShape, startTime, startPrice }
  const dragRef = useRef(null);
  const pointerStartRef = useRef(null);
  const setSizeRef = useRef(setSize);
  setSizeRef.current = setSize;

  const ready = !!(chartRefs && chartRefs.chart && chartRefs.candleSeries
    && chartRefs.container);
  const isWired = WIRED_TOOLS.has(activeTool);

  // ── Container size tracking ─────────────────────────────────────
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
      const isSelected = s.id === selectedId;
      if (isSelected) {
        // Thin halo behind the selected shape.
        const haloStyle = { ...style,
          color: style.color, width: (style.width || 1) + 3 };
        ctx.save();
        ctx.globalAlpha = 0.18;
        tool.draw(ctx, px, haloStyle, view);
        ctx.restore();
      }
      tool.draw(ctx, px, style, view);

      if (isSelected) {
        // Handle dots at each point.
        for (const p of px) {
          ctx.save();
          ctx.beginPath();
          ctx.arc(p.x, p.y, HANDLE_RADIUS, 0, Math.PI * 2);
          ctx.fillStyle = '#0d111f';
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = style.color || '#5fc9ce';
          ctx.stroke();
          ctx.restore();
        }
      }
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
          if (price != null) style.label = `$${Number(price).toFixed(2)}`;
        }
        tool.draw(ctx, previewPoints, style, view);
      }
    }
  }, [shapes, selectedId, collecting, hoverPx, chartRefs, ready]);

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

  // ── Helpers ─────────────────────────────────────────────────────
  const pixelToData = useCallback((evtClientX, evtClientY) => {
    const cv = canvasRef.current;
    if (!cv) return null;
    const r = cv.getBoundingClientRect();
    const x = evtClientX - r.left;
    const y = evtClientY - r.top;
    const time = chartRefs.chart.timeScale().coordinateToTime(x);
    const price = chartRefs.candleSeries.coordinateToPrice(y);
    if (time == null || price == null) return null;
    return { x, y, time: Number(time), price };
  }, [chartRefs]);

  const clientToCanvas = useCallback((cx, cy) => {
    const cv = canvasRef.current;
    if (!cv) return null;
    const r = cv.getBoundingClientRect();
    return { x: cx - r.left, y: cy - r.top };
  }, []);

  // ── Pointer state machine ───────────────────────────────────────
  const onPointerDown = useCallback((evt) => {
    const cv = canvasRef.current;
    if (!cv || !ready) return;
    const canvasPt = clientToCanvas(evt.clientX, evt.clientY);
    pointerStartRef.current = { ...canvasPt, when: Date.now() };

    // 1) If a wired tool is active → defer to the existing draw flow
    //    (committed on pointerUp at near-start position).
    if (isWired) return;

    // 2) Cursor mode — check selected shape's handles first.
    if (selectedId) {
      const selShape = shapes.find((s) => s.id === selectedId);
      if (selShape) {
        const px = shapeToPixels(selShape,
          chartRefs.chart, chartRefs.candleSeries);
        if (px) {
          const hIdx = hitHandle(selShape, px, canvasPt.x, canvasPt.y);
          if (hIdx >= 0) {
            cv.setPointerCapture(evt.pointerId);
            dragRef.current = {
              kind: 'point',
              shapeId: selectedId,
              pointIdx: hIdx,
              originalShape: JSON.parse(JSON.stringify(selShape)),
            };
            return;
          }
        }
      }
    }

    // 3) Cursor mode — body drag if we hit a shape already selected.
    const hit = findHitShape(shapes, canvasPt.x, canvasPt.y,
      chartRefs.chart, chartRefs.candleSeries,
      cv.width, cv.height);
    if (hit && hit.id === selectedId) {
      const data = pixelToData(evt.clientX, evt.clientY);
      if (data) {
        cv.setPointerCapture(evt.pointerId);
        dragRef.current = {
          kind: 'body',
          shapeId: hit.id,
          originalShape: JSON.parse(JSON.stringify(hit)),
          startTime: data.time,
          startPrice: data.price,
        };
      }
    }
  }, [ready, isWired, selectedId, shapes, chartRefs, clientToCanvas, pixelToData]);

  const onPointerMove = useCallback((evt) => {
    const cv = canvasRef.current;
    if (!cv || !ready) return;
    const canvasPt = clientToCanvas(evt.clientX, evt.clientY);
    setHoverPx(canvasPt);

    if (!dragRef.current) return;
    const data = pixelToData(evt.clientX, evt.clientY);
    if (!data) return;
    const d = dragRef.current;
    if (d.kind === 'point') {
      updateShape(d.shapeId, (s) => {
        const next = { ...s, points: s.points.map((p, i) =>
          i === d.pointIdx ? { time: data.time, price: data.price } : p) };
        return next;
      }, { silent: true });
    } else if (d.kind === 'body') {
      const dt = data.time - d.startTime;
      const dp = data.price - d.startPrice;
      updateShape(d.shapeId, (s) => ({
        ...s,
        points: d.originalShape.points.map((p) => ({
          time: p.time + dt,
          price: p.price + dp,
        })),
      }), { silent: true });
    }
  }, [ready, clientToCanvas, pixelToData, updateShape]);

  const onPointerUp = useCallback((evt) => {
    const cv = canvasRef.current;
    if (!cv) return;
    const canvasPt = clientToCanvas(evt.clientX, evt.clientY);
    const start = pointerStartRef.current;
    const moved = start ? dist(start.x, start.y, canvasPt.x, canvasPt.y)
      : Infinity;
    pointerStartRef.current = null;

    // End of drag — commit one history entry for the whole gesture.
    if (dragRef.current) {
      try { cv.releasePointerCapture(evt.pointerId); } catch (_) { /* */ }
      const d = dragRef.current;
      dragRef.current = null;
      // Snapshot the post-drag state into history (silent: false).
      updateShape(d.shapeId, (s) => ({ ...s }));
      return;
    }

    // If movement was tiny, treat as a click / tap.
    if (moved > DRAG_THRESHOLD_PX) return;

    if (isWired) {
      // Defer to the original collect-new-shape flow.
      handleDrawClick(evt);
      return;
    }

    // Cursor mode tap — select / deselect.
    const hit = findHitShape(shapes, canvasPt.x, canvasPt.y,
      chartRefs.chart, chartRefs.candleSeries,
      cv.width, cv.height);
    setSelectedId(hit ? hit.id : null);
  }, [clientToCanvas, isWired, shapes, chartRefs, setSelectedId, updateShape]);

  const handleDrawClick = useCallback((evt) => {
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
      if (onToolReset) onToolReset();
      return;
    }

    const next = collecting && collecting.tool === activeTool
      ? { ...collecting, points: [...collecting.points,
          { time: data.time, price: data.price }] }
      : { tool: activeTool, points: [{ time: data.time, price: data.price }] };

    if (next.points.length >= tool.pointCount) {
      addShape({ tool: next.tool, points: next.points });
      setCollecting(null);
      if (onToolReset) onToolReset();
    } else {
      setCollecting(next);
    }
  }, [activeTool, collecting, isWired, pixelToData, addShape, onToolReset]);

  const onMouseLeave = useCallback(() => setHoverPx(null), []);

  const onContextMenu = useCallback((evt) => {
    evt.preventDefault();
    const cv = canvasRef.current;
    if (!cv) return;
    const canvasPt = clientToCanvas(evt.clientX, evt.clientY);
    const hit = findHitShape(shapes, canvasPt.x, canvasPt.y,
      chartRefs.chart, chartRefs.candleSeries,
      cv.width, cv.height);
    if (hit) removeShape(hit.id);
  }, [shapes, removeShape, chartRefs, clientToCanvas]);

  // ── Keyboard ────────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target;
      const tag = (t && t.tagName) || '';
      const isText = tag === 'INPUT' || tag === 'TEXTAREA'
        || (t && t.isContentEditable);
      if (isText) return;

      // Cmd-Z / Ctrl-Z: undo. Cmd-Shift-Z or Ctrl-Y: redo.
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

      if (e.key === 'Escape') {
        if (collecting) setCollecting(null);
        else if (selectedId) setSelectedId(null);
        return;
      }

      if ((e.key === 'Backspace' || e.key === 'Delete') && selectedId) {
        e.preventDefault();
        removeShape(selectedId);
      }
    };
    globalThis.addEventListener('keydown', onKey);
    return () => globalThis.removeEventListener('keydown', onKey);
  }, [collecting, selectedId, removeShape, setSelectedId, undo, redo]);

  // ── Floating action chip near the selected shape ────────────────
  const selectedShape = useMemo(
    () => (selectedId ? shapes.find((s) => s.id === selectedId) : null),
    [selectedId, shapes],
  );

  const chipPosition = useMemo(() => {
    if (!selectedShape || !ready) return null;
    const px = shapeToPixels(selectedShape,
      chartRefs.chart, chartRefs.candleSeries);
    if (!px || px.length === 0) return null;
    // Anchor to the topmost handle so the chip floats above the shape.
    let topPt = px[0];
    for (const p of px) if (p.y < topPt.y) topPt = p;
    return { left: Math.max(8, topPt.x - 110),
             top: Math.max(8, topPt.y - 42) };
  }, [selectedShape, ready, chartRefs, shapes]);

  const [colorPickerOpen, setColorPickerOpen] = useState(false);
  useEffect(() => setColorPickerOpen(false), [selectedId]);

  const onPickColor = useCallback((color) => {
    if (!selectedId) return;
    updateShape(selectedId, (s) => ({
      ...s,
      style: { ...(s.style || {}), color },
    }));
    setColorPickerOpen(false);
  }, [selectedId, updateShape]);

  // ── Render ──────────────────────────────────────────────────────
  if (!ready) return null;
  const cursor = isWired
    ? 'crosshair'
    : (selectedId ? 'move' : 'default');

  return (
    <>
      <canvas
        ref={canvasRef}
        width={Math.max(1, Math.round(size.w))}
        height={Math.max(1, Math.round(size.h))}
        data-testid="analysis-drawing-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onMouseLeave={onMouseLeave}
        onContextMenu={onContextMenu}
        style={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          // Always intercept pointer events when a wired tool OR a
          // selected shape exists. In pure cursor + no selection mode,
          // pass through so the chart's own crosshair/pan works.
          pointerEvents: (isWired || selectedId || shapes.length > 0)
            ? 'auto' : 'none',
          cursor,
          zIndex: 6,
        }}
      />
      {selectedShape && chipPosition && (
        <div
          data-testid="analysis-shape-chip"
          style={{
            position: 'absolute',
            left: chipPosition.left,
            top: chipPosition.top,
            zIndex: 8,
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            padding: 4,
            background: 'rgba(13, 17, 31, 0.95)',
            border: '1px solid var(--border, #2a3349)',
            borderRadius: 8,
            boxShadow: '0 6px 16px rgba(0, 0, 0, 0.45)',
            fontSize: 11,
          }}
        >
          <button
            type="button"
            data-testid="shape-chip-color"
            onClick={() => setColorPickerOpen((o) => !o)}
            title="Color"
            style={{
              width: 26, height: 26, borderRadius: 6,
              background: 'transparent',
              border: '1px solid var(--border-subtle, #2a3349)',
              display: 'inline-flex', alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
            }}
          >
            <span style={{
              display: 'inline-block',
              width: 14, height: 14, borderRadius: '50%',
              background: (selectedShape.style && selectedShape.style.color)
                || DEFAULT_STYLE.color,
            }} />
          </button>
          {colorPickerOpen && (
            <div
              style={{
                position: 'absolute',
                top: 32, left: 0,
                background: 'rgba(13, 17, 31, 0.96)',
                border: '1px solid var(--border, #2a3349)',
                borderRadius: 8,
                padding: 4,
                display: 'flex',
                gap: 4,
              }}
            >
              {COLOR_SWATCHES.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => onPickColor(c)}
                  title={c}
                  style={{
                    width: 18, height: 18, borderRadius: '50%',
                    background: c, cursor: 'pointer',
                    border: '1px solid rgba(255,255,255,0.1)',
                    padding: 0,
                  }}
                />
              ))}
            </div>
          )}
          <button
            type="button"
            data-testid="shape-chip-duplicate"
            onClick={() => duplicateShape(selectedId)}
            title="Duplicate"
            style={{
              padding: '4px 8px',
              background: 'transparent',
              border: '1px solid var(--border-subtle, #2a3349)',
              borderRadius: 6,
              color: 'var(--text-primary, #e6edf3)',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            ⎘
          </button>
          <button
            type="button"
            data-testid="shape-chip-delete"
            onClick={() => removeShape(selectedId)}
            title="Delete (Backspace)"
            style={{
              padding: '4px 8px',
              background: 'rgba(232, 96, 110, 0.12)',
              border: '1px solid rgba(232, 96, 110, 0.4)',
              borderRadius: 6,
              color: '#e8606e',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            🗑
          </button>
          <button
            type="button"
            data-testid="shape-chip-close"
            onClick={() => setSelectedId(null)}
            title="Deselect (Esc)"
            style={{
              padding: '4px 8px',
              background: 'transparent',
              border: '1px solid var(--border-subtle, #2a3349)',
              borderRadius: 6,
              color: 'var(--muted, #8593b0)',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            ✕
          </button>
        </div>
      )}
    </>
  );
}
