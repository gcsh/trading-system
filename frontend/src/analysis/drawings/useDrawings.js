/**
 * Phase D.3.1 + D.4 — drawing-state persistence per ticker.
 *
 * Shapes are stored as:
 *   { id, tool, points: [{time, price}, ...], style }
 *
 * `time` is a unix-seconds number, `price` is the bar price. These
 * data-space coordinates survive pan/zoom because the DrawingLayer
 * converts them to pixels on every render.
 *
 * Persistence key: `tb.analysis.drawings.<TICKER>`.
 *
 * D.4: undo/redo stacks (capped at 50 entries each, cleared on
 * ticker change), updateShape mutator for drag-edit, and a
 * setSelectedId helper that lives on the parent so the DrawingLayer
 * can render selection handles + a floating action chip.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

function storageKey(ticker) {
  return `tb.analysis.drawings.${(ticker || '').toUpperCase()}`;
}

function loadFromStorage(ticker) {
  try {
    const raw = globalThis.localStorage.getItem(storageKey(ticker));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Also drop shapes whose points carry NaN/non-finite coordinates —
    // an earlier D.4 build briefly stored time=NaN on daily charts,
    // and those shapes would silently never render again. Sweep them
    // on next load so the canvas doesn't accumulate ghost entries.
    return parsed.filter((s) => {
      if (!s || !s.tool || !Array.isArray(s.points)) return false;
      for (const p of s.points) {
        if (!p) return false;
        if (!Number.isFinite(p.time) || !Number.isFinite(p.price)) {
          return false;
        }
      }
      return true;
    });
  } catch (_) {
    return [];
  }
}

function saveToStorage(ticker, shapes) {
  try {
    globalThis.localStorage.setItem(
      storageKey(ticker), JSON.stringify(shapes),
    );
  } catch (_) { /* quota / private mode — silent */ }
}

function makeId() {
  return `d_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e6).toString(36)}`;
}

const MAX_HISTORY = 50;

export default function useDrawings(ticker) {
  const [shapes, setShapesState] = useState(() => loadFromStorage(ticker));
  const [selectedId, setSelectedId] = useState(null);
  const pastRef = useRef([]);     // undo stack (newest = end)
  const futureRef = useRef([]);   // redo stack
  const shapesRef = useRef(shapes);
  shapesRef.current = shapes;

  // Re-hydrate when the ticker changes; reset history so the operator
  // doesn't accidentally undo through a ticker boundary.
  useEffect(() => {
    setShapesState(loadFromStorage(ticker));
    setSelectedId(null);
    pastRef.current = [];
    futureRef.current = [];
  }, [ticker]);

  // Wrap setter so every state-changing call also writes to storage
  // AND records an undo snapshot. opts.silent=true bypasses history
  // (used by undo/redo themselves and by drag-preview ticks).
  const commit = useCallback((next, opts = {}) => {
    if (!opts.silent) {
      pastRef.current.push(shapesRef.current);
      if (pastRef.current.length > MAX_HISTORY) pastRef.current.shift();
      // New edit invalidates the redo stack.
      futureRef.current = [];
    }
    setShapesState(next);
    if (!opts.skipSave) saveToStorage(ticker, next);
  }, [ticker]);

  const addShape = useCallback((shape) => {
    const next = [...shapesRef.current,
      { ...shape, id: shape.id || makeId() }];
    commit(next);
  }, [commit]);

  const removeShape = useCallback((id) => {
    const next = shapesRef.current.filter((s) => s.id !== id);
    commit(next);
    setSelectedId((cur) => (cur === id ? null : cur));
  }, [commit]);

  const updateShape = useCallback((id, mutator, opts = {}) => {
    const next = shapesRef.current.map((s) =>
      s.id === id ? mutator(s) : s);
    commit(next, opts);
  }, [commit]);

  const duplicateShape = useCallback((id) => {
    const src = shapesRef.current.find((s) => s.id === id);
    if (!src) return;
    // Nudge the duplicate slightly so it's visually distinguishable.
    const NUDGE_BARS = 60 * 60 * 24;   // 1 day in unix seconds
    const NUDGE_PRICE = 0;             // no price offset
    const dup = {
      ...src,
      id: makeId(),
      points: src.points.map((p) => ({
        time: p.time + NUDGE_BARS,
        price: p.price + NUDGE_PRICE,
      })),
    };
    commit([...shapesRef.current, dup]);
    setSelectedId(dup.id);
  }, [commit]);

  const clearShapes = useCallback(() => {
    commit([]);
    setSelectedId(null);
  }, [commit]);

  // D.4 — set/unset the `locked` flag on every shape. Locked shapes
  // still render but can't be selected, dragged, deleted, or
  // right-clicked. Cmd+K exposes this via "Lock all" / "Unlock all".
  const lockAll = useCallback((locked) => {
    const next = shapesRef.current.map((s) => ({ ...s, locked: !!locked }));
    commit(next);
    if (locked) setSelectedId(null);
  }, [commit]);

  const undo = useCallback(() => {
    if (pastRef.current.length === 0) return;
    const prev = pastRef.current.pop();
    futureRef.current.push(shapesRef.current);
    setShapesState(prev);
    saveToStorage(ticker, prev);
    setSelectedId(null);
  }, [ticker]);

  const redo = useCallback(() => {
    if (futureRef.current.length === 0) return;
    const next = futureRef.current.pop();
    pastRef.current.push(shapesRef.current);
    setShapesState(next);
    saveToStorage(ticker, next);
    setSelectedId(null);
  }, [ticker]);

  return {
    shapes,
    selectedId,
    setSelectedId,
    addShape,
    removeShape,
    updateShape,
    duplicateShape,
    clearShapes,
    lockAll,
    undo,
    redo,
    canUndo: pastRef.current.length > 0,
    canRedo: futureRef.current.length > 0,
  };
}
