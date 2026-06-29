/* MITS Phase 19 Cluster A — Activity Feed event card.
 *
 * One row in the vertical timeline. Sources two upstream shapes:
 *   - Alert  (from /alerts/list)
 *       { kind: 'alert', title, body, severity, category, ticker, timestamp, meta }
 *   - Signal (from /bot/status.recent_signals)
 *       { timestamp, ticker, action, confidence, reason, strategy, status,
 *         order_id, paper, quantity, price, instrument, ... }
 *
 * Renders a uniform UI either way, with a category icon, time-ago,
 * ticker badge, plain-English message, and an expand button that
 * reveals the full meta json on click. Card is clickable when a
 * decision_id or trade_id is present.
 */
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Pill } from '../../design/Components.jsx';

const CATEGORY_ICON = {
  signal:    '◆',
  order:     '▷',
  trade:     '▷',
  risk:      '⚠',
  system:    '⚙',
  ai:        '✦',
  engine:    '⟳',
  decision:  '✦',
  alert:     '!',
};
const SEVERITY_TONE = {
  info:     'info',
  success:  'success',
  warning:  'warning',
  danger:   'error',
  critical: 'error',
};

function ageString(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60)   return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function fmtTs(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('en-US', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
  } catch (e) { return iso; }
}

/**
 * Normalise either an Alert or a recent_signals row into a uniform
 * card shape so the renderer below stays small.
 */
export function normalizeEvent(raw) {
  if (!raw) return null;
  // Alert shape
  if (raw.kind === 'alert') {
    return {
      kind: 'alert',
      category: raw.category || 'system',
      severity: raw.severity || 'info',
      title:    raw.title || '(untitled)',
      body:     raw.body || '',
      ticker:   raw.ticker || null,
      timestamp: raw.timestamp,
      meta:     raw.meta || {},
      link:     null,
    };
  }
  // Signal shape
  const action = (raw.action || '').toUpperCase();
  const sev = raw.status === 'failed' ? 'danger'
            : raw.status === 'submitted' ? 'success'
            : 'info';
  const category = raw.status === 'submitted' || raw.status === 'failed'
    ? 'trade' : 'signal';
  const title = `${raw.ticker || '?'} · ${action || 'HOLD'}`;
  const body = raw.reason || raw.strategy || '';
  const link = raw.trade_id ? `/v2/decision/cockpit/${raw.trade_id}` : null;
  return {
    kind: 'signal',
    category,
    severity: sev,
    title,
    body,
    ticker: raw.ticker || null,
    timestamp: raw.timestamp,
    meta: raw,
    link,
  };
}

export default function ActivityEventCard({ event }) {
  const [expanded, setExpanded] = useState(false);
  if (!event) return null;
  const icon = CATEGORY_ICON[event.category] || '·';
  const tone = SEVERITY_TONE[event.severity] || 'neutral';
  const headerContent = (
    <div className="v2-act-card__head">
      <div className="v2-act-card__icon" aria-hidden="true">{icon}</div>
      <div className="v2-act-card__title">
        <div className="v2-act-card__title-line">
          <span className="v2-act-card__title-text">{event.title}</span>
          {event.ticker && (
            <Link
              to={`/v2/stock/${encodeURIComponent(event.ticker)}`}
              className="v2-act-card__ticker"
              onClick={(e) => e.stopPropagation()}
            >
              {event.ticker}
            </Link>
          )}
        </div>
        {event.body && (
          <div className="v2-act-card__body">{event.body}</div>
        )}
      </div>
      <div className="v2-act-card__meta">
        <Pill tone={tone} size="sm">{event.category}</Pill>
        <span className="v2-act-card__time mono" title={fmtTs(event.timestamp)}>
          {ageString(event.timestamp)}
        </span>
        <button
          type="button"
          className="v2-act-card__expand"
          onClick={(e) => { e.stopPropagation(); setExpanded(v => !v); }}
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? '▾' : '▸'}
        </button>
      </div>
    </div>
  );

  const card = (
    <div className={`v2-act-card v2-act-card--${tone}`}>
      {headerContent}
      {expanded && (
        <pre className="v2-act-card__json mono">
          {JSON.stringify(event.meta, null, 2)}
        </pre>
      )}
    </div>
  );

  if (event.link) {
    return (
      <Link to={event.link} style={{ textDecoration: 'none', color: 'inherit' }}>
        {card}
      </Link>
    );
  }
  return card;
}
