/**
 * CohortMatrix — P2.4 posterior win-rate heatmap.
 *
 * Reads /cohorts/matrix and renders the (strategy × regime) heatmap.
 * Each cell shows:
 *   - observed win-rate (sized by closed count)
 *   - posterior_win_rate (Bayesian-blended with research prior)
 *   - lift vs baseline
 * Click a cell to see the prior breakdown (prior_n, prior_wr, source).
 */
import React, { useEffect, useMemo, useState } from 'react';

function fmtPct(v, fallback = '—') {
  if (v == null || isNaN(v)) return fallback;
  return `${(v * 100).toFixed(1)}%`;
}
function fmtNum(v, digits = 2, fallback = '—') {
  if (v == null || isNaN(v)) return fallback;
  return Number(v).toFixed(digits);
}

// Heat color: red below 0.40, yellow ~0.50, green above 0.60.
function heatColor(wr) {
  if (wr == null) return 'var(--panel-2)';
  if (wr >= 0.65) return 'rgba(155, 230, 90, 0.42)';
  if (wr >= 0.55) return 'rgba(255, 216, 77, 0.36)';
  if (wr >= 0.45) return 'rgba(255, 148, 77, 0.32)';
  return 'rgba(255, 93, 93, 0.32)';
}


function CellDetail({ cell, baseline }) {
  if (!cell) {
    return (
      <div className="empty" style={{ padding: 16, fontSize: 12 }}>
        Click a cell to see prior + observation breakdown.
      </div>
    );
  }
  return (
    <div className="panel" style={{ padding: 16 }}>
      <div style={{ fontSize: 14, fontWeight: 700 }}>
        {cell.strategy}
        <span style={{ color: 'var(--muted)', fontWeight: 400 }}> · {cell.regime}</span>
        {cell.grade && cell.grade !== '—' && (
          <span style={{ color: 'var(--muted)', fontWeight: 400 }}> · grade {cell.grade}</span>
        )}
      </div>
      <div style={{
        marginTop: 12, display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 8,
      }}>
        <Stat label="Posterior WR" value={fmtPct(cell.posterior_win_rate)}
              hint="Bayesian blend of prior + observations" highlight />
        <Stat label="Observed WR" value={fmtPct(cell.win_rate)}
              hint={`${cell.closed} closed (W${cell.wins}/L${cell.losses})`} />
        <Stat label="Prior WR" value={fmtPct(cell.prior?.prior_win_rate)}
              hint={cell.prior?.prior_n ? `n_eff = ${cell.prior.prior_n}` : 'no encoded prior'} />
        <Stat label="Lift vs baseline" value={cell.lift != null ? `${cell.lift}×` : '—'}
              hint={`baseline ${fmtPct(baseline?.win_rate)}`} />
        <Stat label="Expectancy" value={fmtNum(cell.expectancy, 2)} hint="avg P&L per closed trade" />
        <Stat label="Profit factor" value={cell.profit_factor === 'inf' ? '∞' : fmtNum(cell.profit_factor, 2)} />
      </div>
      {cell.prior?.source && (
        <div style={{
          marginTop: 12, padding: '8px 10px',
          background: 'var(--panel-2)', borderRadius: 6, fontSize: 11,
          color: 'var(--text-soft)',
        }}>
          <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
            <span style={{ color: 'var(--muted)', textTransform: 'uppercase',
                              fontSize: 9, letterSpacing: '0.06em', fontWeight: 600 }}>
              Prior source
            </span>
            <span style={{
              fontSize: 10, padding: '1px 6px', borderRadius: 8,
              background: cell.prior.source === 'curated_research'
                ? 'rgba(155,230,90,0.18)' : 'var(--panel)',
              color: cell.prior.source === 'curated_research'
                ? 'var(--accent)' : 'var(--muted)',
              fontWeight: 600,
            }}>
              {cell.prior.source === 'curated_research' ? 'curated research' : 'fallback baseline'}
            </span>
          </div>
          {cell.prior.citation && (
            <div style={{ marginTop: 4, color: 'var(--text-soft)' }}>{cell.prior.citation}</div>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, hint, highlight }) {
  return (
    <div style={{
      padding: '8px 10px',
      background: highlight ? 'var(--accent-soft, rgba(155,230,90,0.12))' : 'var(--panel-2)',
      borderRadius: 6,
      border: highlight ? '1px solid var(--accent)' : '1px solid var(--border)',
    }}>
      <div style={{ fontSize: 9, color: 'var(--muted)', textTransform: 'uppercase',
                       letterSpacing: '0.05em', fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, marginTop: 2,
                       fontFeatureSettings: '"tnum"' }}>{value}</div>
      {hint && <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

export default function CohortMatrix() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);
  const [showPosterior, setShowPosterior] = useState(true);

  useEffect(() => {
    fetch('/cohorts/matrix?limit=10000&min_cohort_closed=1')
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, []);

  const { strategies, regimes, byKey } = useMemo(() => {
    if (!data?.cells) return { strategies: [], regimes: [], byKey: {} };
    // Roll up to (strategy, regime); if a cell has multiple grade entries
    // we take the highest-closed one for display (the prior blend is keyed
    // on (strategy, regime) anyway).
    const map = {};
    for (const c of data.cells) {
      const key = `${c.strategy}::${c.regime}`;
      const prev = map[key];
      if (!prev || (c.closed || 0) > (prev.closed || 0)) {
        map[key] = c;
      }
    }
    const strats = Array.from(new Set(Object.values(map).map((c) => c.strategy))).sort();
    const regs = Array.from(new Set(Object.values(map).map((c) => c.regime))).sort();
    return { strategies: strats, regimes: regs, byKey: map };
  }, [data]);

  if (err) return <div className="empty">cohort matrix error: {err}</div>;
  if (!data) return <div className="empty">Loading cohort matrix…</div>;
  if (!data.cells?.length) {
    return <div className="empty">No cohort cells yet — need closed trades or synthetic backfill.</div>;
  }

  const selected = selectedKey ? byKey[selectedKey] : null;

  return (
    <div>
      <div className="row" style={{ alignItems: 'center', marginBottom: 12, gap: 12 }}>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          <strong>{data.totals?.n_closed || 0}</strong> closed labels ·
          <strong> {data.totals?.n_cohorts || 0}</strong> cohorts ·
          baseline WR <strong>{fmtPct(data.baseline?.win_rate)}</strong>
        </div>
        <div style={{ marginLeft: 'auto', fontSize: 11 }}>
          <label style={{ cursor: 'pointer', color: 'var(--text-soft)' }}>
            <input type="checkbox" checked={showPosterior}
                   onChange={(e) => setShowPosterior(e.target.checked)} />
            <span style={{ marginLeft: 4 }}>show posterior (Bayesian blend)</span>
          </label>
        </div>
      </div>

      <div style={{ overflow: 'auto', border: '1px solid var(--border)', borderRadius: 8 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse',
                            fontSize: 12, fontFeatureSettings: '"tnum"' }}>
          <thead>
            <tr>
              <th style={{ padding: '6px 10px', textAlign: 'left', fontSize: 10,
                              color: 'var(--muted)', textTransform: 'uppercase',
                              letterSpacing: '0.05em', position: 'sticky',
                              top: 0, background: 'var(--panel)' }}>
                Strategy ↓ / Regime →
              </th>
              {regimes.map((r) => (
                <th key={r} style={{ padding: '6px 10px', textAlign: 'center', fontSize: 10,
                                          color: 'var(--muted)', textTransform: 'uppercase',
                                          letterSpacing: '0.05em', position: 'sticky',
                                          top: 0, background: 'var(--panel)' }}>{r}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {strategies.map((s) => (
              <tr key={s} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '6px 10px', fontWeight: 600, whiteSpace: 'nowrap' }}>
                  {s}
                </td>
                {regimes.map((r) => {
                  const key = `${s}::${r}`;
                  const cell = byKey[key];
                  if (!cell) return <td key={r} style={{ padding: '6px 10px' }}>—</td>;
                  const wr = showPosterior ? cell.posterior_win_rate : cell.win_rate;
                  const isSel = selectedKey === key;
                  return (
                    <td key={r}
                        onClick={() => setSelectedKey(isSel ? null : key)}
                        style={{
                          padding: '8px 10px', textAlign: 'center',
                          background: heatColor(wr),
                          cursor: 'pointer',
                          border: isSel ? '2px solid var(--accent)' : '1px solid transparent',
                        }}>
                      <div style={{ fontWeight: 700 }}>{fmtPct(wr)}</div>
                      <div style={{ fontSize: 10, color: 'var(--muted)' }}>
                        n={cell.closed}
                        {cell.lift != null && cell.lift !== 1 && (
                          <span style={{
                            marginLeft: 4,
                            color: cell.lift > 1 ? 'var(--accent)' : 'var(--danger)',
                          }}>
                            {cell.lift > 1 ? '+' : ''}{((cell.lift - 1) * 100).toFixed(0)}%
                          </span>
                        )}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 16 }}>
        <CellDetail cell={selected} baseline={data.baseline} />
      </div>

      <div style={{ marginTop: 10, fontSize: 10, color: 'var(--muted-2)' }}>
        Heat = win rate · n = closed sample size · ±% = lift vs baseline.
        Posterior = (prior_n · prior_wr + n · obs_wr) / (prior_n + n). Toggle off to see raw observed WR.
      </div>
    </div>
  );
}
