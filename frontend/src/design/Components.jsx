/* MITS Phase 19 Stream 0 — atomic component library for /v2.
 *
 * 11 primitives. All scoped under the .v2-root class — they will not
 * leak into legacy pages (which keep their own .panel / .pill / .btn
 * styling from styles.css).
 *
 * Public API:
 *   Card, Stat, Pill, Sparkline, MiniHeatmap, KPIWidget,
 *   AlertBanner, BotHealthChip, Section, Table, EmptyState
 */
import React, { useState, useMemo } from 'react';
import './tokens.css';

/* ───────────────────────────────────────────────────────────────────
 * Card — base panel wrapper.
 *   variant: 'default' | 'elevated' | 'outlined'
 *   glow:    'none' | 'cyan' | 'green' | 'red' | 'purple'
 * ─────────────────────────────────────────────────────────────────── */
export function Card({
  variant = 'default',
  glow = 'none',
  className = '',
  style,
  children,
  ...rest
}) {
  const cls = [
    'v2-card',
    variant !== 'default' ? `v2-card--${variant}` : '',
    glow !== 'none' ? `v2-card--glow-${glow}` : '',
    className,
  ].filter(Boolean).join(' ');
  return (
    <div className={cls} style={style} {...rest}>
      {children}
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * Stat — labelled numeric KPI.
 * ─────────────────────────────────────────────────────────────────── */
export function Stat({ label, value, delta, deltaPositive, mono = false, hint }) {
  let deltaCls = 'v2-stat__delta--flat';
  if (deltaPositive === true) deltaCls = 'v2-stat__delta--pos';
  else if (deltaPositive === false) deltaCls = 'v2-stat__delta--neg';
  return (
    <div className="v2-stat" title={hint || undefined}>
      <div className="v2-stat__label">{label}</div>
      <div className={`v2-stat__value ${mono ? 'v2-stat__value--mono' : ''}`}>
        {value}
      </div>
      {delta != null && delta !== '' && (
        <div className={`v2-stat__delta ${deltaCls}`}>{delta}</div>
      )}
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * Pill — small status chip.
 *   tone: 'success' | 'warning' | 'error' | 'info' | 'neutral'
 *   size: 'sm' | 'md'
 * ─────────────────────────────────────────────────────────────────── */
export function Pill({ tone = 'neutral', size = 'sm', children, ...rest }) {
  const cls = [
    'v2-pill',
    `v2-pill--${tone}`,
    size === 'md' ? 'v2-pill--md' : '',
  ].filter(Boolean).join(' ');
  return <span className={cls} {...rest}>{children}</span>;
}

/* ───────────────────────────────────────────────────────────────────
 * Sparkline — tiny inline SVG. No chart library.
 * ─────────────────────────────────────────────────────────────────── */
export function Sparkline({
  data = [],
  color = 'var(--accent-cyan)',
  height = 40,
  width = 120,
  strokeWidth = 1.5,
  fill = true,
}) {
  if (!Array.isArray(data) || data.length < 2) {
    return (
      <svg width={width} height={height} aria-hidden="true">
        <line x1={0} y1={height / 2} x2={width} y2={height / 2}
              stroke="var(--border-default)" strokeDasharray="2 2" />
      </svg>
    );
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const stepX = width / (data.length - 1);
  const pts = data.map((v, i) => {
    const x = i * stepX;
    const y = height - ((v - min) / range) * (height - 4) - 2;
    return [x, y];
  });
  const linePath = pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const fillPath = `${linePath} L ${width} ${height} L 0 ${height} Z`;
  return (
    <svg width={width} height={height} aria-hidden="true">
      {fill && <path d={fillPath} fill={color} opacity={0.15} />}
      <path d={linePath} fill="none" stroke={color} strokeWidth={strokeWidth}
            strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * MiniHeatmap — matrix heatmap (e.g. GEX by expiration × strike).
 *   data:      2D array of numeric values
 *   rowLabels: labels for each row (left margin)
 *   colLabels: labels for each column (above the grid)
 *   minVal/maxVal: optional clamp. Otherwise computed from data.
 * ─────────────────────────────────────────────────────────────────── */
export function MiniHeatmap({
  data = [[]],
  rowLabels = [],
  colLabels = [],
  minVal,
  maxVal,
}) {
  const flat = data.flat().filter(v => typeof v === 'number' && !Number.isNaN(v));
  const lo = minVal != null ? minVal : (flat.length ? Math.min(...flat) : 0);
  const hi = maxVal != null ? maxVal : (flat.length ? Math.max(...flat) : 1);
  const span = hi - lo || 1;

  function cellColor(v) {
    if (typeof v !== 'number' || Number.isNaN(v)) return 'transparent';
    const t = (v - lo) / span;
    // Diverging palette: red for negative (below midpoint), green for positive.
    if (v < 0) {
      const intensity = Math.min(1, Math.abs(v) / Math.max(Math.abs(lo), 1e-9));
      return `rgba(255, 51, 85, ${0.15 + 0.6 * intensity})`;
    }
    const intensity = Math.min(1, t);
    return `rgba(0, 255, 136, ${0.15 + 0.6 * intensity})`;
  }

  return (
    <div className="v2-heatmap-wrap">
      {colLabels.length > 0 && (
        <div className="v2-heatmap__collabels">
          {colLabels.map((l, i) => <div key={i} className="v2-heatmap__collabel">{l}</div>)}
        </div>
      )}
      <div className="v2-heatmap">
        {data.map((row, ri) => (
          <div key={ri} className="v2-heatmap__row">
            {rowLabels[ri] != null && (
              <div className="v2-heatmap__rowlabel">{rowLabels[ri]}</div>
            )}
            {row.map((v, ci) => (
              <div key={ci}
                   className="v2-heatmap__cell"
                   style={{ background: cellColor(v) }}
                   title={typeof v === 'number' ? v.toFixed(2) : ''}>
                {typeof v === 'number' ? v.toFixed(0) : ''}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * KPIWidget — labelled value with icon + trend arrow.
 *   trend: 'up' | 'down' | 'flat'
 * ─────────────────────────────────────────────────────────────────── */
export function KPIWidget({ icon, label, value, trend = 'flat', trendText, hint }) {
  const arrow = trend === 'up' ? '▲' : trend === 'down' ? '▼' : '▬';
  return (
    <div className="v2-kpi" title={hint || undefined}>
      <div className="v2-kpi__head">
        {icon != null && <span aria-hidden="true">{icon}</span>}
        <span>{label}</span>
      </div>
      <div className="v2-kpi__value">{value}</div>
      <div className={`v2-kpi__trend v2-kpi__trend--${trend}`}>
        <span>{arrow}</span>
        {trendText && <span>{trendText}</span>}
      </div>
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * AlertBanner — top-of-page notice. Dismissible optional.
 * ─────────────────────────────────────────────────────────────────── */
export function AlertBanner({ severity = 'info', dismissible = false, children }) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  const icon = severity === 'critical' ? '⚠' : severity === 'warning' ? '⚡' : 'ℹ';
  return (
    <div className={`v2-alert v2-alert--${severity}`} role="alert">
      <span aria-hidden="true">{icon}</span>
      <span>{children}</span>
      {dismissible && (
        <button type="button"
                className="v2-alert__close"
                onClick={() => setDismissed(true)}
                aria-label="Dismiss">
          ✕
        </button>
      )}
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * BotHealthChip — engine running indicator for topbar.
 *   status: 'running' | 'paused' | 'error'
 * ─────────────────────────────────────────────────────────────────── */
export function BotHealthChip({ status = 'running', cycles, lastCycleAt }) {
  const cls = `v2-bothealth v2-bothealth--${status}`;
  const lastText = useMemo(() => {
    if (!lastCycleAt) return '';
    const ms = Date.parse(lastCycleAt);
    if (Number.isNaN(ms)) return '';
    const ageSec = Math.max(0, (Date.now() - ms) / 1000);
    if (ageSec < 90) return `${Math.round(ageSec)}s`;
    if (ageSec < 3600) return `${Math.round(ageSec / 60)}m`;
    return `${Math.round(ageSec / 3600)}h`;
  }, [lastCycleAt]);
  const label = status.toUpperCase();
  return (
    <span className={cls} title={`Engine ${label}${cycles != null ? ` · ${cycles} cycles` : ''}${lastText ? ` · last ${lastText} ago` : ''}`}>
      <span className="v2-bothealth__dot" />
      <span>{label}</span>
      {cycles != null && <span style={{ opacity: 0.7 }}>· {cycles}</span>}
      {lastText && <span style={{ opacity: 0.7 }}>· {lastText}</span>}
    </span>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * Section — titled block wrapper.
 * ─────────────────────────────────────────────────────────────────── */
export function Section({ title, subtitle, actions, children }) {
  return (
    <section className="v2-section">
      <header className="v2-section__header">
        {title && <h2 className="v2-section__title">{title}</h2>}
        {subtitle && <span className="v2-section__subtitle">{subtitle}</span>}
        {actions && <div className="v2-section__actions">{actions}</div>}
      </header>
      <div>{children}</div>
    </section>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * Table — basic dense data table.
 *   cols: [{ key, label, mono?, align? }, …]
 *   rows: [{ key/value pairs matching cols.key }, …]
 * ─────────────────────────────────────────────────────────────────── */
export function Table({ cols = [], rows = [], striped = false, sticky = false }) {
  const cls = [
    'v2-table',
    striped ? 'v2-table--striped' : '',
    sticky ? 'v2-table--sticky' : '',
  ].filter(Boolean).join(' ');
  return (
    <table className={cls}>
      <thead>
        <tr>
          {cols.map(c => (
            <th key={c.key}
                className={c.mono ? 'mono' : ''}
                style={{ textAlign: c.align || 'left' }}>
              {c.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr key={r.__key || ri}>
            {cols.map(c => (
              <td key={c.key}
                  className={c.mono ? 'mono' : ''}
                  style={{ textAlign: c.align || 'left' }}>
                {r[c.key]}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ───────────────────────────────────────────────────────────────────
 * EmptyState — used for insufficient-data sections.
 * ─────────────────────────────────────────────────────────────────── */
export function EmptyState({ icon = '∅', message = 'No data', action }) {
  return (
    <div className="v2-empty">
      <div className="v2-empty__icon">{icon}</div>
      <div className="v2-empty__message">{message}</div>
      {action}
    </div>
  );
}

export default {
  Card, Stat, Pill, Sparkline, MiniHeatmap, KPIWidget,
  AlertBanner, BotHealthChip, Section, Table, EmptyState,
};
