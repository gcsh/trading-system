/* MITS Phase 19 Stream 0 — V2 layout shell.
 *
 * CSS Grid:
 *   [topbar 64px]
 *   [sidebar 240px (or 64px collapsed)] [main]
 *   [footer 32px]
 *
 * Mounts under /v2/*. Sibling routes are filled by Stream 1/2/3.
 * Original /v1/* + / routes keep using the legacy Layout.jsx as
 * fallback so no bookmark breaks.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, Link } from 'react-router-dom';
import { BotHealthChip } from '../design/Components.jsx';
import './v2.css';

const NAV_GROUPS = [
  {
    label: 'Trading',
    items: [
      { to: '/v2/',            label: 'Mission Control', icon: '◉' },
      { to: '/v2/watchlist',   label: 'Watchlist',       icon: '◉' },
      { to: '/v2/activity',    label: 'Activity',        icon: '◉' },
    ],
  },
  {
    label: 'Analysis',
    items: [
      { to: '/v2/analysis',    label: 'Stock Detail',    icon: '◉' },
      { to: '/v2/gex',         label: 'GEX Dashboard',   icon: '◉' },
      { to: '/v2/flow',        label: 'Flow Intel',      icon: '◉' },
      { to: '/v2/theory',      label: 'Theory Studio',   icon: '◉' },
      { to: '/v2/knowledge',   label: 'Knowledge Graph', icon: '◉' },
    ],
  },
  {
    label: 'Decision',
    items: [
      { to: '/v2/decision/cockpit',   label: 'Cockpit',         icon: '◉' },
      { to: '/v2/decision/scorecard', label: 'Scorecard',       icon: '◉' },
      { to: '/v2/strategy',           label: 'Strategy Matrix', icon: '◉' },
      { to: '/v2/portfolio',          label: 'Portfolio',       icon: '◉' },
    ],
  },
  {
    label: 'Learning',
    items: [
      { to: '/v2/learning/funnel',  label: 'Funnel',            icon: '◉' },
      { to: '/v2/hypothesis-studio', label: 'Hypothesis Studio', icon: '◉' },
      { to: '/v2/detectors',         label: 'Detectors',         icon: '◉' },
      { to: '/v2/journal',           label: 'Trade Journal',     icon: '◉' },
    ],
  },
  {
    label: 'Settings',
    items: [
      { to: '/v2/settings/bot',   label: 'Bot Config',    icon: '◉' },
      { to: '/v2/settings/flags', label: 'Safety Flags',  icon: '◉' },
      { to: '/v2/diagnostics',    label: 'Diagnostics',   icon: '◉' },
    ],
  },
];

const TITLE_MAP = new Map();
for (const g of NAV_GROUPS) for (const i of g.items) TITLE_MAP.set(i.to, i.label);

/* ──────────────────────────────────────────────────────────────────── */
function Sidebar({ collapsed, onToggle }) {
  return (
    <aside className={`v2-sidebar ${collapsed ? 'v2-sidebar--collapsed' : ''}`}>
      <div className="v2-sidebar__head">
        <div className="v2-sidebar__brand">
          {!collapsed && <span className="v2-sidebar__brand-text">MITS</span>}
          <span className="v2-sidebar__brand-mark">▤</span>
        </div>
      </div>
      <nav className="v2-sidebar__nav">
        {NAV_GROUPS.map(group => (
          <div key={group.label} className="v2-sidebar__group">
            {!collapsed && (
              <div className="v2-sidebar__group-label">{group.label}</div>
            )}
            {group.items.map(item => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/v2/'}
                className={({ isActive }) =>
                  `v2-sidebar__link ${isActive ? 'v2-sidebar__link--active' : ''}`}
                title={collapsed ? item.label : undefined}
              >
                <span className="v2-sidebar__icon" aria-hidden="true">{item.icon}</span>
                {!collapsed && <span>{item.label}</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}

/* ──────────────────────────────────────────────────────────────────── */
function Topbar({ onHamburger, title, status }) {
  const [nyTime, setNyTime] = useState('');
  useEffect(() => {
    function tick() {
      try {
        const s = new Intl.DateTimeFormat('en-US', {
          timeZone: 'America/New_York',
          hour: '2-digit', minute: '2-digit', second: '2-digit',
          hour12: false,
        }).format(new Date());
        setNyTime(`${s} ET`);
      } catch (e) {
        setNyTime('');
      }
    }
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <header className="v2-topbar">
      <div className="v2-topbar__left">
        <button className="v2-topbar__hamburger"
                type="button"
                onClick={onHamburger}
                aria-label="Toggle sidebar">
          <span aria-hidden="true">≡</span>
        </button>
        <div className="v2-topbar__title">{title || 'MITS v2'}</div>
      </div>
      <div className="v2-topbar__center">
        <div className="v2-topbar__search">
          <span className="v2-topbar__search-icon">⌕</span>
          <input type="search"
                 placeholder="Search tickers, decisions, trades…"
                 className="v2-topbar__search-input"
                 disabled
                 aria-label="Global search (coming soon)" />
        </div>
      </div>
      <div className="v2-topbar__right">
        <BotHealthChip
          status={status?.status || 'running'}
          cycles={status?.cycles}
          lastCycleAt={status?.last_cycle_at}
        />
        <button className="v2-topbar__icon-btn" title="Notifications" disabled>🔔</button>
        <Link to="/v2/settings/bot" className="v2-topbar__icon-btn" title="Settings">⚙</Link>
        <span className="v2-topbar__time mono">{nyTime}</span>
      </div>
    </header>
  );
}

/* ──────────────────────────────────────────────────────────────────── */
function Footer({ build, env }) {
  return (
    <footer className="v2-footer">
      <span>Real money paper trading — every $ matters</span>
      <span className="v2-footer__build">
        build {build || 'dev'} · env {env || 'paper'}
      </span>
    </footer>
  );
}

/* ──────────────────────────────────────────────────────────────────── */
export default function V2Layout() {
  const [collapsed, setCollapsed] = useState(false);
  const [status, setStatus] = useState(null);
  const location = useLocation();

  // Page title derived from sidebar map (longest-match against pathname).
  const title = useMemo(() => {
    let best = 'MITS v2';
    let bestLen = 0;
    for (const [path, label] of TITLE_MAP) {
      if (location.pathname === path || location.pathname.startsWith(path + '/')) {
        if (path.length > bestLen) { best = label; bestLen = path.length; }
      }
    }
    return best;
  }, [location.pathname]);

  // Poll /bot/status — same surface as legacy Layout uses.
  useEffect(() => {
    let cancelled = false;
    async function fetchStatus() {
      try {
        const r = await fetch('/bot/status');
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          // Normalise to BotHealthChip's contract.
          const running = j.running || j.engine_running || j.status === 'running';
          setStatus({
            status: running ? 'running' : 'paused',
            cycles: j.cycles_completed || j.cycles || null,
            last_cycle_at: j.last_cycle_at || j.last_cycle || null,
          });
        }
      } catch (e) {
        if (!cancelled) setStatus(s => s || { status: 'error' });
      }
    }
    fetchStatus();
    const id = setInterval(fetchStatus, 15000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <div className={`v2-root v2-shell ${collapsed ? 'v2-shell--collapsed' : ''}`}>
      <Topbar
        title={title}
        status={status}
        onHamburger={() => setCollapsed(c => !c)}
      />
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed(c => !c)} />
      <main className="v2-main">
        <Outlet />
      </main>
      <Footer build={import.meta.env?.MODE || 'production'} env="paper" />
    </div>
  );
}
