/**
 * Phase C.4 — Cmd-K command palette for the Analysis page.
 *
 * Single modal that exposes:
 *   • Toggle a chart overlay (any of the 23 theories — flips
 *     entries in/out of ``selectedTheories``)
 *   • Quick actions: clear all overlays, open fullscreen (TODO),
 *     etc — extensible via the ``actions`` prop.
 *
 * Why a single palette instead of three menus: operators stop
 * navigating menus when the keyboard is the fastest surface. The
 * palette filters across both "overlay theories" and "actions" so
 * one search reaches everything ("boll" finds Bollinger, "clear"
 * finds Clear all overlays).
 *
 * Open: Cmd/Ctrl-K (handled at parent so other pages can re-bind).
 * Close: Esc, click backdrop, or pick an entry.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { THEORY_CATALOG } from './theoryCatalog.js';

function score(haystack, needle) {
  if (!needle) return 1;
  const h = haystack.toLowerCase();
  const n = needle.toLowerCase();
  if (h.startsWith(n)) return 3;
  if (h.includes(n)) return 2;
  // Fuzzy: every letter of needle appears in haystack in order.
  let i = 0;
  for (const ch of h) {
    if (ch === n[i]) i += 1;
    if (i === n.length) return 1;
  }
  return 0;
}

export default function CommandPalette({
  open,
  onClose,
  selectedTheories = [],
  onToggleTheory,
  actions = [],
}) {
  const [q, setQ] = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) {
      setQ('');
      setCursor(0);
      // After mount, focus the input.
      const id = setTimeout(() => {
        if (inputRef.current) inputRef.current.focus();
      }, 0);
      return () => clearTimeout(id);
    }
    return undefined;
  }, [open]);

  const entries = useMemo(() => {
    const theoryEntries = THEORY_CATALOG.map((t) => ({
      key: `theory:${t.id}`,
      label: t.label,
      hint: selectedTheories.includes(t.id) ? 'on' : 'off',
      group: 'Chart overlay',
      color: t.color,
      onPick: () => onToggleTheory && onToggleTheory(t.id),
    }));
    const actionEntries = (actions || []).map((a, i) => ({
      key: `action:${a.id || i}`,
      label: a.label,
      hint: a.hint || '',
      group: 'Action',
      color: a.color || '#9aa5b2',
      onPick: () => a.onPick && a.onPick(),
    }));
    const all = [...theoryEntries, ...actionEntries];
    if (!q.trim()) return all;
    return all
      .map((e) => ({ e, s: Math.max(score(e.label, q), score(e.group, q) - 1) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .map((x) => x.e);
  }, [q, selectedTheories, actions, onToggleTheory]);

  useEffect(() => {
    if (cursor >= entries.length) setCursor(Math.max(0, entries.length - 1));
  }, [entries.length, cursor]);

  if (!open) return null;

  const onKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(entries.length - 1, c + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const entry = entries[cursor];
      if (entry) entry.onPick();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose && onClose();
    }
  };

  return (
    <div
      onClick={onClose}
      data-testid="analysis-cmdk"
      style={{
        position: 'fixed', inset: 0, zIndex: 1100,
        background: 'rgba(8, 12, 22, 0.55)',
        display: 'grid', placeItems: 'start center',
        padding: '12vh 16px 16px',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(560px, 96vw)',
          background: 'var(--panel, #0d111f)',
          border: '1px solid var(--border, #2a3349)',
          borderRadius: 12,
          boxShadow: '0 18px 48px rgba(0,0,0,0.45)',
          overflow: 'hidden',
        }}
      >
        <input
          ref={inputRef}
          type="text"
          value={q}
          onChange={(e) => { setQ(e.target.value); setCursor(0); }}
          onKeyDown={onKeyDown}
          placeholder="Search theories, indicators, actions…"
          style={{
            width: '100%',
            padding: '14px 16px',
            background: 'transparent',
            border: 'none',
            outline: 'none',
            color: 'var(--text-primary, #e6edf3)',
            fontSize: 14,
            borderBottom: '1px solid var(--border-subtle, #2a3349)',
          }}
        />
        <div
          role="listbox"
          style={{
            maxHeight: '50vh',
            overflowY: 'auto',
            padding: 6,
          }}
        >
          {entries.length === 0 ? (
            <div style={{
              padding: 16, fontSize: 12,
              color: 'var(--muted, #8593b0)',
              textAlign: 'center',
            }}>
              ∅ No matches.
            </div>
          ) : (
            entries.map((entry, i) => {
              const active = i === cursor;
              return (
                <button
                  key={entry.key}
                  type="button"
                  role="option"
                  aria-selected={active}
                  data-testid={`cmdk-row-${entry.key}`}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => entry.onPick()}
                  style={{
                    display: 'flex',
                    width: '100%',
                    alignItems: 'center',
                    gap: 10,
                    padding: '8px 10px',
                    background: active
                      ? 'rgba(95, 201, 206, 0.10)'
                      : 'transparent',
                    border: 'none',
                    borderRadius: 6,
                    cursor: 'pointer',
                    color: 'var(--text-primary, #e6edf3)',
                    fontSize: 13,
                    textAlign: 'left',
                  }}
                >
                  <span style={{
                    display: 'inline-block',
                    width: 8, height: 8, borderRadius: '50%',
                    background: entry.color,
                    flexShrink: 0,
                  }} />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span style={{
                      display: 'block',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}>{entry.label}</span>
                    <span style={{
                      display: 'block',
                      fontSize: 10,
                      color: 'var(--muted, #8593b0)',
                      textTransform: 'uppercase',
                      letterSpacing: 0.4,
                    }}>{entry.group}</span>
                  </span>
                  {entry.hint && (
                    <span style={{
                      fontSize: 10,
                      color: entry.hint === 'on'
                        ? 'var(--accent, #5fc9ce)'
                        : 'var(--muted, #8593b0)',
                      textTransform: 'uppercase',
                      letterSpacing: 0.4,
                      padding: '2px 6px',
                      borderRadius: 999,
                      border: '1px solid ' + (entry.hint === 'on'
                        ? 'var(--accent, #5fc9ce)' : 'var(--border-subtle, #2a3349)'),
                    }}>
                      {entry.hint}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>
        <div style={{
          padding: '6px 12px',
          fontSize: 10,
          color: 'var(--muted, #8593b0)',
          borderTop: '1px solid var(--border-subtle, #2a3349)',
          display: 'flex',
          gap: 12,
          justifyContent: 'flex-end',
        }}>
          <span>↑↓ navigate</span>
          <span>↵ pick</span>
          <span>Esc close</span>
        </div>
      </div>
    </div>
  );
}
