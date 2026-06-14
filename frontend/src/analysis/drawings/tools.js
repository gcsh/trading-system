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

  // ── Tier-2 extended primitives (D.3.3) ──────────────────────────
  ray: {
    label: 'Ray',
    pointCount: 2,
    draw(ctx, pts, style, view) {
      if (pts.length < 2) return;
      const [a, b] = pts;
      // Extend from a through b out to the right edge of the canvas.
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      let extX = view.width + 50;
      let extY = b.y;
      if (Math.abs(dx) > 1e-6) {
        const t = (view.width - a.x) / dx;
        if (t > 1) {
          extX = a.x + dx * t;
          extY = a.y + dy * t;
        } else {
          extX = b.x;
          extY = b.y;
        }
      }
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 2;
      ctx.setLineDash(style.dash || []);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(extX, extY);
      ctx.stroke();
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      return pointToSegmentDistance(
        x, y, pts[0].x, pts[0].y, pts[1].x, pts[1].y) < HIT_PX;
    },
  },

  extended_line: {
    label: 'Extended line',
    pointCount: 2,
    draw(ctx, pts, style, view) {
      if (pts.length < 2) return;
      const [a, b] = pts;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      // Project both directions to the canvas bounds.
      let xL = 0;
      let yL = a.y;
      let xR = view.width;
      let yR = b.y;
      if (Math.abs(dx) > 1e-6) {
        const tL = (0 - a.x) / dx;
        const tR = (view.width - a.x) / dx;
        xL = a.x + dx * tL; yL = a.y + dy * tL;
        xR = a.x + dx * tR; yR = a.y + dy * tR;
      } else {
        xL = a.x; xR = a.x;
        yL = 0; yR = view.height;
      }
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 2;
      ctx.setLineDash(style.dash || []);
      ctx.beginPath();
      ctx.moveTo(xL, yL);
      ctx.lineTo(xR, yR);
      ctx.stroke();
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      return pointToSegmentDistance(
        x, y, pts[0].x, pts[0].y, pts[1].x, pts[1].y) < HIT_PX;
    },
  },

  vertical: {
    label: 'Vertical line',
    pointCount: 1,
    draw(ctx, pts, style, view) {
      if (pts.length < 1) return;
      const x = pts[0].x;
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 1;
      ctx.setLineDash(style.dash || [6, 4]);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, view.height);
      ctx.stroke();
      ctx.restore();
    },
    hitTest(pts, _x, _y, _view) {
      if (pts.length < 1) return false;
      // Hit only the column 6px wide.
      return Math.abs(_x - pts[0].x) < HIT_PX;
    },
  },

  channel: {
    label: 'Parallel channel',
    pointCount: 3,
    draw(ctx, pts, style, view) {
      if (pts.length < 2) return;
      const [a, b, c] = pts;
      // Baseline a→b
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 2;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      if (c) {
        // Parallel offset = perpendicular distance from baseline to c
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const lenSq = dx * dx + dy * dy;
        const t = lenSq > 0
          ? ((c.x - a.x) * dx + (c.y - a.y) * dy) / lenSq
          : 0;
        const projX = a.x + dx * t;
        const projY = a.y + dy * t;
        const ox = c.x - projX;
        const oy = c.y - projY;
        ctx.beginPath();
        ctx.moveTo(a.x + ox, a.y + oy);
        ctx.lineTo(b.x + ox, b.y + oy);
        ctx.stroke();
        // Translucent fill between the two parallel lines.
        ctx.fillStyle = (style.color || '#5fc9ce') + '20';
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.lineTo(b.x + ox, b.y + oy);
        ctx.lineTo(a.x + ox, a.y + oy);
        ctx.closePath();
        ctx.fill();
      }
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      // Hit either parallel line.
      const baseHit = pointToSegmentDistance(
        x, y, pts[0].x, pts[0].y, pts[1].x, pts[1].y) < HIT_PX;
      if (baseHit || pts.length < 3) return baseHit;
      const dx = pts[1].x - pts[0].x;
      const dy = pts[1].y - pts[0].y;
      const lenSq = dx * dx + dy * dy;
      const t = lenSq > 0
        ? ((pts[2].x - pts[0].x) * dx + (pts[2].y - pts[0].y) * dy) / lenSq
        : 0;
      const projX = pts[0].x + dx * t;
      const projY = pts[0].y + dy * t;
      const ox = pts[2].x - projX;
      const oy = pts[2].y - projY;
      return pointToSegmentDistance(
        x, y, pts[0].x + ox, pts[0].y + oy,
        pts[1].x + ox, pts[1].y + oy) < HIT_PX;
    },
  },

  fib_extension: {
    label: 'Fib extension',
    pointCount: 3,
    // Standard extension ratios.
    levels: [0, 0.382, 0.618, 1.0, 1.272, 1.618, 2.0, 2.618],
    draw(ctx, pts, style, view) {
      if (pts.length < 2) return;
      const [a, b, c] = pts;
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      if (c) {
        ctx.lineTo(c.x, c.y);
      }
      ctx.stroke();
      // Render extension levels measured from the b→a swing,
      // projected from c (or hover).
      if (c) {
        const range = a.y - b.y;
        const left = Math.min(b.x, c.x);
        const right = Math.max(b.x, c.x);
        ctx.font = '10px var(--font-mono, monospace)';
        ctx.textBaseline = 'middle';
        ctx.setLineDash([4, 4]);
        for (const lvl of this.levels) {
          const y = c.y - range * lvl;
          ctx.strokeStyle = lvl === 1.0 || lvl === 1.618 ? style.color
            : style.color + 'aa';
          ctx.lineWidth = lvl === 1.0 || lvl === 1.618 ? 1.4 : 1;
          ctx.beginPath();
          ctx.moveTo(left, y);
          ctx.lineTo(right + 80, y);
          ctx.stroke();
          ctx.fillStyle = ctx.strokeStyle;
          ctx.textAlign = 'left';
          const pct = (lvl * 100).toFixed(
            lvl === 0.382 || lvl === 0.618 || lvl === 1.272
              || lvl === 1.618 || lvl === 2.618 ? 1 : 0);
          ctx.fillText(`${pct}%`, right + 86, y);
        }
      }
      ctx.restore();
    },
    hitTest(pts, x, y) {
      if (pts.length < 2) return false;
      return pointToSegmentDistance(
        x, y, pts[0].x, pts[0].y, pts[1].x, pts[1].y) < HIT_PX;
    },
  },

  pitchfork: {
    label: 'Andrews Pitchfork',
    pointCount: 3,
    draw(ctx, pts, style, view) {
      if (pts.length < 2) return;
      const [p0, p1, p2] = pts;
      ctx.save();
      ctx.strokeStyle = style.color;
      ctx.lineWidth = style.width || 1.5;
      ctx.setLineDash([]);
      if (p2) {
        // Median line: starts at p0, passes through midpoint of p1↔p2.
        const mx = (p1.x + p2.x) / 2;
        const my = (p1.y + p2.y) / 2;
        const dx = mx - p0.x;
        const dy = my - p0.y;
        // Extend out to canvas right edge.
        let extT = 1;
        if (Math.abs(dx) > 1e-6) {
          extT = Math.max(extT, (view.width - p0.x) / dx);
        }
        const endX = p0.x + dx * extT;
        const endY = p0.y + dy * extT;
        // Median
        ctx.beginPath();
        ctx.moveTo(p0.x, p0.y);
        ctx.lineTo(endX, endY);
        ctx.stroke();
        // Upper parallel through p1
        const ux = p1.x - mx;
        const uy = p1.y - my;
        ctx.beginPath();
        ctx.moveTo(p0.x + ux, p0.y + uy);
        ctx.lineTo(endX + ux, endY + uy);
        ctx.stroke();
        // Lower parallel through p2
        ctx.beginPath();
        ctx.moveTo(p0.x - ux, p0.y - uy);
        ctx.lineTo(endX - ux, endY - uy);
        ctx.stroke();
        // Anchor segment p1-p2 (light)
        ctx.save();
        ctx.globalAlpha = 0.4;
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.stroke();
        ctx.restore();
      } else {
        // Preview while collecting the 3rd point.
        ctx.beginPath();
        ctx.moveTo(p0.x, p0.y);
        ctx.lineTo(p1.x, p1.y);
        ctx.stroke();
      }
      ctx.restore();
    },
    hitTest(pts, x, y, view) {
      if (pts.length < 3) {
        if (pts.length === 2) {
          return pointToSegmentDistance(x, y, pts[0].x, pts[0].y,
            pts[1].x, pts[1].y) < HIT_PX;
        }
        return false;
      }
      const [p0, p1, p2] = pts;
      const mx = (p1.x + p2.x) / 2;
      const my = (p1.y + p2.y) / 2;
      return pointToSegmentDistance(x, y, p0.x, p0.y, mx, my) < HIT_PX
        || pointToSegmentDistance(x, y, p0.x, p0.y, p1.x, p1.y) < HIT_PX
        || pointToSegmentDistance(x, y, p0.x, p0.y, p2.x, p2.y) < HIT_PX;
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
