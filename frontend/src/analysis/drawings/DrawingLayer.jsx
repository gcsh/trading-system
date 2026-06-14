/**
 * Drawing overlay — select / drag / delete restored.
 *
 *   • In `cursor` mode the canvas is `pointerEvents: none` so chart
 *     pan/zoom work normally. A capture-phase pointerdown listener on
 *     the chart container hit-tests shapes BEFORE lightweight-charts
 *     sees the event. A hit -> stopPropagation + start a drag (handle
 *     drag if a handle, body drag otherwise). A miss -> deselect and
 *     let the chart pan.
 *
 *   • In a wired drawing tool (trendline, fib, etc.) canvas is
 *     `pointerEvents: auto` and the existing onClick collector runs.
 *     Tool stays active until the operator picks Cursor.
 *
 *   • Drag preview uses silent `updateShape` writes so undo isn't
 *     polluted. On pointerup we silently rollback to the original
 *     points, then commit the final state non-silently so a single
 *     pre-drag snapshot lands in the past stack.
 *
 *   • Selected shape gets a halo + handle dots painted on the canvas
 *     and a floating chip (delete / duplicate / color / deselect)
 *     rendered as an HTML overlay.
 *
 *   • Right-click on a shape still deletes via the container-level
 *     contextmenu listener. Backspace/Delete removes the selected
 *     shape from the keyboard.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DRAWING_TOOLS, DEFAULT_STYLE } from './tools.js';

const WIRED_TOOLS = new Set([
  'trendline', 'horizontal', 'fib', 'rect', 'text',
  'ray', 'extended_line', 'vertical', 'channel', 'pitchfork', 'fib_extension',
]);

const HANDLE_R = 5;       // painted radius
const HANDLE_HIT = 10;    // hit radius
const DRAG_THRESHOLD = 3; // pixels of motion before we call it a drag

const SWATCHES = ['#5fc9ce', '#ffd166', '#e8606e', '#a78bfa', '#e6edf3'];

const CHIP_BTN = {
  background: 'transparent',
  border: 'none',
  color: '#e6edf3',
  cursor: 'pointer',
  fontSize: 12,
  padding: '2px 5px',
  lineHeight: 1,
  borderRadius: 4,
};

// lightweight-charts coordinateToTime returns `Time = number |
// BusinessDay | string`. Normalize to unix seconds so the shape
// storage is always numeric.
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
  selectedId,
  setSelectedId,
  addShape,
  removeShape,
  updateShape,
  duplicateShape,
  undo,
  redo,
}) {
  const canvasRef = useRef(null);
  const [collecting, setCollecting] = useState(null);
  const [hoverPx, setHoverPx] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [chipPos, setChipPos] = useState(null);

  // Refs so the container-level pointerdown listener never has to
  // re-attach when shapes / selectedId / activeTool tick.
  const shapesRef = useRef(shapes);
  shapesRef.current = shapes;
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const activeToolRef = useRef(activeTool);
  activeToolRef.current = activeTool;
  const chipPosRef = useRef(null);

  const ready = !!(chartRefs && chartRefs.chart && chartRefs.candleSeries
    && chartRefs.container);
  const isWired = WIRED_TOOLS.has(activeTool);
  const selectedShape = selectedId
    ? (shapes || []).find((s) => s.id === selectedId) : null;

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

  // ── Clear selection when leaving cursor mode ────────────────────
  useEffect(() => {
    if (activeTool !== 'cursor' && selectedIdRef.current && setSelectedId) {
      setSelectedId(null);
    }
  }, [activeTool, setSelectedId]);

  // ── Repaint ─────────────────────────────────────────────────────
  const repaint = useCallback(() => {
    const cv = canvasRef.current;
    if (!cv || !ready) return;
    const ctx = cv.getContext('2d');
    const w = cv.width;
    const h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const view = { width: w, height: h };

    let chipAnchor = null;

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
      if (selectedId && s.id === selectedId) {
        ctx.save();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#ffd166';
        ctx.fillStyle = '#0d111f';
        for (const p of px) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, HANDLE_R, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
        ctx.restore();
        for (const p of px) {
          if (!chipAnchor
              || p.y < chipAnchor.y
              || (p.y === chipAnchor.y && p.x > chipAnchor.x)) {
            chipAnchor = p;
          }
        }
      }
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

    if (chipAnchor) {
      const newPos = { x: chipAnchor.x, y: chipAnchor.y };
      const cur = chipPosRef.current;
      if (!cur
          || Math.abs(cur.x - newPos.x) > 1
          || Math.abs(cur.y - newPos.y) > 1) {
        chipPosRef.current = newPos;
        setChipPos(newPos);
      }
    } else if (chipPosRef.current) {
      chipPosRef.current = null;
      setChipPos(null);
    }
  }, [shapes, collecting, hoverPx, chartRefs, ready, selectedId]);

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

  // ── Wired-tool click → collect points ───────────────────────────
  const onClick = useCallback((evt) => {
    if (!isWired) return;
    const cv = canvasRef.current;
    if (!cv) return;
    const r = cv.getBoundingClientRect();
    const x = evt.clientX - r.left;
    const y = evt.clientY - r.top;
    const rawTime = chartRefs.chart.timeScale().coordinateToTime(x);
    const price = chartRefs.candleSeries.coordinateToPrice(y);
    if (rawTime == null || price == null) return;
    const time = timeToUnix(rawTime);
    if (!Number.isFinite(time) || time <= 0) return;

    const tool = DRAWING_TOOLS[activeTool];
    if (!tool) return;

    if (activeTool === 'text') {
      // eslint-disable-next-line no-alert
      const txt = globalThis.prompt('Note text:', '');
      if (txt) {
        addShape({
          tool: 'text',
          points: [{ time, price }],
          style: { color: DEFAULT_STYLE.color, text: txt, fontSize: 12 },
        });
      }
      return;
    }

    const next = collecting && collecting.tool === activeTool
      ? { ...collecting, points: [...collecting.points, { time, price }] }
      : { tool: activeTool, points: [{ time, price }] };

    if (next.points.length >= tool.pointCount) {
      addShape({ tool: next.tool, points: next.points });
      setCollecting(null);
    } else {
      setCollecting(next);
    }
  }, [activeTool, collecting, isWired, chartRefs, addShape]);

  const onMouseMove = useCallback((evt) => {
    if (!isWired) {
      if (hoverPx) setHoverPx(null);
      return;
    }
    const cv = canvasRef.current;
    if (!cv) return;
    const r = cv.getBoundingClientRect();
    setHoverPx({ x: evt.clientX - r.left, y: evt.clientY - r.top });
  }, [isWired, hoverPx]);

  const onMouseLeave = useCallback(() => setHoverPx(null), []);

  // ── Cursor-mode selection + drag + right-click delete ───────────
  useEffect(() => {
    if (!ready) return undefined;
    const el = chartRefs.container;

    const drag = { active: false };

    const onMove = (e) => {
      if (!drag.active) return;
      const cv = canvasRef.current;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      if (!drag.moved) {
        if (Math.abs(x - drag.originX) > DRAG_THRESHOLD
            || Math.abs(y - drag.originY) > DRAG_THRESHOLD) {
          drag.moved = true;
        } else {
          return;
        }
      }

      const ts = chartRefs.chart.timeScale();
      const candleSeries = chartRefs.candleSeries;

      if (drag.mode === 'point') {
        const newTime = timeToUnix(ts.coordinateToTime(x));
        const newPrice = candleSeries.coordinateToPrice(y);
        if (!Number.isFinite(newTime) || newTime <= 0
            || newPrice == null) return;
        if (!updateShape) return;
        updateShape(drag.id, (s) => {
          const points = s.points.map((p, i) =>
            i === drag.pointIdx
              ? { time: newTime, price: newPrice }
              : p);
          return { ...s, points };
        }, { silent: true });
      } else {
        const t0 = timeToUnix(ts.coordinateToTime(drag.originX));
        const t1 = timeToUnix(ts.coordinateToTime(x));
        const p0 = candleSeries.coordinateToPrice(drag.originY);
        const p1 = candleSeries.coordinateToPrice(y);
        if (!Number.isFinite(t0) || !Number.isFinite(t1)
            || p0 == null || p1 == null) return;
        const dt = t1 - t0;
        const dp = p1 - p0;
        if (!updateShape) return;
        updateShape(drag.id, (s) => ({
          ...s,
          points: drag.originalPoints.map((p) => ({
            time: p.time + dt,
            price: p.price + dp,
          })),
        }), { silent: true });
      }
    };

    const onUp = () => {
      if (drag.active && drag.moved && updateShape) {
        const finalShape = shapesRef.current.find((s) => s.id === drag.id);
        if (finalShape) {
          const finalPts = finalShape.points.map((p) => ({ ...p }));
          const origPts = drag.originalPoints;
          // Rollback silently so the next non-silent write records the
          // pre-drag snapshot in the past stack.
          updateShape(drag.id, (s) => ({ ...s, points: origPts }),
            { silent: true });
          setTimeout(() => {
            updateShape(drag.id, (s) => ({ ...s, points: finalPts }));
          }, 0);
        }
      }
      drag.active = false;
      globalThis.removeEventListener('pointermove', onMove);
      globalThis.removeEventListener('pointerup', onUp);
    };

    const onDown = (e) => {
      if (e.button !== 0) return;
      if (activeToolRef.current !== 'cursor') return;
      const cv = canvasRef.current;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      const view = { width: cv.width, height: cv.height };
      const currentShapes = shapesRef.current || [];
      const curSel = selectedIdRef.current;

      // 1) Handles of the currently-selected shape get priority so
      //    you can grab an endpoint that overlaps another shape.
      if (curSel) {
        const s = currentShapes.find((sh) => sh.id === curSel);
        if (s) {
          const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
          if (px) {
            for (let i = 0; i < px.length; i += 1) {
              const ddx = px[i].x - x;
              const ddy = px[i].y - y;
              if (ddx * ddx + ddy * ddy
                  <= HANDLE_HIT * HANDLE_HIT) {
                e.stopPropagation();
                e.preventDefault();
                drag.active = true;
                drag.id = s.id;
                drag.mode = 'point';
                drag.pointIdx = i;
                drag.originX = x;
                drag.originY = y;
                drag.originalPoints = s.points.map((p) => ({ ...p }));
                drag.moved = false;
                globalThis.addEventListener('pointermove', onMove);
                globalThis.addEventListener('pointerup', onUp);
                return;
              }
            }
          }
        }
      }

      // 2) Body hit on ANY shape (newest-first so the topmost wins).
      for (let i = currentShapes.length - 1; i >= 0; i -= 1) {
        const s = currentShapes[i];
        const tool = DRAWING_TOOLS[s.tool];
        if (!tool) continue;
        const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
        if (!px) continue;
        if (tool.hitTest(px, x, y, view, s.style)) {
          e.stopPropagation();
          e.preventDefault();
          if (setSelectedId) setSelectedId(s.id);
          drag.active = true;
          drag.id = s.id;
          drag.mode = 'body';
          drag.originX = x;
          drag.originY = y;
          drag.originalPoints = s.points.map((p) => ({ ...p }));
          drag.moved = false;
          globalThis.addEventListener('pointermove', onMove);
          globalThis.addEventListener('pointerup', onUp);
          return;
        }
      }

      // 3) Empty space → deselect, let the chart handle pan.
      if (curSel && setSelectedId) setSelectedId(null);
    };

    const onContext = (e) => {
      const cv = canvasRef.current;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      const view = { width: cv.width, height: cv.height };
      const currentShapes = shapesRef.current || [];
      for (let i = currentShapes.length - 1; i >= 0; i -= 1) {
        const s = currentShapes[i];
        const tool = DRAWING_TOOLS[s.tool];
        if (!tool) continue;
        const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
        if (!px) continue;
        if (tool.hitTest(px, x, y, view, s.style)) {
          e.preventDefault();
          removeShape(s.id);
          return;
        }
      }
    };

    el.addEventListener('pointerdown', onDown, true);
    el.addEventListener('contextmenu', onContext);
    return () => {
      el.removeEventListener('pointerdown', onDown, true);
      el.removeEventListener('contextmenu', onContext);
      globalThis.removeEventListener('pointermove', onMove);
      globalThis.removeEventListener('pointerup', onUp);
    };
  }, [ready, chartRefs, setSelectedId, updateShape, removeShape]);

  // ── Keyboard ────────────────────────────────────────────────────
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
      if (e.key === 'Escape') {
        if (collecting) setCollecting(null);
        else if (selectedIdRef.current && setSelectedId) setSelectedId(null);
        return;
      }
      if ((e.key === 'Backspace' || e.key === 'Delete')
          && selectedIdRef.current) {
        e.preventDefault();
        removeShape(selectedIdRef.current);
      }
    };
    globalThis.addEventListener('keydown', onKey);
    return () => globalThis.removeEventListener('keydown', onKey);
  }, [collecting, undo, redo, removeShape, setSelectedId]);

  if (!ready) return null;

  return (
    <>
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
          // Wired tool → grab clicks here. Cursor mode → let them
          // through to the chart; selection is captured at container
          // level so chart pan/zoom still fire on empty-space clicks.
          pointerEvents: isWired ? 'auto' : 'none',
          cursor: isWired ? 'crosshair' : 'default',
          zIndex: 6,
        }}
      />
      {selectedShape && chipPos && (
        <div
          data-testid="analysis-drawing-chip"
          style={{
            position: 'absolute',
            left: Math.max(2, Math.min(size.w - 200, chipPos.x + 10)),
            top: Math.max(2, chipPos.y - 32),
            zIndex: 8,
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            background: 'rgba(13, 17, 31, 0.96)',
            border: '1px solid rgba(255, 209, 102, 0.55)',
            borderRadius: 6,
            padding: '3px 5px',
            pointerEvents: 'auto',
            boxShadow: '0 4px 12px rgba(0, 0, 0, 0.4)',
            fontSize: 12,
            userSelect: 'none',
          }}
        >
          <button
            type="button"
            title="Delete (Backspace)"
            data-testid="analysis-drawing-delete"
            onClick={() => removeShape(selectedShape.id)}
            style={{ ...CHIP_BTN, color: '#e8606e' }}
          >Delete</button>
          {duplicateShape && (
            <button
              type="button"
              title="Duplicate"
              data-testid="analysis-drawing-duplicate"
              onClick={() => duplicateShape(selectedShape.id)}
              style={CHIP_BTN}
            >Dup</button>
          )}
          <span style={{
            display: 'inline-flex', gap: 3, marginLeft: 2,
          }}>
            {SWATCHES.map((c) => (
              <button
                key={c}
                type="button"
                title={`Color ${c}`}
                onClick={() => updateShape && updateShape(selectedShape.id,
                  (s) => ({ ...s, style: { ...(s.style || {}), color: c } }))}
                style={{
                  background: c,
                  width: 12, height: 12,
                  padding: 0,
                  border: '1px solid rgba(255,255,255,0.4)',
                  borderRadius: 3,
                  cursor: 'pointer',
                }}
              />
            ))}
          </span>
          <button
            type="button"
            title="Deselect"
            onClick={() => setSelectedId && setSelectedId(null)}
            style={CHIP_BTN}
          >Close</button>
        </div>
      )}
    </>
  );
}
