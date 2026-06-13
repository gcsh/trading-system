import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useHeatseekerMulti } from '../hooks/swr/useHeatseekerMulti.js';
import { money } from '../lib/format.js';

/**
 * Phase 19 UI Redesign — Hero GEX Heatmap.
 *
 * Rewrite of the previous `MultiExpiryGexHeatmap` so the matrix becomes
 * the page's primary visual block, NOT a peer panel below the legacy
 * table. Key inversions from the prior layout:
 *
 *   - Y axis = STRIKES (descending, high → low) so the eye reads the
 *     option chain top-down like a ladder.
 *   - X axis = EXPIRIES (left = earliest, right = latest) labelled with
 *     bucket OR date. Uses ALL expirations from the API response — no
 *     more `TARGET_COLS = 13` truncation that hid the term-structure.
 *   - Diverging red ↔ green scale centered at zero net GEX, with a
 *     mid-grey for near-zero. Tokens only — var(--danger) / var(--accent).
 *   - Horizontal SPOT line overlaid as a dashed var(--muted) row.
 *   - Y-axis tick annotations: CALL WALL, PUT WALL, GAMMA FLIP.
 *   - Colour-scale legend pinned top-right INSIDE the panel (5 stops).
 *   - Long chains scroll vertically (max-height container) so NVDA/TSLA
 *     don't blow the page.
 *   - Hover = PORTAL-rendered card; `title=` removed.
 *   - Empty state renders the existing dotted-empty look at full height,
 *     loading state renders a same-shape shimmer skeleton.
 *
 * Props:
 *   ticker        — symbol to query.
 *   dte           — DTE bucket label coming from the controls bar; if
 *                   provided, expirations are filtered client-side. The
 *                   backend bucket labels live in the `.label` field on
 *                   each expiration ("0DTE", "1W", "2W", "3W", "1M",
 *                   ">1M") so we map operator values to those labels.
 *   height        — outer container height (default 520).
 *   onCellClick   — optional callback (cell) => void for drill-in.
 */

// Map the page's DTE dropdown values onto the backend's bucket labels so
// the heatmap can be filtered without a backend round-trip. `all` shows
// every expiry; numeric labels collapse to the corresponding bucket(s).
const DTE_TO_BUCKETS = {
  '0d':  ['0DTE'],
  '1d':  ['0DTE'],
  '5d':  ['0DTE', '1W'],
  '7d':  ['0DTE', '1W'],
  '14d': ['0DTE', '1W', '2W'],
  '30d': ['0DTE', '1W', '2W', '3W', '1M'],
  '60d': ['0DTE', '1W', '2W', '3W', '1M', '>1M'],
  'all': null,
};

// Compact GEX magnitude — mirrors the page-level `gx()` so cell text
// lines up with the KPI strip / table values shown elsewhere.
function gx(v) {
  const n = Number(v) || 0;
  const a = Math.abs(n);
  const s = n < 0 ? '-' : '';
  if (a >= 1e9) return `${s}${(a / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(0)}K`;
  return `${s}${a.toFixed(0)}`;
}

function strikeLabel(s) {
  if (s == null) return '';
  const n = Number(s);
  if (!Number.isFinite(n)) return '';
  if (Math.abs(n - Math.round(n)) < 0.01) return String(Math.round(n));
  return n.toFixed(1);
}

// Short expiry header: "Jun13" / "Jul19" — falls back to DTE if the date
// can't be parsed. Pure-format, no timezone shenanigans.
function shortExpiry(iso) {
  if (!iso) return '—';
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!y || !m || !d) return iso;
  const date = new Date(Date.UTC(y, m - 1, d));
  const mon = date.toLocaleString('en-US', { month: 'short', timeZone: 'UTC' });
  return `${mon}${d}`;
}

