import React, { useEffect, useState } from 'react';

/**
 * Compact system-health pill that polls /audit/health. Green = all invariants
 * hold. Click → opens a panel listing the live violations so a non-technical
 * user can see exactly what's wrong (e.g. "trade #20 strike not snapped").
 *
 * Lives at the very top of the Cockpit so it's visible before scrolling.
 */
export default function AuditHealthBanner() {
  const [health, setHealth] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/audit/health')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (active && d) setHealth(d); })
      .catch(() => {});
    load();
    const id = setInterval(load, 30 * 1000);  // 30s
    return () => { active = false; clearInterval(id); };
  }, []);

  if (!health) return null;

  const reconcile = health.reconciliation?.violations?.length || 0;
  const expired = health.expired_options?.violations?.length || 0;
  const tradeBad = health.recent_trade_violations?.length || 0;
  const totalIssues = reconcile + expired + tradeBad;
  const ok = health.ok && totalIssues === 0;

  const tone = ok ? 'on' : (reconcile > 0 || expired > 0) ? 'danger' : 'warn';
  const tintBg = ok ? 'var(--accent-soft)' : tone === 'danger' ? 'var(--danger-soft)' : 'rgba(214,158,46,0.18)';
  const tintFg = ok ? 'var(--accent)' : tone === 'danger' ? 'var(--danger)' : 'var(--warn)';

  return (
    <div className="panel" style={{ padding: '10px 14px', background: 'var(--bg-elev)' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 18 }}>{ok ? '✅' : '⚠️'}</span>
        <div style={{ fontWeight: 700 }}>System integrity</div>
        <span className={`pill ${tone}`} style={{ background: tintBg, color: tintFg, border: `1px solid ${tintFg}` }}>
          {ok ? 'all clean' : `${totalIssues} issue${totalIssues === 1 ? '' : 's'}`}
        </span>
        {!ok && (
          <>
            {reconcile > 0 && <span style={{ fontSize: 12, color: 'var(--text-soft)' }}>· {reconcile} account drift</span>}
            {expired > 0 && <span style={{ fontSize: 12, color: 'var(--text-soft)' }}>· {expired} expired option still open</span>}
            {tradeBad > 0 && <span style={{ fontSize: 12, color: 'var(--text-soft)' }}>· {tradeBad} trade row issues</span>}
          </>
        )}
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>
          checked {new Date(health.checked_at + 'Z').toLocaleTimeString()}
        </span>
        <button className="btn small" onClick={() => setOpen((x) => !x)}>
          {open ? 'hide details' : 'show details'}
        </button>
      </div>
      {open && (
        <div style={{ marginTop: 10, padding: 10, background: 'var(--panel-2)', borderRadius: 8, fontSize: 12.5, lineHeight: 1.6 }}>
          <div><strong>Account:</strong> cash ${health.account.cash} · realized ${health.account.realized_pnl} · positions ${health.account.positions_market_value} · pv ${health.account.portfolio_value}</div>
          {reconcile > 0 && (
            <div style={{ marginTop: 6, color: 'var(--danger)' }}>
              <strong>Reconciliation drift:</strong>
              <ul style={{ margin: '4px 0 0 18px' }}>
                {health.reconciliation.violations.map((v, i) => <li key={i}>{v.message}</li>)}
              </ul>
            </div>
          )}
          {expired > 0 && (
            <div style={{ marginTop: 6, color: 'var(--danger)' }}>
              <strong>Expired options still open:</strong>
              <ul style={{ margin: '4px 0 0 18px' }}>
                {health.expired_options.violations.map((v, i) => <li key={i}>{v.message}</li>)}
              </ul>
            </div>
          )}
          {tradeBad > 0 && (
            <div style={{ marginTop: 6, color: 'var(--warn)' }}>
              <strong>Historical trade row issues</strong> (these were written before invariants existed; new trades are now blocked at write time):
              <ul style={{ margin: '4px 0 0 18px' }}>
                {health.recent_trade_violations.slice(0, 10).map((v, i) => (
                  <li key={i}>trade #{v.trade_id} {v.ticker}: {v.message}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
