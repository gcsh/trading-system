/**
 * Phase D.3.2 — drawing-tool registry.
 *
 * Each tool defines:
 *   pointCount  — clicks needed to finalize a shape (1 or 2 today; 3+
 *                 reserved for pitchfork / channel in D.3.3).
 *   draw(ctx, pixelPoints, style)  — paint the finalized shape on the
 *                                    canvas, given converted pixel
 *                                    coordinates for the current view.
 *   hitTest(pixelPoints, x, y, view) — true if (x,y) is "on" the shape.
 *                                       Used for right-click delete.
 *
 * Shapes are stored in DATA space (time + price) so they stay glued to
 * the bars when the operator pans/zooms. The DrawingLayer converts to
 * pixel coordinates on every paint via lightweight-charts' scale APIs.
 */

// Pixel distance threshold for hit-testing.
const HIT_PX = 6;

function dist(x1, y1, x2, y2) {
  return Math.hypot(x2 - x1, y2 - y1);
}

// Perpendicular distance from point (px,py) to segment (x1,y1)-(x2,y2).
function pointToSegmentDistance(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return dist(px, py, x1, y1);
  let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  return dist(px, py, x1 + t * dx, y1 + t * dy);
}

export const DRAWING_TOOLS = {
  trendline: {
    label: 'Trend line',
    pointCount: 2,
    draw(ctx, pts, style) {
      if (pts.length < 2) return;
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 2;
      ctx.setLineDash(style.dash || []);
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      ctx.lineTo(pts[1].x, pts[1].y);
      ctx.stroke();
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      return pointToSegmentDistance(
        x, y, pts[0].x, pts[0].y, pts[1].x, pts[1].y) < HIT_PX;
    },
  },

  horizontal: {
    label: 'Horizontal line',
    pointCount: 1,
    draw(ctx, pts, style, view) {
      if (pts.length < 1) return;
      const y = pts[0].y;
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 1;
      ctx.setLineDash(style.dash || [6, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(view.width, y);
      ctx.stroke();
      // Price label on the right.
      if (style.label) {
        ctx.fillStyle = style.color;
        ctx.font = '11px var(--font-mono, monospace)';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'bottom';
        ctx.fillText(style.label, view.width - 6, y - 4);
      }
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 1) return false;
      return Math.abs(y - pts[0].y) < HIT_PX;
    },
  },

  fib: {
    label: 'Fibonacci retracement',
    pointCount: 2,
    // Standard 7-level retracement set + 161.8 extension.
    levels: [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.618],
    draw(ctx, pts, style, view) {
      if (pts.length < 2) return;
      const [a, b] = pts;
      const top = Math.min(a.y, b.y);
      const bot = Math.max(a.y, b.y);
      const left = Math.min(a.x, b.x);
      const right = Math.max(a.x, b.x);
      const inverted = a.y > b.y;  // 0% is at the bar with the higher price
      const range = bot - top;
      ctx.save();
      // Draw the anchor segment.
      ctx.strokeStyle = style.color;
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      // Draw each retracement level.
      ctx.font = '10px var(--font-mono, monospace)';
      ctx.textBaseline = 'middle';
      ctx.setLineDash([4, 4]);
      for (const lvl of this.levels) {
        const y = inverted
          ? top + range * lvl
          : bot - range * lvl;
        ctx.strokeStyle = lvl === 0 || lvl === 1 ? style.color
          : (lvl === 0.5 ? '#e8606e'
          : (lvl === 0.618 ? '#5fc9ce' : style.color + 'aa'));
        ctx.lineWidth = lvl === 0 || lvl === 1 || lvl === 0.5 ? 1.4 : 1;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(right + 80, y);
        ctx.stroke();
        // Label.
        ctx.fillStyle = ctx.strokeStyle;
        ctx.textAlign = 'left';
        const pct = (lvl * 100).toFixed(lvl === 0.236 || lvl === 0.382
          || lvl === 0.786 || lvl === 0.618 ? 1 : 0);
        ctx.fillText(`${pct}%`, right + 86, y);
      }
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      const [a, b] = pts;
      return pointToSegmentDistance(x, y, a.x, a.y, b.x, b.y) < HIT_PX;
    },
  },

  rect: {
    label: 'Rectangle',
    pointCount: 2,
    draw(ctx, pts, style) {
      if (pts.length < 2) return;
      const x = Math.min(pts[0].x, pts[1].x);
      const y = Math.min(pts[0].y, pts[1].y);
      const w = Math.abs(pts[1].x - pts[0].x);
      const h = Math.abs(pts[1].y - pts[0].y);
      ctx.save();
      ctx.fillStyle = style.color + '22';
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 1.5;
      ctx.setLineDash(style.dash || []);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      const x1 = Math.min(pts[0].x, pts[1].x);
      const y1 = Math.min(pts[0].y, pts[1].y);
      const x2 = Math.max(pts[0].x, pts[1].x);
      const y2 = Math.max(pts[0].y, pts[1].y);
      // Hit on any of the 4 edges (or inside near-edge).
      const onTop    = y >= y1 - HIT_PX && y <= y1 + HIT_PX && x >= x1 && x <= x2;
      const onBot    = y >= y2 - HIT_PX && y <= y2 + HIT_PX && x >= x1 && x <= x2;
      const onLeft   = x >= x1 - HIT_PX && x <= x1 + HIT_PX && y >= y1 && y <= y2;
      const onRight  = x >= x2 - HIT_PX && x <= x2 + HIT_PX && y >= y1 && y <= y2;
      return onTop || onBot || onLeft || onRight;
    },
  },

  text: {
    label: 'Text note',
    pointCount: 1,
    draw(ctx, pts, style) {
      if (pts.length < 1) return;
      const txt = style.text || 'Note';
      ctx.save();
      ctx.font = `${style.fontSize || 12}px var(--font-mono, monospace)`;
      const metrics = ctx.measureText(txt);
      const pad = 4;
      const w = metrics.width + pad * 2;
      const h = (style.fontSize || 12) + pad * 2;
      // Bubble background.
      ctx.fillStyle = 'rgba(13, 17, 31, 0.92)';
      ctx.strokeStyle = style.color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(pts[0].x, pts[0].y - h, w, h, 4);
      ctx.fill();
      ctx.stroke();
      // Text.
      ctx.fillStyle = style.color;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      ctx.fillText(txt, pts[0].x + pad, pts[0].y - h / 2);
      ctx.restore();
    },
    hitTest(pts, x, y, _view, style) {
      if (pts.length < 1) return false;
      const fs = (style && style.fontSize) || 12;
      const w = (style && style.text ? style.text.length * fs * 0.6 : 40) + 8;
      const h = fs + 8;
      return x >= pts[0].x - 2 && x <= pts[0].x + w + 2
        && y >= pts[0].y - h - 2 && y <= pts[0].y + 2;
    },
  },
};

export const DEFAULT_STYLE = {
  color: '#5fc9ce',
  width: 2,
  dash: [],
};