// Diverging colour ramp via design tokens. Returns an `rgba()` string
// because the tokens are HEX (#10b981 / #f43f5e); we approximate each
// channel inline so the heatmap stays on-brand without hard-coding new
// colours. Near-zero → transparent so the panel background reads through.
function colorForNetGex(value, maxAbs) {
  if (!maxAbs || !Number.isFinite(value) || value === 0) return 'transparent';
  // sqrt-scaled intensity so smaller cells stay visible next to walls.
  const t = Math.min(1, Math.sqrt(Math.abs(value) / maxAbs));
  const alpha = (0.10 + 0.78 * t).toFixed(2);
  // --accent (#10b981) → 16,185,129  --danger (#f43f5e) → 244,63,94
  if (value > 0) return `rgba(16, 185, 129, ${alpha})`;
  return `rgba(244, 63, 94, ${alpha})`;
}

// Legend stops for the colour-scale chip — five fixed steps so the user
// can map a cell shade back to roughly which order of magnitude it sits
// in. Labels are derived from the matrix max so they're meaningful per
// ticker (NVDA in the billions, ARM in the millions).
function legendStops(maxAbs) {
  if (!maxAbs) return [];
  return [
    { v: -maxAbs,       label: `-${gx(maxAbs)}` },
    { v: -maxAbs * 0.4, label: `-${gx(maxAbs * 0.4)}` },
    { v: 0,             label: '0' },
    { v: maxAbs * 0.4,  label: `+${gx(maxAbs * 0.4)}` },
    { v: maxAbs,        label: `+${gx(maxAbs)}` },
  ];
}

// Portal-rendered hover card. Positioned at (x, y) in viewport space
// with a small offset so the cursor never overlaps the tip. Escapes the
// heatmap's overflow:auto clipping so long chains can still hover near
// the edge without the card getting cut off.
function HoverPortal({ cell, x, y }) {
  if (!cell) return null;
  const styleObj = {
    position: 'fixed',
    left: Math.min(window.innerWidth - 220, x + 14),
    top: Math.min(window.innerHeight - 140, y + 14),
    zIndex: 9999,
    background: 'var(--panel-2)',
    border: '1px solid var(--border-strong)',
    borderRadius: 6,
    padding: '8px 10px',
    fontSize: 11.5,
    color: 'var(--text)',
    fontFeatureSettings: '"tnum"',
    boxShadow: '0 6px 24px rgba(0,0,0,0.45)',
    pointerEvents: 'none',
    minWidth: 200,
  };
  return createPortal(
    <div style={styleObj}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <strong style={{ color: 'var(--text)' }}>Strike {strikeLabel(cell.strike)}</strong>
        <span style={{ color: 'var(--muted)' }}>{cell.expiryShort}</span>
      </div>
      <div style={{ color: 'var(--muted)', fontSize: 10.5, marginBottom: 4 }}>
        {cell.expiryDate || cell.bucket} · DTE {cell.dte ?? '—'}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '2px 10px' }}>
        <span style={{ color: 'var(--muted)' }}>Call GEX</span>
        <span style={{ color: 'var(--accent)' }}>{gx(cell.call_gex)}</span>
        <span style={{ color: 'var(--muted)' }}>Put GEX</span>
        <span style={{ color: 'var(--danger)' }}>{gx(cell.put_gex)}</span>
        <span style={{ color: 'var(--muted)' }}>Net GEX</span>
        <span style={{ color: cell.net_gex >= 0 ? 'var(--accent)' : 'var(--danger)', fontWeight: 700 }}>
          {gx(cell.net_gex)}
        </span>
      </div>
    </div>,
    document.body,
  );
}

// Same dotted look as the rest of the site but pinned to the hero's
// reserved height so the page doesn't reflow when data is missing.
function EmptyState({ note, height }) {
  return (
    <div
      className="empty"
      style={{
        height,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'var(--muted)',
        fontSize: 13,
        gap: 6,
      }}
    >
      <div style={{ fontSize: 24 }}>⏳</div>
      <div style={{ fontWeight: 600, color: 'var(--text-soft)' }}>
        Multi-expiration data unavailable
      </div>
      <div>yfinance rate-limited; retry in 1 min</div>
      {note && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--warn)' }}>{note}</div>
      )}
    </div>
  );
}

