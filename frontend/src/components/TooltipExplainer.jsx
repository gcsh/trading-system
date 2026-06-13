/**
 * Feature-Merge F5 — TooltipExplainer primitive.
 *
 * Reusable plain-English explainer with a small ⓘ icon. Hover (desktop) or
 * tap (touch) → popup with `term` headline + plain-English `explanation`.
 *
 * Why a custom primitive: the operator is markets-beginner. The original
 * site has no shared tooltip component for jargon (Brier, ECE, Wilson CI,
 * Spearman, composite quality, calibration). Wrapping every technical
 * label with <TooltipExplainer term="…" explanation="…"> keeps the
 * vocabulary one-translation-from-English, on every page going forward.
 *
 * Styling matches the ORIGINAL site (var(--…) tokens, .panel feel) —
 * NO imports from src/design/* or src/v2/*.
 *
 * Props:
 *   term        — short label of the technical concept
 *   explanation — 1-3 sentence plain-English description
 *   children    — optional element wrapped before the ⓘ; if absent we
 *                 render just the icon
 *   iconSize    — px; default 13
 *   inline      — render inline-flex (default) vs block
 */
import React, { useEffect, useRef, useState } from 'react';

const POPUP_W = 280;

export default function TooltipExplainer({
  term,
  explanation,
  children,
  iconSize = 13,
  inline = true,
}) {
  // Two-source open state: `hover` is hover/focus driven, `pinned` is
  // click-locked. The popup is visible if EITHER is set. Click toggles
  // pinned and pin-open overrides hover-out so the test (and touch
  // users) can read the tooltip without it disappearing on mouse leave.
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const ref = useRef(null);
  const open = hover || pinned;

  // Click-outside closes the pinned popup on touch devices where hover
  // is sticky.
  useEffect(() => {
    if (!pinned) return undefined;
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        setPinned(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('touchstart', onDoc);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('touchstart', onDoc);
    };
  }, [pinned]);

  return (
    <span
      ref={ref}
      data-testid="tooltip-explainer"
      data-term={term}
      data-open={open ? '1' : '0'}
      style={{
        display: inline ? 'inline-flex' : 'flex',
        alignItems: 'center',
        gap: 4,
        position: 'relative',
        verticalAlign: 'baseline',
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {children}
      <button
        type="button"
        aria-label={`What is ${term}?`}
        onClick={(e) => {
          e.stopPropagation();
          setPinned((v) => !v);
        }}
        onFocus={() => setHover(true)}
        onBlur={() => setHover(false)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: iconSize + 4,
          height: iconSize + 4,
          borderRadius: '50%',
          border: '1px solid var(--border-strong)',
          background: 'transparent',
          color: 'var(--muted)',
          fontSize: Math.max(9, iconSize - 3),
          fontWeight: 700,
          cursor: 'help',
          padding: 0,
          lineHeight: 1,
        }}
      >
        i
      </button>
      {open && (
        <span
          role="tooltip"
          data-testid="tooltip-popup"
          style={{
            position: 'absolute',
            zIndex: 100,
            top: '100%',
            left: 0,
            marginTop: 6,
            width: POPUP_W,
            maxWidth: '85vw',
            padding: '10px 12px',
            background: 'var(--bg-elev)',
            border: '1px solid var(--border-strong)',
            borderRadius: 'var(--radius-sm, 8px)',
            boxShadow: 'var(--shadow-md, 0 8px 24px rgba(0,0,0,0.45))',
            fontSize: 12,
            lineHeight: 1.45,
            color: 'var(--text-soft)',
            textAlign: 'left',
            whiteSpace: 'normal',
            pointerEvents: 'none',
          }}
        >
          <div style={{
            color: 'var(--text)',
            fontWeight: 700,
            marginBottom: 4,
            fontSize: 12.5,
          }}>
            {term}
          </div>
          <div>{explanation}</div>
        </span>
      )}
    </span>
  );
}
