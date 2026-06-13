/**
 * CurrentlyHoldingStrip — shows the operator's open positions right now.
 *
 * Why it exists: the Decision Flow heatmap shows what fired IN THE
 * CURRENT WINDOW (last N evaluations). It does NOT show lifetime
 * holdings. The operator's intuition "0 executions but cash is $6 —
 * what's going on?" is answered here: positions taken in EARLIER
 * cycles are still on the book; the engine is correctly skipping
 * them now (already_held).
 *
 * Backend: /paper/positions (mark-to-market live).
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import EvidencePanel from './EvidencePanel.jsx';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}


// MITS-5 — per-position thesis-health chip + drill-down modal.
function ThesisHealthChip({ position }) {
  const [health, setHealth] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!position || !position.id) return;
    let cancelled = false;
    fetch(`/thesis/health/${position.id}`)
      .then((r) => r.ok ? r.json() : null)
      .then((body) => !cancelled && setHealth(body))
      .catch(() => !cancelled && setHealth(null));
    return () => { cancelled = true; };
  }, [position && position.id]);

  if (!health || health.abstain || health.score == null) {
    return null;
  }
  const score = Number(health.score);
  const klass = score >= 70 ? 'pill on'
              : score >= 40 ? 'pill info'
              : 'pill danger';
  return (
    <>
      <span
        className={klass}
        style={{ fontSize: 10, cursor: 'pointer', marginTop: 4 }}
        title={`Thesis health ${score.toFixed(0)} — click for breakdown`}
        onClick={() => setOpen(true)}
      >
        thesis {score.toFixed(0)}/100
      </span>
      {open && (
        <div
          onClick={() => setOpen(false)}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            zIndex: 1000,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="panel"
            style={{ maxWidth: 520, width: '90%', padding: 16 }}
          >
            <div className="row" style={{ justifyContent: 'space-between',
                                                    alignItems: 'baseline' }}>
              <h3 style={{ margin: 0 }}>
                Thesis health · {health.ticker}
              </h3>
              <button className="btn small" onClick={() => setOpen(false)}>
                Close
              </button>
            </div>
            <div style={{ marginTop: 8, fontSize: 12 }}>
              Pattern <strong>{health.pattern || '—'}</strong>
              {' · '}regime <strong>{health.regime || 'any'}</strong>
              {' · '}score <strong>{score.toFixed(0)}/100</strong>
            </div>
            <div style={{ marginTop: 6, fontSize: 12, color: 'var(--muted)' }}>
              Winner profile: N={health.winner_profile
                  && health.winner_profile.sample_size}, confidence{' '}
              {health.winner_profile
                && (health.winner_profile.confidence * 100).toFixed(0)}%
            </div>
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)',
                                    textTransform: 'uppercase' }}>
                Intact traits
              </div>
              {(health.intact_traits || []).length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--muted)' }}>none</div>
              )}
              {(health.intact_traits || []).map((t) => (
                <div key={t} style={{ fontSize: 12 }}>
                  <span className="pill on" style={{ fontSize: 10 }}>OK</span>{' '}
                  {t.replace(/_/g, ' ')}
                </div>
              ))}
            </div>
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)',
                                    textTransform: 'uppercase' }}>
                Degraded traits
              </div>
              {(health.degraded_traits || []).length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--muted)' }}>none</div>
              )}
              {(health.degraded_traits || []).map((t) => (
                <div key={t} style={{ fontSize: 12 }}>
                  <span className="pill danger" style={{ fontSize: 10 }}>X</span>{' '}
                  {t.replace(/_/g, ' ')}
                </div>
              ))}
            </div>
            <div style={{ marginTop: 12, fontSize: 12,
                                color: 'var(--muted)' }}>
              {health.reason}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function money(n, frac = 2) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD',
    minimumFractionDigits: frac, maximumFractionDigits: frac,
  }).format(Number(n));
}

// MITS Phase 14.B — pulls /portfolio/context and renders a thin chip strip
// (Net long $X · Lev Y× · SPY-3% → Z%) with the top-3 pairwise rhos on hover.
function PortfolioContextStrip({ pctx }) {
  if (!pctx) return null;
  const longUsd = Number(pctx.net_long_notional) || 0;
  const shortUsd = Number(pctx.net_short_notional) || 0;
  const lev = Number(pctx.leverage) || 0;
  const stressPct = Number(pctx.stress_spy_down_3pct_pct) || 0;
  const stressUsd = Number(pctx.stress_spy_down_3pct_pnl) || 0;

  // Top-3 pairwise rhos by absolute value across the matrix.
  const pairs = [];
  const seen = new Set();
  const matrix = pctx.pairwise_correlation || {};
  for (const a of Object.keys(matrix)) {
    for (const b of Object.keys(matrix[a] || {})) {
      const key = [a, b].sort().join('|');
      if (seen.has(key)) continue;
      seen.add(key);
      pairs.push({ a, b, rho: Number(matrix[a][b]) });
    }
  }
  pairs.sort((x, y) => Math.abs(y.rho) - Math.abs(x.rho));
  const top = pairs.slice(0, 3);
  const tooltip = top.length === 0
    ? 'No pairwise correlations available yet.'
    : 'Top pairwise correlations:\n' + top
        .map((p) => `  ${p.a}↔${p.b}: ${p.rho.toFixed(2)}`)
        .join('\n');

  return (
    <div
      className="row"
      style={{ gap: 8, fontSize: 11, color: 'var(--muted)',
               flexWrap: 'wrap' }}
      title={tooltip}
    >
      <span>
        Net long <strong className="accent-data">{money(longUsd, 0)}</strong>
      </span>
      {shortUsd > 0 && (
        <span>
          short <strong className="accent-bear">{money(shortUsd, 0)}</strong>
        </span>
      )}
      <span>
        Lev <strong>{lev.toFixed(2)}×</strong>
      </span>
      <span className={stressPct < 0 ? 'pill danger' : 'pill info'}
            style={{ fontSize: 10.5 }}>
        SPY-3% → {(stressPct * 100).toFixed(2)}% ({money(stressUsd, 0)})
      </span>
    </div>
  );
}


export default function CurrentlyHoldingStrip() {
  const [positions, setPositions] = useState([]);
  const [state, setState] = useState(null);
  const [pctx, setPctx] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [p, s, ctx] = await Promise.all([
        api('/paper/positions'),
        api('/paper/state'),
        api('/portfolio/context').catch(() => null),
      ]);
      setPositions(Array.isArray(p) ? p : []);
      setState(s);
      setPctx(ctx);
      setError(null);
    } catch (e) { setError(e.message); }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  // Empty state: no positions yet → don't render anything. The
  // Decision Flow heatmap below tells the rest of the story.
  if (!positions.length) return null;

  const totalInvested = positions.reduce(
    (sum, p) => sum + (Number(p.market_value) || 0), 0);
  const totalPnL = positions.reduce(
    (sum, p) => sum + (Number(p.unrealized_pnl) || 0), 0);
  const equity = state ? Number(state.portfolio_value) : null;
  const cash = state ? Number(state.cash) : null;

  return (
    <div className="panel panel--markets">
      <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>Currently holding</div>
          <h3 style={{ margin: '4px 0 0' }}>
            {positions.length} open position{positions.length === 1 ? '' : 's'}
            <span style={{ color: 'var(--muted)', fontSize: 13, marginLeft: 8 }}>
              · {money(totalInvested)} invested
            </span>
          </h3>
        </div>
        <div className="row" style={{ gap: 10, fontSize: 12, color: 'var(--muted)' }}>
          {cash != null && <span>Cash <strong className="accent-system">{money(cash)}</strong></span>}
          {equity != null && <span>Equity <strong className="accent-data">{money(equity)}</strong></span>}
          {Math.abs(totalPnL) > 0.01 && (
            <span className={totalPnL >= 0 ? 'pill on' : 'pill danger'}>
              {totalPnL >= 0 ? '+' : ''}{money(totalPnL)} unrealized
            </span>
          )}
        </div>
      </div>

      <PortfolioContextStrip pctx={pctx} />

      {error && (
        <div className="accent-bear" style={{ fontSize: 12, marginBottom: 8 }}>
          {error}
        </div>
      )}

      <div style={{
        display: 'grid', gap: 8,
        gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
      }}>
        {positions.map((p) => {
          const pnl = Number(p.unrealized_pnl) || 0;
          const pnlPct = Number(p.unrealized_pnl_pct) || 0;
          const cur = Number(p.current_price) || 0;
          const avg = Number(p.avg_cost) || 0;
          const positive = pnl >= 0;
          return (
            <div key={p.id} style={{
              padding: '10px 12px',
              background: 'var(--panel-2)',
              border: '1px solid var(--border)',
              borderLeft: `3px solid ${positive ? 'var(--accent)' : 'var(--danger)'}`,
              borderRadius: 8,
            }}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div className="row" style={{ gap: 6, alignItems: 'baseline' }}>
                  <strong>{p.ticker}</strong>
                  {p.entry_grade && (
                    <span
                      className="pill"
                      style={{
                        fontSize: 9.5, padding: '1px 5px',
                        background: 'var(--panel)',
                        color: 'var(--muted)',
                        border: '1px solid var(--border)',
                      }}
                      title={`Ranker grade at entry: ${p.entry_grade}`}
                    >
                      {p.entry_grade}
                    </span>
                  )}
                </div>
                <span className={positive ? 'pill on' : pnl === 0 ? 'pill info' : 'pill danger'}
                      style={{ fontSize: 10.5 }}>
                  {positive ? '+' : ''}{pnlPct.toFixed(2)}%
                </span>
              </div>
              <div style={{
                fontSize: 11.5, color: 'var(--muted)', marginTop: 4,
                fontFeatureSettings: '"tnum"',
              }}>
                {Number(p.quantity).toFixed(2)} @ {money(avg)} · now {money(cur)}
              </div>
              <div className="row" style={{
                justifyContent: 'space-between', marginTop: 4,
                fontSize: 11.5,
              }}>
                <span style={{ color: 'var(--muted)' }}>
                  {money(p.market_value)} mkt
                </span>
                <span className={positive ? 'accent-markets' : pnl === 0 ? 'accent-muted' : 'accent-bear'}>
                  {positive ? '+' : ''}{money(pnl)}
                </span>
              </div>
              {/* MITS-5 — thesis-health chip per position. Click to
                  see the intact / degraded winner-trait breakdown. */}
              <ThesisHealthChip position={p} />
              {/* MITS Phase 1 — corpus evidence for this ticker (top 3
                  patterns at the 1d horizon). Renders nothing if the
                  knowledge graph has no cells yet. */}
              <div style={{ marginTop: 8 }}>
                <EvidencePanel ticker={p.ticker} topN={3} horizon="1d" />
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 8, fontSize: 11, color: 'var(--muted)' }}>
        These positions were taken in earlier cycles. The Decision Flow heatmap below
        shows ⌂ cyan cells for tickers the engine is correctly skipping this cycle
        (already_held — no pyramiding).
      </div>
    </div>
  );
}
