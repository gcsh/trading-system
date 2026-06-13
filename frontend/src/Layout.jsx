import React, { useCallback, useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import AuthoritySpine from './components/AuthoritySpine.jsx';
import ErrorBoundary from './components/ErrorBoundary.jsx';
import ThemeToggle from './components/ThemeToggle.jsx';
import ChatWidget from './components/ChatWidget.jsx';
import SnapshotQualityChip from './components/SnapshotQualityChip.jsx';
import { money, num, pct } from './lib/format.js';

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

// Consolidated to 6 pages (2026-06-01). Old routes still resolve via
// redirects in main.jsx (e.g. /heatseeker → /intel?tab=gex), so any
// existing bookmark continues to work.
const NAV = [
  { to: '/', label: 'Today', icon: '🎯', exact: true },
  { to: '/trial-scorecard', label: 'Trial', icon: '🏁' },
  { to: '/lake', label: 'Lake', icon: '🌊' },
  { to: '/tomorrow', label: 'Tomorrow', icon: '🌅' },
  { to: '/trade-loop', label: 'Loop', icon: '🔁' },
  { to: '/analysis', label: 'Analysis', icon: '📈' },
  { to: '/trades', label: 'Trades', icon: '🧾' },
  { to: '/intel', label: 'Intel', icon: '🧠' },
  { to: '/knowledge', label: 'Knowledge', icon: '📚' },
  { to: '/detectors', label: 'Edge', icon: '📊' },
  { to: '/retrospective', label: 'Retro', icon: '📅' },
  { to: '/council', label: 'Council', icon: '🎓' },
  { to: '/lab', label: 'Lab', icon: '🧪' },
  // MITS Phase 18.E — operator console for 18.A-D learning surfaces.
  { to: '/hypothesis-studio', label: 'Studio', icon: '🔬' },
  { to: '/settings', label: 'Settings', icon: '⚙️' },
];

function HeartbeatBadge({ status }) {
  // Use ms since last_cycle_at. Server returns ISO timestamp.
  const lastIso = status?.last_cycle_at;
  if (!lastIso) {
    return null;
  }
  const lastMs = Date.parse(lastIso);
  if (Number.isNaN(lastMs)) return null;
  const ageSec = Math.max(0, (Date.now() - lastMs) / 1000);
  let color = 'var(--accent)';
  let label = `${Math.round(ageSec)}s`;
  if (ageSec > 600) {
    color = 'var(--danger)';
    label = `${Math.round(ageSec / 60)}m STALE`;
  } else if (ageSec > 90) {
    color = '#ffd84d';
    label = `${Math.round(ageSec)}s`;
  }
  return (
    <div title={`Engine last cycle ${ageSec.toFixed(0)}s ago`}
         style={{
           padding: '4px 10px', borderRadius: 14,
           background: `${color}22`, color,
           fontSize: 11, fontWeight: 700,
           letterSpacing: '0.04em',
         }}>
      ❤ {label}
    </div>
  );
}


function EquityReadout({ paperState, performance, equityChange }) {
  // Falls back gracefully if /paper/state isn't available (non-paper
  // brokers may not implement it).
  const equity = num(performance?.equity_end);
  const cash = paperState ? num(paperState.cash) : null;
  const positions = paperState ? num(paperState.open_positions) : null;
  const invested = (cash != null && equity != null)
    ? Math.max(0, equity - cash) : null;
  const pos = equityChange >= 0;
  const change = pct(equityChange, 2, { showSign: true });

  return (
    <div
      className="row"
      style={{ gap: 10, fontSize: 12 }}
      title={[
        `Equity: ${money(equity)}`,
        invested != null ? `Invested in ${positions} position${positions === 1 ? '' : 's'}: ${money(invested)}` : null,
        cash != null ? `Cash available: ${money(cash)}` : null,
        `Change since start: ${change}`,
      ].filter(Boolean).join('\n')}
    >
      <span style={{ color: 'var(--text-soft)' }}>
        <strong style={{ color: 'var(--text)', fontFeatureSettings: '"tnum"' }}>
          {money(equity)}
        </strong>
        <span style={{ color: pos ? 'var(--accent-2)' : 'var(--danger-2)', marginLeft: 4 }}>
          {change}
        </span>
      </span>
      {invested != null && invested > 0.01 && (
        <span className="pill purple" style={{ fontSize: 10.5 }}>
          {positions} pos · {money(invested)} in
        </span>
      )}
      {cash != null && cash > 0.01 && cash < (equity || Infinity) && (
        <span className="pill info" style={{ fontSize: 10.5 }}>
          {money(cash)} cash
        </span>
      )}
    </div>
  );
}

export default function Layout() {
  const [config, setConfig] = useState(null);
  const [status, setStatus] = useState({ running: false });
  const [performance, setPerformance] = useState(null);
  const [equity, setEquity] = useState([]);
  const [paperState, setPaperState] = useState(null);
  const location = useLocation();

  const refresh = useCallback(async () => {
    try {
      const [c, s, p, eq, ps] = await Promise.all([
        api('/config'),
        api('/bot/status'),
        api('/portfolio/performance'),
        api('/portfolio/equity?limit=240'),
        api('/paper/state').catch(() => null),
      ]);
      setConfig(c);
      setStatus(s);
      setPerformance(p);
      setEquity(eq);
      setPaperState(ps);
    } catch (e) {
      console.warn('refresh failed', e);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  const updateConfig = async (patch) => {
    const next = { ...config, ...patch };
    setConfig(next);
    try {
      await api('/config', { method: 'POST', body: JSON.stringify(next) });
    } catch (e) {
      console.warn('save failed', e);
    }
  };

  const toggleBot = async () => {
    const path = status.running ? '/bot/stop' : '/bot/start';
    await api(path, { method: 'POST' });
    await refresh();
  };

  const runOnce = async () => {
    await api('/bot/run-cycle', { method: 'POST' });
    await refresh();
  };

  if (!config) {
    return <div style={{ padding: 24 }}>Loading…</div>;
  }

  const broker = config.broker || 'local_paper';
  const isPaper = config.paper_mode || broker.includes('paper') || broker === 'local_paper';
  const auto = !!config.auto_execute;
  const equityChange = num(performance?.equity_change_pct);
  const ctx = { config, status, performance, equity, updateConfig, refresh };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="logo">T</div>
          <span>TradingBot</span>
        </div>

        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.exact}
            className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
          >
            <span className="icon">{item.icon}</span>
            <span>{item.label}</span>
          </NavLink>
        ))}

      </aside>

      <div>
        <header className="topbar">
          <div className="row" style={{ gap: 10, flex: 1, justifyContent: 'flex-end' }}>
            <ThemeToggle />
            {/* P4.3 — snapshot quality chip: surfaces data_quality +
                accounting_version of the most recent equity snapshot.
                Hidden when everything is clean (polite=false here so
                it always shows; flip polite=true to hide on "good"). */}
            <SnapshotQualityChip />
            {/* P4.2 — heartbeat badge: green if last cycle < 90s,
                yellow 90s-10min, red >10min. Catches silent-stop bugs
                where the engine wedges but the systemd unit stays up. */}
            <HeartbeatBadge status={status} />
            <div className={`status-badge ${status.running ? 'live' : 'stopped'}`}>
              <span className="dot pulse" style={{ width: 6, height: 6, borderRadius: '50%', background: 'currentColor', display: 'inline-block' }} />
              {status.running ? 'engine live' : 'engine stopped'}
            </div>
            {/* Rich equity readout — shows equity / invested / cash so
                positions are visually obvious even when after-hours
                marks match cost (the 2026-05-31 "$5,000 with 3 open
                positions" confusion). */}
            <EquityReadout
              paperState={paperState}
              performance={performance}
              equityChange={equityChange}
            />
            <button className="btn small" onClick={runOnce}>Run cycle</button>
            <button
              className={`btn ${status.running ? 'danger' : 'primary'}`}
              onClick={toggleBot}
            >
              {status.running ? 'Stop' : 'Start'}
            </button>
          </div>
        </header>

        <AuthoritySpine />

        <main className="main">
          <ErrorBoundary key={location.pathname}>
            <Outlet context={ctx} />
          </ErrorBoundary>
        </main>
      </div>
      <ChatWidget />
    </div>
  );
}
