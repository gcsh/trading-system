/* MITS Phase 19 Cluster A — Activity Feed v2 (/v2/activity).
 *
 * Live stream of bot events. Merges two upstream sources into a
 * unified, normalised timeline:
 *
 *   - /alerts/list?limit=N        (the persistent alert history)
 *   - /bot/status.recent_signals  (last ~20 engine signals/orders)
 *
 * Layout:
 *   ROW 0  KPI strip — events today / cycles / last cycle age / critical alerts
 *   ROW 1  Filter chips — All / Decisions / Trades / Engine / Errors / Cycles
 *   ROW 2  Vertical timeline (newest first)
 *   PAUSE/RESUME button — freezes the auto-poll without losing buffer
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import ActivityEventCard, { normalizeEvent } from '../components/ActivityEventCard.jsx';

const POLL_MS = 5_000;
const ALERT_LIMIT = 100;

const FILTER_CHIPS = [
  { key: 'all',       label: 'All',       icon: '◉' },
  { key: 'decision',  label: 'Decisions', icon: '✦' },
  { key: 'trade',     label: 'Trades',    icon: '▷' },
  { key: 'signal',    label: 'Signals',   icon: '◆' },
  { key: 'engine',    label: 'Engine',    icon: '⟳' },
  { key: 'risk',      label: 'Risk',      icon: '⚠' },
  { key: 'system',    label: 'System',    icon: '⚙' },
];

function fmtN(v) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toLocaleString();
}
function ageString(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60)   return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export default function ActivityFeed() {
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState(null);
  const [err, setErr] = useState(null);
  const [filter, setFilter] = useState('all');
  const [paused, setPaused] = useState(false);
  const [now, setNow] = useState(Date.now()); // for KPI re-render

  // Ticker for KPI age display so "Xs ago" stays current.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (paused) return undefined;
    let cancelled = false;
    async function load() {
      try {
        const [aRes, sRes] = await Promise.all([
          fetch(`/alerts/list?limit=${ALERT_LIMIT}`),
          fetch('/bot/status'),
        ]);
        if (cancelled) return;
        const alerts = aRes.ok ? await aRes.json() : [];
        const st = sRes.ok ? await sRes.json() : null;
        // Merge: normalise both shapes, then sort by timestamp desc.
        const norms = [
          ...(Array.isArray(alerts) ? alerts : []).map(normalizeEvent),
          ...((st?.recent_signals) || []).map(normalizeEvent),
        ].filter(Boolean);
        // De-dupe: alerts and signals can overlap on order events. Key
        // by `(timestamp + ticker + category + title)` to suppress
        // exact duplicates.
        const seen = new Set();
        const dedup = [];
        for (const e of norms) {
          const k = `${e.timestamp}|${e.ticker || ''}|${e.category}|${e.title}`;
          if (seen.has(k)) continue;
          seen.add(k);
          dedup.push(e);
        }
        dedup.sort((a, b) => {
          const ta = Date.parse(a.timestamp || '') || 0;
          const tb = Date.parse(b.timestamp || '') || 0;
          return tb - ta;
        });
        setEvents(dedup);
        setStatus(st);
        setErr(null);
      } catch (e) {
        if (!cancelled) setErr(e.message);
      }
    }
    load();
    const id = setInterval(load, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [paused]);

  // Filter chips
  const filtered = useMemo(() => {
    if (filter === 'all') return events;
    if (filter === 'decision') {
      return events.filter(e => ['signal', 'decision', 'ai'].includes(e.category));
    }
    if (filter === 'engine') {
      return events.filter(e => ['engine', 'system'].includes(e.category));
    }
    return events.filter(e => e.category === filter);
  }, [events, filter]);

  // KPIs
  const todayStart = useMemo(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  }, [now]);
  const eventsToday = events.filter(
    e => (Date.parse(e.timestamp || '') || 0) >= todayStart
  ).length;
  const criticalToday = events.filter(
    e => ['critical', 'danger'].includes(e.severity)
      && (Date.parse(e.timestamp || '') || 0) >= todayStart
  ).length;
  const cycles = status?.cycles ?? null;
  const lastCycle = status?.last_cycle_at || null;

  return (
    <div className="v2-root v2-act-page">
      <Section
        title="Activity Feed"
        subtitle="Live bot heartbeat — signals, trades, engine + risk alerts"
        actions={
          <button type="button"
                  className={`v2-act-pause v2-act-pause--${paused ? 'paused' : 'live'}`}
                  onClick={() => setPaused(p => !p)}>
            {paused ? '▶ Resume' : '⏸ Pause'}
          </button>
        }
      >
        {/* KPI strip */}
        <div className="v2-act-kpi">
          <Card><Stat label="Events Today" value={fmtN(eventsToday)}
            hint="All categories combined since midnight"
          /></Card>
          <Card><Stat label="Engine Cycles" value={fmtN(cycles)}
            hint="Total cycles since engine start"
          /></Card>
          <Card><Stat label="Last Cycle" value={ageString(lastCycle)}
            deltaPositive={(() => {
              const t = Date.parse(lastCycle || '');
              if (!t) return false;
              return (Date.now() - t) / 1000 < 120;
            })()}
            delta={(() => {
              const t = Date.parse(lastCycle || '');
              if (!t) return 'unknown';
              const sec = (Date.now() - t) / 1000;
              return sec < 60 ? 'live' : sec < 300 ? 'recent' : 'stale';
            })()}
            hint="Time since last engine cycle"
          /></Card>
          <Card><Stat label="Critical Today" value={fmtN(criticalToday)}
            deltaPositive={criticalToday === 0}
            delta={criticalToday === 0 ? 'clean' : 'review'}
            hint="Critical / danger severity alerts since midnight"
          /></Card>
        </div>

        {/* Filter chips */}
        <Card variant="default" style={{ marginTop: 16 }}>
          <div className="v2-act-chips">
            {FILTER_CHIPS.map(c => (
              <button
                key={c.key}
                type="button"
                className={`v2-act-chip ${filter === c.key ? 'v2-act-chip--active' : ''}`}
                onClick={() => setFilter(c.key)}
              >
                <span className="v2-act-chip__icon">{c.icon}</span>
                <span>{c.label}</span>
                {c.key === 'all' && (
                  <Pill tone="neutral" size="sm">{events.length}</Pill>
                )}
              </button>
            ))}
          </div>
        </Card>

        {err && (
          <Card variant="outlined" style={{ marginTop: 16,
            borderColor: 'var(--accent-red-dim)', color: 'var(--accent-red)' }}>
            Error loading: {err}
          </Card>
        )}

        {/* Timeline */}
        <div className="v2-act-timeline" style={{ marginTop: 16 }}>
          {filtered.length === 0 && (
            <Card>
              <EmptyState
                icon="⟳"
                message={paused
                  ? 'Feed paused. Press Resume to continue streaming.'
                  : 'No events match this filter yet.'}
              />
            </Card>
          )}
          {filtered.map((e, i) => (
            <ActivityEventCard
              key={`${e.timestamp}-${e.title}-${i}`}
              event={e}
            />
          ))}
        </div>
      </Section>
    </div>
  );
}
