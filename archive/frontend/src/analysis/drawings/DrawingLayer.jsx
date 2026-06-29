/**
 * Drawing overlay — full rewrite.
 *
 * Architecture (one model, no surprises):
 *
 *   1. The canvas is paint-only. `pointerEvents: 'none'` ALWAYS so the
 *      chart receives pan/zoom on every empty-space gesture.
 *
 *   2. All input flows through a single capture-phase pointerdown
 *      listener on the chart container, which fires BEFORE
 *      lightweight-charts' own listeners and stopPropagation()'s when
 *      it wants to claim the gesture.
 *
 *   3. AUTO-RESET: a wired drawing tool finalizes one shape, then we
 *      flip back to `cursor` automatically. No more accidental lines
 *      from each subsequent click — the old "tool stays active" model
 *      was the reason the canvas filled with stray trendlines.
 *
 *   4. Cursor mode is the editing mode. Click on a handle of the
 *      selected shape -> drag that endpoint. Click on the body of any
 *      shape -> select + start a body drag. Click on empty space ->
 *      deselect and the chart pans.
 *
 *   5. Drag preview writes points silently. On pointerup with motion
 *      we silently rollback to the pre-drag points, then write the
 *      final state non-silently so undo records exactly one entry.
 *
 *   6. Right-click on any shape deletes it. Backspace / Delete removes
 *      the selection. Esc cancels in-progress collection or deselects.
 *
 * Props:
 *   chartRefs       {chart, candleSeries, container}
 *   activeTool      slug from DRAWING_TOOLS
 *   setActiveTool   parent's setter (used for auto-reset)
 *   shapes          array of {id, tool, points:[{time,price},...], style}
 *   selectedId / setSelectedId
 *   addShape / removeShape / updateShape / duplicateShape
 *   undo / redo
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DRAWING_TOOLS, DEFAULT_STYLE } from './tools.js';

const WIRED_TOOLS = new Set([
  'trendline', 'horizontal', 'fib', 'rect', 'text',
  'ray', 'extended_line', 'vertical', 'channel', 'pitchfork', 'fib_extension',
]);

const HANDLE_PAINT = 5;   // visible radius
const HANDLE_HIT = 12;    // hit radius (loose, easy to grab)
const DRAG_THRESHOLD = 3; // px of motion before we treat it as a drag
const SNAP_PX = 10;       // D.4 magnet-snap pixel threshold (OHLC)

const SWATCHES = ['#5fc9ce', '#ffd166', '#e8606e', '#a78bfa', '#e6edf3'];

const CHIP_BTN = {
  background: 'transparent',
  border: 'none',
  color: '#e6edf3',
  cursor: 'pointer',
  fontSize: 12,
  fontWeight: 600,
  padding: '4px 8px',
  lineHeight: 1,
  borderRadius: 4,
};

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

// D.4 — magnet-snap to the nearest candle's High / Low / Close.
//
// Given a hover pixel and the OHLC bar series, find the bar with the
// closest x coordinate and check if any of its H/L/C lies within
// SNAP_PX of the cursor in screen space. Returns the snapped data
// point + a label ('H'/'L'/'C') for the UI, or null if no snap.
function snapToOHLC(cursorPx, bars, chart, candleSeries) {
  if (!bars || !bars.length || !cursorPx) return null;
  const ts = chart.timeScale();

  // Find the bar whose x is closest to cursor.x. Bars are dense, so
  // a linear-scan binary-search is fine for typical 1000-bar windows.
  let bestBar = null;
  let bestDx = Infinity;
  for (const b of bars) {
    const bx = ts.timeToCoordinate(b.time);
    if (bx == null) continue;
    const dx = Math.abs(bx - cursorPx.x);
    if (dx < bestDx) {
      bestDx = dx;
      bestBar = { bar: b, x: bx };
    }
    if (bx > cursorPx.x + 40) break;   // bars are sorted; bail early
  }
  if (!bestBar) return null;

  const candidates = [
    { label: 'H', value: bestBar.bar.high },
    { label: 'L', value: bestBar.bar.low },
    { label: 'C', value: bestBar.bar.close },
  ];
  let bestSnap = null;
  let bestDist = SNAP_PX;
  for (const c of candidates) {
    if (c.value == null) continue;
    const y = candleSeries.priceToCoordinate(c.value);
    if (y == null) continue;
    const dist = Math.hypot(bestBar.x - cursorPx.x, y - cursorPx.y);
    if (dist < bestDist) {
      bestDist = dist;
      bestSnap = {
        x: bestBar.x,
        y,
        time: bestBar.bar.time,
        price: c.value,
        label: c.label,
      };
    }
  }
  return bestSnap;
}

export default function DrawingLayer({
  chartRefs,
  activeTool,
  setActiveTool,
  shapes,
  selectedId,
  setSelectedId,
  addShape,
  removeShape,
  updateShape,
  duplicateShape,
  undo,
  redo,
  bars,         // D.4 — OHLC array for magnet-snap
}) {
  const canvasRef = useRef(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [collecting, setCollecting] = useState(null);
  const [hoverPx, setHoverPx] = useState(null);
  const [snapPx, setSnapPx] = useState(null);
  const [chipPos, setChipPos] = useState(null);

  const barsRef = useRef(bars);
  barsRef.current = bars;

  // Refs so the long-lived container listeners don't have to re-attach
  // every render.
  const shapesRef = useRef(shapes);
  shapesRef.current = shapes;
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const activeToolRef = useRef(activeTool);
  activeToolRef.current = activeTool;
  const collectingRef = useRef(collecting);
  collectingRef.current = collecting;
  const chipPosRef = useRef(null);

  const ready = !!(chartRefs && chartRefs.chart && chartRefs.candleSeries
    && chartRefs.container);
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

  // ── Switching tool resets in-progress collection. Switching AWAY
  //    from cursor also clears the selection so handles don't bleed
  //    into the wired-tool UX. ───────────────────────────────────────
  useEffect(() => {
    if (activeTool !== 'cursor' && selectedIdRef.current && setSelectedId) {
      setSelectedId(null);
    }
    setCollecting((cur) => (cur && cur.tool !== activeTool ? null : cur));
  }, [activeTool, setSelectedId]);

  // ── Paint ───────────────────────────────────────────────────────
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
      // D.4 — small lock badge at the topmost point of locked shapes.
      if (s.locked && px[0]) {
        ctx.save();
        ctx.fillStyle = 'rgba(95, 201, 206, 0.85)';
        ctx.font = '10px var(--font-mono, monospace)';
        ctx.textBaseline = 'bottom';
        ctx.textAlign = 'left';
        ctx.fillText('🔒', px[0].x + 6, px[0].y - 2);
        ctx.restore();
      }
      if (selectedId && s.id === selectedId) {
        ctx.save();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#ffd166';
        ctx.fillStyle = '#0d111f';
        for (const p of px) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, HANDLE_PAINT, 0, Math.PI * 2);
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

    // D.4 — if magnet-snap is active, use the snapped pixel for the
    // rubber-band preview so the operator sees where the click will
    // land.
    const cursorPx = snapPx
      ? { x: snapPx.x, y: snapPx.y }
      : hoverPx;

    if (collecting && collecting.tool && cursorPx) {
      const tool = DRAWING_TOOLS[collecting.tool];
      if (tool) {
        const previewPoints = [];
        for (const p of collecting.points) {
          const x = chartRefs.chart.timeScale().timeToCoordinate(p.time);
          const y = chartRefs.candleSeries.priceToCoordinate(p.price);
          if (x != null && y != null) previewPoints.push({ x, y });
        }
        previewPoints.push(cursorPx);
        const style = { ...DEFAULT_STYLE, color: '#ffd166' };
        if (collecting.tool === 'horizontal') {
          const price = chartRefs.candleSeries.coordinateToPrice(cursorPx.y);
          if (price != null) style.label = `$${Number(price).toFixed(2)}`;
        }
        tool.draw(ctx, previewPoints, style, view);
      }
    }

    // D.4 — paint the snap indicator on top: filled cyan dot + tiny
    // H/L/C label so the operator knows what they're snapping to.
    if (snapPx) {
      ctx.save();
      ctx.fillStyle = '#5fc9ce';
      ctx.strokeStyle = '#0d111f';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(snapPx.x, snapPx.y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.font = '10px var(--font-mono, monospace)';
      ctx.fillStyle = '#5fc9ce';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(
        `${snapPx.label} $${Number(snapPx.price).toFixed(2)}`,
        snapPx.x + 8, snapPx.y - 6,
      );
      ctx.restore();
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
  }, [shapes, collecting, hoverPx, snapPx, chartRefs, ready, selectedId]);

  // Repaint on scale change (pan/zoom) and on resize.
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

  // ── One container listener handles every input gesture ──────────
  useEffect(() => {
    if (!ready) return undefined;
    const el = chartRefs.container;

    // Active drag state (point or body). Plain object owned by the
    // closure; cleared on pointerup.
    const drag = { active: false };

    const cleanupDrag = () => {
      drag.active = false;
      globalThis.removeEventListener('pointermove', onMove);
      globalThis.removeEventListener('pointerup', onUp);
    };

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
      if (!updateShape) return;

      const ts = chartRefs.chart.timeScale();
      const candleSeries = chartRefs.candleSeries;

      if (drag.mode === 'point') {
        const newTime = timeToUnix(ts.coordinateToTime(x));
        const newPrice = candleSeries.coordinateToPrice(y);
        if (!Number.isFinite(newTime) || newTime <= 0
            || newPrice == null) return;
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
          // Silent rollback then non-silent final write so undo records
          // exactly one snapshot (the pre-drag state).
          updateShape(drag.id, (s) => ({ ...s, points: origPts }),
            { silent: true });
          setTimeout(() => {
            updateShape(drag.id, (s) => ({ ...s, points: finalPts }));
          }, 0);
        }
      }
      cleanupDrag();
    };

    const onDown = (e) => {
      if (e.button !== 0) return;            // only left-click
      const cv = canvasRef.current;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      const view = { width: cv.width, height: cv.height };
      const tool = activeToolRef.current;

      // ── Wired drawing tool: collect a point ─────────────────────
      if (WIRED_TOOLS.has(tool)) {
        e.stopPropagation();
        e.preventDefault();
        const ts = chartRefs.chart.timeScale();
        // D.4 — if the cursor is within SNAP_PX of a candle's H/L/C,
        // use that exact value instead of the free-pixel readout.
        const snap = snapToOHLC(
          { x, y }, barsRef.current,
          chartRefs.chart, chartRefs.candleSeries,
        );
        const rawTime = snap ? snap.time : ts.coordinateToTime(x);
        const price = snap
          ? snap.price
          : chartRefs.candleSeries.coordinateToPrice(y);
        if (rawTime == null || price == null) return;
        const time = timeToUnix(rawTime);
        if (!Number.isFinite(time) || time <= 0) return;

        const def = DRAWING_TOOLS[tool];
        if (!def) return;

        if (tool === 'text') {
          // eslint-disable-next-line no-alert
          const txt = globalThis.prompt('Note text:', '');
          if (txt) {
            addShape({
              tool: 'text',
              points: [{ time, price }],
              style: { color: DEFAULT_STYLE.color, text: txt, fontSize: 12 },
            });
          }
          // Auto-reset to cursor so the next click doesn't drop more
          // notes. This is the single most important behavior change.
          if (setActiveTool) setActiveTool('cursor');
          setCollecting(null);
          return;
        }

        const cur = collectingRef.current;
        const next = cur && cur.tool === tool
          ? { ...cur, points: [...cur.points, { time, price }] }
          : { tool, points: [{ time, price }] };

        if (next.points.length >= def.pointCount) {
          addShape({ tool: next.tool, points: next.points });
          setCollecting(null);
          // Auto-reset to cursor. No more runaway clicks creating
          // extra lines.
          if (setActiveTool) setActiveTool('cursor');
        } else {
          setCollecting(next);
        }
        return;
      }

      // ── Cursor mode: handle drag, body drag, or deselect ────────
      const currentShapes = shapesRef.current || [];
      const curSel = selectedIdRef.current;

      // (1) Handle of the currently-selected shape — priority hit so
      //     an endpoint overlapping other lines is still grabbable.
      //     D.4 — locked shapes are non-interactive (read-only).
      if (curSel) {
        const s = currentShapes.find((sh) => sh.id === curSel);
        if (s && !s.locked) {
          const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
          if (px) {
            for (let i = 0; i < px.length; i += 1) {
              const ddx = px[i].x - x;
              const ddy = px[i].y - y;
              if (ddx * ddx + ddy * ddy <= HANDLE_HIT * HANDLE_HIT) {
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

      // (2) Any shape body — newest first so topmost wins. D.4 —
      //     locked shapes are skipped so the chart underneath can be
      //     panned even when they cover a wide region.
      for (let i = currentShapes.length - 1; i >= 0; i -= 1) {
        const s = currentShapes[i];
        if (s.locked) continue;
        const def = DRAWING_TOOLS[s.tool];
        if (!def) continue;
        const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
        if (!px) continue;
        if (def.hitTest(px, x, y, view, s.style)) {
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

      // (3) Empty space → deselect, do NOT stopPropagation so the
      //     chart receives the gesture and pans normally.
      if (curSel && setSelectedId) setSelectedId(null);
    };

    // Hover preview while collecting; also drives the rubber-band line
    // before the second click lands.
    const onHoverMove = (e) => {
      const tool = activeToolRef.current;
      if (!WIRED_TOOLS.has(tool)) {
        if (chipPosRef.current === null) return;
        // collecting was reset elsewhere; nothing to do
      }
      const cv = canvasRef.current;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      // Only update hoverPx when a wired tool is active so paint
      // doesn't churn during pure pan/zoom.
      if (WIRED_TOOLS.has(tool)) {
        const px = { x: e.clientX - r.left, y: e.clientY - r.top };
        setHoverPx(px);
        // D.4 — magnet snap indicator + price replacement.
        const snap = snapToOHLC(
          px, barsRef.current,
          chartRefs.chart, chartRefs.candleSeries,
        );
        setSnapPx(snap);
      } else {
        if (hoverPx) setHoverPx(null);
        if (snapPx) setSnapPx(null);
      }
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
        const def = DRAWING_TOOLS[s.tool];
        if (!def) continue;
        const px = shapeToPixels(s, chartRefs.chart, chartRefs.candleSeries);
        if (!px) continue;
        if (def.hitTest(px, x, y, view, s.style)) {
          e.preventDefault();
          if (removeShape) removeShape(s.id);
          return;
        }
      }
    };

    el.addEventListener('pointerdown', onDown, true);
    el.addEventListener('pointermove', onHoverMove);
    el.addEventListener('contextmenu', onContext);
    return () => {
      el.removeEventListener('pointerdown', onDown, true);
      el.removeEventListener('pointermove', onHoverMove);
      el.removeEventListener('contextmenu', onContext);
      globalThis.removeEventListener('pointermove', onMove);
      globalThis.removeEventListener('pointerup', onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, chartRefs, setActiveTool, setSelectedId,
    addShape, updateShape, removeShape]);

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
        if (e.shiftKey) redo && redo(); else undo && undo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        redo && redo();
        return;
      }
      if (e.key === 'Escape') {
        if (collectingRef.current) {
          setCollecting(null);
          if (setActiveTool) setActiveTool('cursor');
        } else if (selectedIdRef.current && setSelectedId) {
          setSelectedId(null);
        }
        return;
      }
      if ((e.key === 'Backspace' || e.key === 'Delete')
          && selectedIdRef.current && removeShape) {
        e.preventDefault();
        removeShape(selectedIdRef.current);
      }
    };
    globalThis.addEventListener('keydown', onKey);
    return () => globalThis.removeEventListener('keydown', onKey);
  }, [undo, redo, removeShape, setSelectedId, setActiveTool]);

  if (!ready) return null;

  return (
    <>
      <canvas
        ref={canvasRef}
        width={Math.max(1, size.w)}
        height={Math.max(1, size.h)}
        data-testid="analysis-drawing-canvas"
        style={{
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          // Paint-only. Every input gesture routes through the
          // container-level capture-phase listener above.
          pointerEvents: 'none',
          zIndex: 6,
        }}
      />
      {selectedShape && chipPos && (
        <div
          data-testid="analysis-drawing-chip"
          style={{
            position: 'absolute',
            left: Math.max(4, Math.min(size.w - 260, chipPos.x + 12)),
            top: Math.max(4, chipPos.y - 38),
            zIndex: 8,
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            background: 'rgba(13, 17, 31, 0.97)',
            border: '1px solid rgba(255, 209, 102, 0.65)',
            borderRadius: 6,
            padding: '3px 5px',
            pointerEvents: 'auto',
            boxShadow: '0 6px 18px rgba(0, 0, 0, 0.55)',
            userSelect: 'none',
          }}
        >
          <button
            type="button"
            title="Delete (Backspace)"
            data-testid="analysis-drawing-delete"
            onClick={() => removeShape && removeShape(selectedShape.id)}
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
            display: 'inline-flex', gap: 3, marginLeft: 2, marginRight: 2,
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
                  width: 14, height: 14,
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
