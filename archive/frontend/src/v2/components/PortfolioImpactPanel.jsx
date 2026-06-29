/* MITS Phase 19 Stream 3 — PortfolioImpactPanel.
 *
 * Renders `portfolio_impact`:
 *   { portfolio_context: {equity, by_sector, pairwise_correlation, ...},
 *     correlation_cap: {blocked, worst_peer, worst_rho, sizing_multiplier} }
 *
 * Highlights: correlation cap status, sector concentration, stress P&L.
 */
import React from 'react';
import { Card, EmptyState, Pill, Stat } from '../../design/Components.jsx';
import { PanelHead, Footer } from './PolicyResultPanel.jsx';

function fmtMoney(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}
function fmtPct(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}

export default function PortfolioImpactPanel({ impact }) {
  if (!impact) {
    return (
      <Card>
        <PanelHead title="Portfolio impact" subtitle="correlation + concentration" />
        <EmptyState message="No portfolio impact for this decision." />
      </Card>
    );
  }
  const ctx = impact.portfolio_context || {};
  const cap = impact.correlation_cap   || {};
  const bySector = ctx.by_sector || {};
  const sectorEntries = Object.entries(bySector).sort(([, a], [, b]) => Number(b) - Number(a)).slice(0, 6);

  const capBlocked = !!cap.blocked || !!cap.hard_block;
  const capTone = capBlocked ? 'error'
                : Number(cap.sizing_multiplier) < 1 ? 'warning'
                :                                     'success';

  return (
    <Card>
      <PanelHead
        title="Portfolio impact"
        subtitle="correlation + concentration"
        right={
          <Pill tone={capTone}>
            {capBlocked ? 'cap blocked' : Number(cap.sizing_multiplier) < 1 ? 'cap throttled' : 'cap ok'}
          </Pill>
        }
      />

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
        gap: 6, marginBottom: 10,
        padding: 8, background: 'var(--bg-tertiary)', borderRadius: 4,
      }}>
        <Stat label="Equity" value={fmtMoney(ctx.equity)} mono />
        <Stat label="Long $" value={fmtMoney(ctx.net_long_notional)} mono />
        <Stat label="Leverage" value={ctx.leverage != null ? Number(ctx.leverage).toFixed(2) + '×' : '—'} mono />
        <Stat label="Worst ρ"
              value={cap.worst_rho != null ? Number(cap.worst_rho).toFixed(2) : '—'}
              delta={cap.worst_peer ? `vs ${cap.worst_peer}` : ''}
              mono />
      </div>

      {sectorEntries.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <div style={{
            fontSize: 11, color: 'var(--accent-cyan)',
            textTransform: 'uppercase', letterSpacing: '0.06em',
            marginBottom: 4,
          }}>Sector concentration (top 6)</div>
          {sectorEntries.map(([sector, pct]) => {
            const p = Math.max(0, Math.min(100, Number(pct) * 100));
            return (
              <div key={sector} style={{
                display: 'flex', alignItems: 'center',
                gap: 8, fontSize: 11, marginBottom: 2,
              }}>
                <span style={{
                  width: 70, color: 'var(--text-secondary)',
                }}>{sector}</span>
                <div style={{
                  flex: 1, height: 4, background: 'var(--bg-tertiary)',
                  borderRadius: 2, overflow: 'hidden',
                }}>
                  <div style={{
                    width: `${p}%`, height: '100%',
                    background: p > 30 ? 'var(--accent-yellow)' : 'var(--accent-cyan)',
                  }} />
                </div>
                <span className="mono" style={{
                  width: 40, textAlign: 'right',
                  color: 'var(--text-secondary)',
                }}>{p.toFixed(1)}%</span>
              </div>
            );
          })}
        </div>
      )}

      {capBlocked && cap.worst_peer && (
        <div style={{
          padding: 6, marginBottom: 8,
          background: 'rgba(255, 51, 85, 0.08)',
          border: '1px solid var(--accent-red)',
          borderRadius: 4, fontSize: 11,
          color: 'var(--accent-red)',
        }}>
          <strong>Cap blocked:</strong> would push correlation with{' '}
          <span className="mono">{cap.worst_peer}</span> to{' '}
          ρ={Number(cap.worst_rho).toFixed(2)}.
        </div>
      )}

      <Footer>
        <span title="If SPY fell 3% right now, this would be the open-portfolio P&L impact.">
          Stress: SPY −3%
        </span>
        <span className="mono" style={{
          color: Number(ctx.stress_spy_down_3pct_pnl) >= 0
            ? 'var(--accent-green)' : 'var(--accent-red)',
        }}>
          {fmtMoney(ctx.stress_spy_down_3pct_pnl)}
          {ctx.stress_spy_down_3pct_pct != null && (
            <span style={{ marginLeft: 4, color: 'var(--text-tertiary)' }}>
              ({fmtPct(ctx.stress_spy_down_3pct_pct)})
            </span>
          )}
        </span>
      </Footer>
    </Card>
  );
}