// Loading skeleton shaped like the heatmap (header + grid + legend) so
// the page doesn't jump when data resolves. Uses a slow opacity pulse
// driven by CSS-keyframe-free inline styles — keeps the bundle clean.
function Skeleton({ height }) {
  return (
    <div style={{ height, display: 'grid', gridTemplateRows: '24px 1fr', gap: 8 }}>
      <div style={{ display: 'flex', gap: 4 }}>
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} style={{ flex: 1, background: 'var(--panel-2)', borderRadius: 3 }} />
        ))}
      </div>
      <div style={{
        background: 'repeating-linear-gradient(0deg, var(--panel-2) 0 38px, var(--panel) 38px 40px)',
        opacity: 0.6,
        borderRadius: 4,
      }} />
    </div>
  );
}

const CELL_HEIGHT = 36;
const CELL_MIN_WIDTH = 56;
const STRIKE_AXIS_WIDTH = 64;
const EXPIRY_AXIS_HEIGHT = 36;

export default function GexHeatmapHero({
  ticker,
  dte = 'all',
  height = 520,
  onCellClick,
}) {
  // Single source of truth — same SWR hook used in the legacy widget so
  // any other consumer (Stock Analysis, etc.) shares the in-flight cache.
  const {
    spotPrice,
    expirations,
    note,
    isLoading,
    error,
  } = useHeatseekerMulti(ticker, { refreshInterval: 60_000, enabled: !!ticker });

  // Hover state lives at the component level so React re-renders the
  // portal in sync with mouse-moves. We track viewport coords (clientX/Y)
  // because the portal is rendered into document.body.
  const [hover, setHover] = useState(null);   // { cell, x, y }
  const scrollerRef = useRef(null);

  // Filter to the operator's DTE bucket selection. Backend already buckets
  // the chain; we trust `.label` over arithmetic on `.dte` so the UI stays
  // consistent with the rest of the site.
  const filteredExpirations = useMemo(() => {
    if (!expirations?.length) return [];
    const allow = DTE_TO_BUCKETS[dte] || null;
    if (!allow) return expirations;
    return expirations.filter((e) => allow.includes(e.label));
  }, [expirations, dte]);

  // Build a dense matrix:
  //   strikes (sorted DESC) × expirations (sorted ASC by DTE)
  //   cell  = { call_gex, put_gex, net_gex }
  // Empty cells are emitted explicitly so the grid is always rectangular,
  // and so a "no contract here" cell can be rendered as a dim dot instead
  // of disappearing (which would mis-read as zero net GEX).
  const matrix = useMemo(() => {
    if (!filteredExpirations.length) {
      return { strikes: [], cols: [], cellsByKey: new Map(), maxAbs: 0 };
    }
    const cols = [...filteredExpirations].sort(
      (a, b) => (a.dte ?? 9e9) - (b.dte ?? 9e9),
    );
    const strikeSet = new Set();
    const cellsByKey = new Map();
    let mx = 0;
    cols.forEach((exp, colIdx) => {
      for (const r of (exp.gex_by_strike || [])) {
        const k = Number(r.strike);
        if (!Number.isFinite(k)) continue;
        strikeSet.add(k);
        const cgx = Number(r.call_gex || 0);
        const pgx = Number(r.put_gex || 0);
        const ngx = Number(r.net_gex || 0);
        cellsByKey.set(`${k}|${colIdx}`, {
          strike: k,
          call_gex: cgx,
          put_gex: pgx,
          net_gex: ngx,
          expiry: exp.expiry,
          expiryDate: exp.expiry,
          expiryShort: shortExpiry(exp.expiry),
          bucket: exp.label,
          dte: exp.dte,
        });
        const a = Math.abs(ngx);
        if (a > mx) mx = a;
      }
    });
    const strikes = [...strikeSet].sort((a, b) => b - a); // DESC: top = highest
    return { strikes, cols, cellsByKey, maxAbs: mx };
  }, [filteredExpirations]);

  // Spot row index (closest strike to spot price) so we can overlay the
  // dashed SPOT line at the correct vertical offset. Returned as a fraction
  // of CELL_HEIGHT so the line sits on the row's centre.
  const spotRowIdx = useMemo(() => {
    if (!matrix.strikes.length || !Number.isFinite(Number(spotPrice))) return null;
    let best = 0;
    let bestDist = Infinity;
    matrix.strikes.forEach((s, i) => {
      const d = Math.abs(s - spotPrice);
      if (d < bestDist) { bestDist = d; best = i; }
    });
    return best;
  }, [matrix.strikes, spotPrice]);

  // Wall / flip annotations on the strike axis. We compute them from the
  // visible chain so they're consistent with the filtered DTE window.
  const annotations = useMemo(() => {
    if (!matrix.strikes.length) return {};
    // Sum net GEX per strike across all visible expiries.
    const perStrike = new Map();
    let callMax = -Infinity, callMaxStrike = null;
    let putMin = Infinity,   putMinStrike = null;
    for (const s of matrix.strikes) {
      let net = 0, call = 0, put = 0;
      matrix.cols.forEach((_, colIdx) => {
        const c = matrix.cellsByKey.get(`${s}|${colIdx}`);
        if (c) { net += c.net_gex; call += c.call_gex; put += c.put_gex; }
      });
      perStrike.set(s, net);
      if (call > callMax) { callMax = call; callMaxStrike = s; }
      if (put < putMin)   { putMin = put;   putMinStrike = s; }
    }
    // Gamma flip = strike where running cumulative (top→down) crosses zero.
    let cum = 0, flipStrike = null, prevCum = 0;
    for (const s of matrix.strikes) {
      const v = perStrike.get(s) || 0;
      const next = cum + v;
      if (cum !== 0 && Math.sign(next) !== Math.sign(cum)) {
        flipStrike = s;
        break;
      }
      prevCum = cum;
      cum = next;
    }
    return { callWall: callMaxStrike, putWall: putMinStrike, flip: flipStrike };
  }, [matrix]);

  const handleEnter = useCallback((cell, ev) => {
    setHover({ cell, x: ev.clientX, y: ev.clientY });
  }, []);
  const handleMove = useCallback((ev) => {
    setHover((h) => (h ? { ...h, x: ev.clientX, y: ev.clientY } : h));
  }, []);
  const handleLeave = useCallback(() => setHover(null), []);
  // Clear hover on scroll/blur so the portal doesn't strand on screen.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return undefined;
    const clear = () => setHover(null);
    el.addEventListener('scroll', clear);
    window.addEventListener('blur', clear);
    return () => {
      el.removeEventListener('scroll', clear);
      window.removeEventListener('blur', clear);
    };
  }, []);

  // --- Render branches ---

  if (error) {
    return (
      <div className="panel" style={{ marginBottom: 0 }}>
        <div className="empty" style={{ height, color: 'var(--warn)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          Could not load /heatseeker/multi/{ticker}: {String(error?.message || error)}
        </div>
      </div>
    );
  }

  if (isLoading && !matrix.strikes.length) {
    return (
      <div className="panel" style={{ marginBottom: 0 }}>
        <Skeleton height={height} />
      </div>
    );
  }

  if (!matrix.strikes.length) {
    return (
      <div className="panel" style={{ marginBottom: 0 }}>
        <EmptyState note={note} height={height} />
      </div>
    );
  }

  const stops = legendStops(matrix.maxAbs);
  const gridWidth = STRIKE_AXIS_WIDTH + matrix.cols.length * CELL_MIN_WIDTH;
  const gridHeight = matrix.strikes.length * CELL_HEIGHT;

  return (
    <div className="panel" style={{ marginBottom: 0, position: 'relative' }}>
      {/* Header strip: title + colour-scale legend pinned top-right. */}
      <div
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 10, gap: 12, flexWrap: 'wrap',
        }}
      >
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)' }}>
            GEX term-structure heatmap
          </div>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>
            Strikes (rows, high → low) × expiries (columns, near → far). Green = call γ, red = put γ.
          </div>
        </div>
        {/* Colour-scale legend — five stops sourced from the matrix max so
            the same gradient maps to the same dollar magnitudes the cells
            are using. */}
        <div
          aria-label="Colour scale"
          style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '6px 8px', border: '1px solid var(--border)',
            borderRadius: 6, background: 'var(--panel-2)',
            fontSize: 10, color: 'var(--muted)',
            fontFeatureSettings: '"tnum"',
          }}
        >
          {stops.map((s) => (
            <span key={s.label} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <span
                style={{
                  display: 'inline-block', width: 14, height: 14, borderRadius: 3,
                  background: colorForNetGex(s.v, matrix.maxAbs) === 'transparent'
                    ? 'var(--panel-3)'
                    : colorForNetGex(s.v, matrix.maxAbs),
                  border: '1px solid var(--border)',
                }}
              />
              {s.label}
            </span>
          ))}
        </div>
      </div>

      {/* Scrollable grid container. Y-overflow lets long chains
          (NVDA/TSLA) scroll without breaking the page layout. */}
      <div
        ref={scrollerRef}
        style={{
          maxHeight: height,
          overflow: 'auto',
          border: '1px solid var(--border)',
          borderRadius: 6,
          position: 'relative',
        }}
        onMouseLeave={handleLeave}
      >
        <div
          style={{
            position: 'relative',
            width: gridWidth,
            minWidth: '100%',
          }}
        >
          {/* Column header row — expiry labels. Sticky so the operator
              can scroll the strike axis and still see which column is
              which date. */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: `${STRIKE_AXIS_WIDTH}px repeat(${matrix.cols.length}, minmax(${CELL_MIN_WIDTH}px, 1fr))`,
              position: 'sticky',
              top: 0,
              background: 'var(--panel)',
              zIndex: 2,
              borderBottom: '1px solid var(--border)',
              height: EXPIRY_AXIS_HEIGHT,
            }}
          >
            <div style={{
              padding: '6px 8px', fontSize: 10, color: 'var(--muted)',
              textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600,
            }}>
              Strike
            </div>
            {matrix.cols.map((exp) => (
              <div
                key={exp.expiry}
                style={{
                  padding: '6px 4px', textAlign: 'center', fontSize: 10.5,
                  color: 'var(--muted)', fontWeight: 600,
                  borderLeft: '1px solid var(--border)',
                  display: 'flex', flexDirection: 'column', justifyContent: 'center',
                }}
              >
                <div style={{ color: 'var(--text-soft)' }}>{shortExpiry(exp.expiry)}</div>
                <div style={{ fontSize: 9.5, color: 'var(--muted-2)' }}>
                  {exp.label} · {exp.dte}d
                </div>
              </div>
            ))}
          </div>

          {/* Body grid — one row per strike. */}
          <div style={{ position: 'relative', height: gridHeight }}>
            {/* Spot line overlay. Positioned absolutely on the body so
                it doesn't perturb cell sizing. */}
            {spotRowIdx != null && (
              <div
                aria-hidden
                style={{
                  position: 'absolute',
                  top: spotRowIdx * CELL_HEIGHT + CELL_HEIGHT / 2,
                  left: STRIKE_AXIS_WIDTH,
                  right: 0,
                  height: 0,
                  borderTop: '1px dashed var(--muted)',
                  zIndex: 3,
                  pointerEvents: 'none',
                }}
              />
            )}

            {matrix.strikes.map((strike, rowIdx) => {
              const isCallWall = annotations.callWall === strike;
              const isPutWall  = annotations.putWall === strike;
              const isFlip     = annotations.flip === strike;
              const isSpot     = spotRowIdx === rowIdx;
              const tag = isFlip ? { label: 'FLIP', color: 'var(--warn)' }
                        : isCallWall ? { label: 'CALL WALL', color: 'var(--accent)' }
                        : isPutWall ? { label: 'PUT WALL', color: 'var(--danger)' }
                        : null;
              return (
                <div
                  key={strike}
                  style={{
                    display: 'grid',
                    gridTemplateColumns: `${STRIKE_AXIS_WIDTH}px repeat(${matrix.cols.length}, minmax(${CELL_MIN_WIDTH}px, 1fr))`,
                    height: CELL_HEIGHT,
                    borderBottom: '1px solid var(--border)',
                    background: isSpot ? 'var(--panel-2)' : 'transparent',
                  }}
                >
                  {/* Strike axis cell — sticky so it stays visible when
                      the grid scrolls horizontally on narrow viewports. */}
                  <div
                    style={{
                      position: 'sticky',
                      left: 0,
                      background: isSpot ? 'var(--panel-2)' : 'var(--panel)',
                      borderRight: '1px solid var(--border)',
                      padding: '0 8px',
                      display: 'flex',
                      flexDirection: 'column',
                      justifyContent: 'center',
                      fontFeatureSettings: '"tnum"',
                      zIndex: 1,
                    }}
                  >
                    <span style={{
                      fontWeight: tag || isSpot ? 700 : 500,
                      color: isSpot ? 'var(--info)' : 'var(--text)',
                      fontSize: 12,
                    }}>
                      {strikeLabel(strike)}
                    </span>
                    {tag && (
                      <span style={{
                        fontSize: 8.5, fontWeight: 700, color: tag.color,
                        letterSpacing: '0.05em',
                      }}>
                        {tag.label}
                      </span>
                    )}
                  </div>
                  {matrix.cols.map((exp, colIdx) => {
                    const cell = matrix.cellsByKey.get(`${strike}|${colIdx}`);
                    const v = cell ? cell.net_gex : 0;
                    const bg = colorForNetGex(v, matrix.maxAbs);
                    const isStrong = Math.abs(v) >= matrix.maxAbs * 0.55;
                    return (
                      <div
                        key={exp.expiry}
                        onMouseEnter={cell ? (e) => handleEnter(cell, e) : undefined}
                        onMouseMove={cell ? handleMove : undefined}
                        onMouseLeave={cell ? handleLeave : undefined}
                        onClick={cell && onCellClick ? () => onCellClick(cell) : undefined}
                        style={{
                          borderLeft: '1px solid var(--border)',
                          background: bg,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          fontSize: 10.5,
                          fontFeatureSettings: '"tnum"',
                          color: cell
                            ? (Math.abs(v) === 0 ? 'var(--muted-2)' : 'var(--text)')
                            : 'var(--muted-2)',
                          fontWeight: isStrong ? 700 : 500,
                          cursor: cell && onCellClick ? 'pointer' : 'default',
                        }}
                      >
                        {cell ? (v === 0 ? '·' : gx(v)) : '·'}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Spot read-out under the grid so the dashed line has a labelled
          counterpart even when the row is scrolled off-screen. */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginTop: 8, fontSize: 11, color: 'var(--muted)',
      }}>
        <div>
          {spotPrice != null && (
            <span>Spot {money(spotPrice)} · dashed row marks nearest strike</span>
          )}
        </div>
        <div>
          {matrix.cols.length} expiries · {matrix.strikes.length} strikes
        </div>
      </div>

      <HoverPortal cell={hover?.cell} x={hover?.x ?? 0} y={hover?.y ?? 0} />
    </div>
  );
}
