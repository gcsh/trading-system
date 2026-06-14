/**
 * Phase D.3.1 — drawing-state persistence per ticker.
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
 * Undo/redo is intentionally NOT here (deferred to D.4); this hook
 * keeps the surface minimal.
 */
import { useCallback, useEffect, useState } from 'react';

function storageKey(ticker) {
  return `tb.analysis.drawings.${(ticker || '').toUpperCase()}`;
}

function loadFromStorage(ticker) {
  try {
    const raw = globalThis.localStorage.getItem(storageKey(ticker));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((s) => s && s.tool && Array.isArray(s.points));
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

export default function useDrawings(ticker) {
  const [shapes, setShapes] = useState(() => loadFromStorage(ticker));

  // Re-hydrate when the ticker changes.
  useEffect(() => {
    setShapes(loadFromStorage(ticker));
  }, [ticker]);

  const addShape = useCallback((shape) => {
    setShapes((prev) => {
      const next = [...prev, { ...shape, id: shape.id || makeId() }];
      saveToStorage(ticker, next);
      return next;
    });
  }, [ticker]);

  const removeShape = useCallback((id) => {
    setShapes((prev) => {
      const next = prev.filter((s) => s.id !== id);
      saveToStorage(ticker, next);
      return next;
    });
  }, [ticker]);

  const clearShapes = useCallback(() => {
    setShapes([]);
    saveToStorage(ticker, []);
  }, [ticker]);

  return { shapes, addShape, removeShape, clearShapes };
}
