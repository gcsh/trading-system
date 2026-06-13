/**
 * Feature-Merge F4 — Full Screen Analysis Mode wrapper.
 *
 * Pre-Phase-19.x: this was a single-column overlay that toggled a
 * `position: fixed` container. It didn't help operators understand the
 * chart because the overlay legend, thesis cards, and pattern context
 * were left BEHIND in the page layout.
 *
 * Phase-19.x rewrite: full-screen mode now ships a 70/30 split.
 *
 *   ┌──────────────────────────────────┬──────────────┐
 *   │                                  │ Price Action │  ← accordion of
 *   │                                  │   ▸ S/D zones│    overlay groups
 *   │                                  │   ▸ VWAP     │    (Solo / hide)
 *   │  CHART  (lightweight-charts +    │   ▸ Wyckoff  │
 *   │   canvas overlay live here)      ├──────────────┤
 *   │                                  │ Volume       │
 *   │                                  │   ▸ Profile  │
 *   │                                  │   ▸ MA20     │
 *   │                                  ├──────────────┤
 *   │  fills `calc(100vh - 56px)`      │ Options      │
 *   │                                  │   ▸ Gamma    │
 *   │                                  │   ▸ Walls    │
 *   │                                  ├──────────────┤
 *   │                                  │ Structure    │
 *   │                                  │   ▸ Fib      │
 *   │                                  │   ▸ Elliott  │
 *   │                                  ├──────────────┤
 *   │                                  │ THESIS       │
 *   │                                  │ (cards)      │
 *   └──────────────────────────────────┴──────────────┘
 *
 * Each accordion group has a "Solo" button — clicking it hides every
 * OTHER overlay in that group so the operator can focus on one signal.
 *
 * Keyboard:
 *   ESC  → exit fullscreen
 *   F    → toggle fullscreen (works in normal mode too)
 *
 * Backward compatibility: if a caller does NOT pass `overlayGroups` or
 * `thesisCards`, we fall back to the legacy single-column behavior. The
 * legacy `overlays` flat-list prop also still works.
 *
 *   props:
 *     ticker          — for per-ticker fullscreen persistence
 *     children        — the chart subtree (NEVER remounted)
 *     overlays        — legacy flat list [{ id, label, color, visible }]
 *     onToggleOverlay — (id) => void
 *     overlayGroups   — NEW { groupName: [{key,label,color,enabled}] }
 *                       — when present, the right-rail accordion uses
 *                         these GROUPS; each group renders its overlays
 *                         with an individual toggle + a Solo button.
 *     onToggleGroupOverlay — NEW (groupName, key) => void
 *     onSoloGroupOverlay   — NEW (groupName, key) => void
 *     thesisCards     — NEW ReactNode rendered at the bottom of the
 *                         right rail (under the accordion).
 *     toolbarLeft     — extra controls rendered to the left of overlays
 *     toolbarRight    — extra controls next to the expand button
 *     height          — default height when NOT fullscreen
 *
 * NON-GOALS:
 *   - Does NOT change layout when collapsed.
 *   - Does NOT touch the chart's data or rendering.
 *   - Does NOT import from frontend/src/design/* or frontend/src/v2/*.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const STORAGE_PREFIX = 'tb.chart.fullscreen.';
const GROUP_COLLAPSE_PREFIX = 'tb.chart.fs.group.';
const TOOLBAR_HEIGHT_PX = 56;

function storageKey(ticker) {
  return `${STORAGE_PREFIX}${(ticker || 'default').toUpperCase()}`;
}

function groupCollapseKey(name) {
  return `${GROUP_COLLAPSE_PREFIX}${name}`;
}

export default function ChartFullscreenWrapper({
  ticker,
  children,
  overlays = [],         // legacy flat: [{ id, label, color, visible }]
  onToggleOverlay,       // (id) => void
  overlayGroups = null,  // NEW: { groupName: [{key,label,color,enabled}] }
  onToggleGroupOverlay,  // NEW: (groupName, key) => void
  onSoloGroupOverlay,    // NEW: (groupName, key) => void
  thesisCards = null,    // NEW: ReactNode (right-rail thesis context)
  toolbarRight = null,
  toolbarLeft = null,
  height,
}) {
  const [fullscreen, setFullscreen] = useState(false);
  const containerRef = useRef(null);
  const longPressTimer = useRef(null);

  // Per-group accordion collapse state, persisted across reloads.
  const [collapsed, setCollapsed] = useState({});

  // Whether the caller is using the new grouped contract.
  const useGroupedRail = !!(overlayGroups && Object.keys(overlayGroups).length > 0);

  // Restore last-known fullscreen state per ticker.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      const v = window.localStorage.getItem(storageKey(ticker));
      if (v === '1') setFullscreen(true);
    } catch (_) { /* ignore */ }
  }, [ticker]);

  // Persist fullscreen state.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(storageKey(ticker), fullscreen ? '1' : '0');
    } catch (_) { /* ignore */ }
  }, [fullscreen, ticker]);

  // Restore per-group collapse state when overlayGroups arrives.
  useEffect(() => {
    if (!useGroupedRail || typeof window === 'undefined') return;
    const next = {};
    for (const name of Object.keys(overlayGroups)) {
      try {
        const v = window.localStorage.getItem(groupCollapseKey(name));
        next[name] = v === '1';
      } catch (_) { /* ignore */ }
    }
    setCollapsed(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useGroupedRail, overlayGroups ? Object.keys(overlayGroups).join('|') : '']);

  const toggleCollapsed = useCallback((name) => {
    setCollapsed((prev) => {
      const next = { ...prev, [name]: !prev[name] };
      try {
        if (typeof window !== 'undefined') {
          window.localStorage.setItem(groupCollapseKey(name), next[name] ? '1' : '0');
        }
      } catch (_) { /* ignore */ }
      return next;
    });
  }, []);

  // ESC to exit + F to toggle. The F-key listener is active in both
  // collapsed and fullscreen modes so operators can hit F from anywhere.
  useEffect(() => {
    const onKey = (e) => {
      // Ignore when typing in inputs / textareas / contenteditable.
      const t = e.target;
      const tag = (t && t.tagName) || '';
      const isText = tag === 'INPUT' || tag === 'TEXTAREA'
        || (t && t.isContentEditable);
      if (isText) return;
      if (e.key === 'Escape' && fullscreen) {
        setFullscreen(false);
      } else if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        setFullscreen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fullscreen]);

  // Lock body scroll in fullscreen.
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const prev = document.body.style.overflow;
    if (fullscreen) document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [fullscreen]);

  // Mobile long-press toggle.
  const handleTouchStart = useCallback(() => {
    if (longPressTimer.current) clearTimeout(longPressTimer.current);
    longPressTimer.current = setTimeout(() => {
      setFullscreen((v) => !v);
    }, 500);
  }, []);
  const cancelLongPress = useCallback(() => {
    if (longPressTimer.current) {
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  }, []);

  const toggle = () => setFullscreen((v) => !v);

  // ── Toolbar (shared across modes) ───────────────────────────────────
  const renderLegacyChips = () => {
    if (!overlays || overlays.length === 0) return null;
    return (
      <div
        className="tb-chart-fs-overlays"
        role="group"
        aria-label="Theory overlays"
        style={{
          display: 'flex',
          gap: 4,
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        {overlays.map((ov) => (
          <button
            key={ov.id}
            type="button"
            onClick={() => onToggleOverlay && onToggleOverlay(ov.id)}
            data-testid={`overlay-toggle-${ov.id}`}
            aria-pressed={ov.visible !== false}
            title={`Toggle ${ov.label}`}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 5,
              padding: '2px 8px',
              borderRadius: 999,
              fontSize: 11,
              cursor: 'pointer',
              background: ov.visible !== false
                ? `${ov.color || '#5fc9ce'}22`
                : 'transparent',
              color: ov.visible !== false
                ? (ov.color || 'var(--text)')
                : 'var(--muted)',
              border: `1px solid ${
                ov.visible !== false
                  ? (ov.color || 'var(--border)')
                  : 'var(--border)'
              }`,
              opacity: ov.visible !== false ? 1 : 0.55,
              textDecoration: ov.visible !== false ? 'none' : 'line-through',
            }}
          >
            <span style={{
              display: 'inline-block',
              width: 8, height: 8, borderRadius: '50%',
              background: ov.color || '#5fc9ce',
              opacity: ov.visible !== false ? 1 : 0.4,
            }} />
            {ov.label}
          </button>
        ))}
      </div>
    );
  };

  const toolbar = (
    <div
      className="tb-chart-fs-toolbar"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        marginBottom: 6,
        flexWrap: 'wrap',
        minHeight: 30,
      }}
    >
      {toolbarLeft}
      {/* The legacy flat chips remain visible in collapsed (non-fullscreen)
          mode even when the new grouped rail is supplied, so operators
          can still toggle overlays without expanding the chart. */}
      {!fullscreen && renderLegacyChips()}
      {fullscreen && !useGroupedRail && renderLegacyChips()}
      <div style={{ flex: 1 }} />
      {toolbarRight}
      <button
        type="button"
        className="btn small"
        onClick={toggle}
        data-testid="chart-fullscreen-toggle"
        aria-pressed={fullscreen}
        title={fullscreen ? 'Exit fullscreen (Esc or F)' : 'Expand chart to fullscreen (F)'}
        style={{ padding: '3px 9px', fontSize: 11, fontWeight: 600 }}
      >
        {fullscreen ? '× Exit' : '⛶ Expand'}
      </button>
    </div>
  );

  // ── Right-rail accordion (fullscreen + grouped contract) ────────────
  const accordion = useMemo(() => {
    if (!useGroupedRail) return null;
    return (
      <div
        className="tb-chart-fs-accordion"
        role="group"
        aria-label="Overlay groups"
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
        }}
      >
        {Object.entries(overlayGroups).map(([groupName, items]) => {
          const isCollapsed = !!collapsed[groupName];
          const itemList = Array.isArray(items) ? items : [];
          const anyEnabled = itemList.some((it) => it.enabled !== false);
          return (
            <div
              key={groupName}
              className="tb-chart-fs-accordion-group"
              data-testid={`overlay-group-${groupName}`}
              style={{
                border: '1px solid var(--border, #2a3349)',
                borderRadius: 8,
                background: 'var(--panel, #0d111f)',
                overflow: 'hidden',
              }}
            >
              <button
                type="button"
                onClick={() => toggleCollapsed(groupName)}
                aria-expanded={!isCollapsed}
                style={{
                  display: 'flex',
                  width: '100%',
                  alignItems: 'center',
                  gap: 8,
                  padding: '8px 10px',
                  background: 'transparent',
                  color: 'var(--text, #e6edf3)',
                  border: 'none',
                  cursor: 'pointer',
                  fontSize: 12,
                  fontWeight: 700,
                  letterSpacing: 0.4,
                  textTransform: 'uppercase',
                }}
              >
                <span style={{
                  display: 'inline-block',
                  width: 8, height: 8, borderRadius: '50%',
                  background: anyEnabled ? 'var(--accent, #26d07c)' : 'var(--muted, #5b6985)',
                }} />
                <span style={{ flex: 1, textAlign: 'left' }}>{groupName}</span>
                <span style={{ fontSize: 10, color: 'var(--muted, #8593b0)' }}>
                  {itemList.filter((it) => it.enabled !== false).length}/{itemList.length}
                </span>
                <span style={{ fontSize: 10, color: 'var(--muted, #8593b0)' }}>
                  {isCollapsed ? '▸' : '▾'}
                </span>
              </button>
              {!isCollapsed && itemList.length > 0 && (
                <div style={{
                  padding: '4px 10px 10px',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 4,
                }}>
                  {itemList.map((it) => {
                    const enabled = it.enabled !== false;
                    return (
                      <div
                        key={it.key}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                          fontSize: 12,
                        }}
                      >
                        <button
                          type="button"
                          onClick={() => onToggleGroupOverlay
                            && onToggleGroupOverlay(groupName, it.key)}
                          aria-pressed={enabled}
                          data-testid={`overlay-group-toggle-${groupName}-${it.key}`}
                          title={`Toggle ${it.label}`}
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 6,
                            flex: 1,
                            minWidth: 0,
                            padding: '4px 6px',
                            borderRadius: 6,
                            border: `1px solid ${enabled
                              ? (it.color || 'var(--border, #2a3349)')
                              : 'var(--border, #2a3349)'}`,
                            background: enabled
                              ? `${it.color || '#5fc9ce'}1a`
                              : 'transparent',
                            color: enabled
                              ? (it.color || 'var(--text, #e6edf3)')
                              : 'var(--muted, #8593b0)',
                            cursor: 'pointer',
                            fontSize: 11.5,
                            textAlign: 'left',
                            opacity: enabled ? 1 : 0.6,
                          }}
                        >
                          <span style={{
                            display: 'inline-block',
                            width: 8, height: 8, borderRadius: '50%',
                            background: it.color || '#5fc9ce',
                            opacity: enabled ? 1 : 0.35,
                          }} />
                          <span style={{
                            flex: 1,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}>
                            {it.label}
                          </span>
                        </button>
                        <button
                          type="button"
                          onClick={() => onSoloGroupOverlay
                            && onSoloGroupOverlay(groupName, it.key)}
                          data-testid={`overlay-group-solo-${groupName}-${it.key}`}
                          title={`Solo ${it.label} (hide other ${groupName} overlays)`}
                          style={{
                            padding: '3px 8px',
                            fontSize: 10,
                            fontWeight: 600,
                            letterSpacing: 0.4,
                            textTransform: 'uppercase',
                            borderRadius: 6,
                            border: '1px solid var(--border, #2a3349)',
                            background: 'transparent',
                            color: 'var(--muted, #8593b0)',
                            cursor: 'pointer',
                          }}
                        >
                          Solo
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  }, [useGroupedRail, overlayGroups, collapsed,
       onToggleGroupOverlay, onSoloGroupOverlay, toggleCollapsed]);

  // ── Layout: legacy single-column vs new 70/30 split ─────────────────
  const useTwoColumn = fullscreen && (useGroupedRail || !!thesisCards);

  const wrapperStyle = fullscreen ? {
    position: 'fixed',
    inset: 0,
    zIndex: 1000,
    background: 'var(--bg, #0a0e1a)',
    padding: 12,
    display: 'flex',
    flexDirection: 'column',
  } : {
    position: 'relative',
  };

  // chart pane fills calc(100vh - 56px) when fullscreen so it never
  // overflows. The 56px header reservation matches our toolbar + Esc hint.
  const chartPaneStyle = fullscreen ? {
    height: `calc(100vh - ${TOOLBAR_HEIGHT_PX}px)`,
    minHeight: 0,
    display: 'flex',
    flexDirection: 'column',
  } : {
    height: height ? height : 'auto',
    display: 'flex',
    flexDirection: 'column',
  };

  return (
    <div
      ref={containerRef}
      className={`tb-chart-fs-wrapper ${fullscreen ? 'is-fullscreen' : ''}`}
      data-fullscreen={fullscreen ? '1' : '0'}
      data-testid="chart-fs-wrapper"
      style={wrapperStyle}
      onTouchStart={handleTouchStart}
      onTouchEnd={cancelLongPress}
      onTouchCancel={cancelLongPress}
      onTouchMove={cancelLongPress}
    >
      {toolbar}

      {useTwoColumn ? (
        <div
          className="tb-chart-fs-split"
          data-testid="chart-fs-split"
          style={{
            flex: 1,
            minHeight: 0,
            display: 'grid',
            gridTemplateColumns: '70fr 30fr',
            gap: 12,
          }}
        >
          {/* Chart pane — 70%. */}
          <div
            className="tb-chart-fs-body"
            data-testid="chart-fs-body"
            style={{
              minWidth: 0,
              ...chartPaneStyle,
            }}
          >
            {children}
          </div>

          {/* Right rail — 30%. Accordion at top, thesis at bottom. */}
          <aside
            className="tb-chart-fs-rail"
            data-testid="chart-fs-rail"
            style={{
              minWidth: 0,
              overflowY: 'auto',
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
              paddingRight: 4,
            }}
          >
            {accordion}
            {thesisCards && (
              <div
                className="tb-chart-fs-thesis"
                data-testid="chart-fs-thesis"
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 8,
                }}
              >
                <div style={{
                  fontSize: 10,
                  color: 'var(--muted, #8593b0)',
                  textTransform: 'uppercase',
                  letterSpacing: 0.4,
                  fontWeight: 700,
                  padding: '4px 2px',
                  borderTop: '1px solid var(--border, #2a3349)',
                  marginTop: 4,
                }}>
                  Thesis context
                </div>
                {thesisCards}
              </div>
            )}
            <div style={{
              fontSize: 10, color: 'var(--muted, #8593b0)',
              textAlign: 'right',
              paddingTop: 4,
            }}>
              Esc · F to exit
            </div>
          </aside>
        </div>
      ) : (
        // Legacy single-column: no overlayGroups + no thesisCards → keep
        // existing behavior so other callers (TheoryStudio etc) don't break.
        <div
          className="tb-chart-fs-body"
          data-testid="chart-fs-body"
          style={{
            flex: fullscreen ? 1 : 'unset',
            minHeight: 0,
            ...chartPaneStyle,
          }}
        >
          {children}
        </div>
      )}

      {fullscreen && !useTwoColumn && (
        <div
          style={{
            fontSize: 10, color: 'var(--muted)', marginTop: 6,
            textAlign: 'right',
          }}
        >
          Press <b>Esc</b> or <b>F</b> to exit fullscreen
        </div>
      )}
    </div>
  );
}
