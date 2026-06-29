import { useEffect, useRef, useState } from 'react';

const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

/**
 * Shared chart viewport: a visible window [vStart, vStart+vCount) into an array
 * of `n` candles, with drag-to-pan and wheel/pinch zoom. Charts compute their
 * geometry from vStart/vCount and wire the returned handlers.
 *
 * `view === null` means "show everything" (the default whenever the data size
 * changes — e.g. switching ticker or timeframe).
 */
export function useTimelineViewport(n) {
  const [view, setView] = useState(null);
  const dragRef = useRef(null);

  // Reset to the full range whenever the dataset size changes.
  useEffect(() => { setView(null); }, [n]);

  const vCount = view ? Math.max(8, Math.min(view.count, n)) : n;
  const vStart = view ? clamp(view.start, 0, Math.max(0, n - vCount)) : 0;

  const beginDrag = (clientX) => { dragRef.current = { x0: clientX, start0: vStart, count: vCount }; };
  const isDragging = () => !!dragRef.current;
  const endDrag = () => { dragRef.current = null; };

  const dragTo = (clientX, xStep) => {
    const d = dragRef.current;
    if (!d || !xStep) return;
    const shift = Math.round((d.x0 - clientX) / xStep);
    setView({ start: clamp(d.start0 + shift, 0, Math.max(0, n - d.count)), count: d.count });
  };

  // factor < 1 zooms in (fewer candles), > 1 zooms out. `frac` is the cursor's
  // horizontal position (0..1) so we zoom around what's under the pointer.
  const zoom = (factor, frac) => setView((v) => {
    const c0 = v ? Math.min(v.count, n) : n;
    const s0 = v ? v.start : 0;
    const c = clamp(Math.round(c0 * factor), 8, n);
    const cursorI = s0 + frac * c0;
    const s = clamp(Math.round(cursorI - frac * c), 0, Math.max(0, n - c));
    return { start: s, count: c };
  });

  const reset = () => setView(null);

  return { vStart, vCount, isZoomed: vCount < n || vStart > 0, beginDrag, isDragging, endDrag, dragTo, zoom, reset };
}

/**
 * Zoom on wheel — but ONLY while Shift (or Ctrl/⌘) is held, so plain scrolling
 * still moves the page. The chart is embedded in a scrollable page, so we must
 * not hijack ordinary wheel scrolling.
 */
export function useWheelZoom(ref, onZoom, deps = []) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const handler = (e) => {
      if (!(e.shiftKey || e.ctrlKey || e.metaKey)) return; // let the page scroll
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const frac = rect.width ? clamp((e.clientX - rect.left) / rect.width, 0, 1) : 0.5;
      onZoom(e.deltaY > 0 ? 1.18 : 0.85, frac);
    };
    el.addEventListener('wheel', handler, { passive: false });
    return () => el.removeEventListener('wheel', handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
