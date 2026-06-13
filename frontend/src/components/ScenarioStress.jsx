/**
 * Stage-11.6 Scenario Stress widget — portfolio-level what-if buttons.
 *
 * Five preset macro shocks (mild risk-off, severe risk-off, risk-on, rates
 * shock, VIX spike, flash crash). Click any to project current open positions
 * under that shock and see per-position + portfolio total P&L impact.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const PRESET_BUTTON_CLASS = {
  mild_risk_off: 'btn',
  severe_risk_off: 'btn danger',
  risk_on: 'btn primary',
  rates_shock: 'btn',
  vix_spike: 'btn',
  flash_crash: 'btn danger',
};

function money(n) {
  if (n == null || isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

function pct(x, digits = 2) {
  if (x == null || isNaN(x)) return '—';
  return `${(x * 100).toFixed(digits)}%`;
}

export default function ScenarioStress() {
  const [presets, setPresets] = useState([]);
  const [result, setResult] = useState(null);
  const [active, setActive] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchJson('/scenarios/presets')
      .then((b) => setPresets(b.presets || []))
      .catch((e) => setError(e.message));
  }, []);

  const runPreset = async (name) => {
    setLoading(true);
    setActive(name);
    setError(null);
    try {
      const r = await fetchJson(`/scenarios/run/${name}`);
      setResult(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const delta = result?.total_pnl_delta ?? 0;
  const pctChange = result?.portfolio_pct_change ?? 0;
  const positions = result?.impacts || [];

  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>🔮 Portfolio Scenario Stress</h3>
        <span style={{ color: 'var(--muted)', fontSize: 12 }}>
          What if the macro tape shifted? — projects current open positions under each shock.
        </span>
      </div>

      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        {presets.map((p) => (
          <button
            key={p.name}
            className={`${PRESET_BUTTON_CLASS[p.name] || 'btn'} small`}
            onClick={() => runPreset(p.name)}
            disabled={loading}
            style={{ outline: active === p.name ? '2px solid var(--accent)' : 'none' }}
            title={p.label}
          >
            {p.label}
          </button>
        ))}
      </div>

      {error && (
        <div style={{ color: 'var(--danger)', marginBottom: 8 }}>{error}</div>
      )}

      {result && (
        <>
          <div className="row" style={{ gap: 16, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
            <div style={{
              fontSize: 22, fontWeight: 600,
              color: delta >= 0 ? 'var(--accent)' : 'var(--danger)',
            }}>
              {money(delta)} ({pct(pctChange)})
            </div>
            <span className="pill info">on {result.summary?.positions || 0} positions</span>
            <span className="pill purple">portfolio value ${result.total_market_value?.toFixed(2)}</span>
            {result.summary?.worst && (
              <span className="pill danger">
                worst: {result.summary.worst.ticker} {money(result.summary.worst.pnl_delta)}
              </span>
            )}
            {result.summary?.best && (
              <span className="pill on">
                best: {result.summary.best.ticker} {money(result.summary.best.pnl_delta)}
              </span>
            )}
          </div>
          {positions.length > 0 ? (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                  <th style={{ padding: 6 }}>Ticker</th>
                  <th style={{ padding: 6 }}>Instr.</th>
                  <th style={{ padding: 6, textAlign: 'right' }}>Value</th>
                  <th style={{ padding: 6, textAlign: 'right' }}>ΔP&L</th>
                  <th style={{ padding: 6, textAlign: 'right' }}>Δ%</th>
                  <th style={{ padding: 6 }}>Breakdown</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={`${p.ticker}-${i}`} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={{ padding: 6, fontWeight: 600 }}>{p.ticker}</td>
                    <td style={{ padding: 6 }}>{p.instrument} {p.side === 'SHORT' ? '(short)' : ''}</td>
                    <td style={{ padding: 6, textAlign: 'right' }}>${p.market_value?.toFixed(2)}</td>
                    <td style={{ padding: 6, textAlign: 'right',
                                    color: p.pnl_delta >= 0 ? 'var(--accent)' : 'var(--danger)',
                                    fontWeight: 600 }}>
                      {money(p.pnl_delta)}
                    </td>
                    <td style={{ padding: 6, textAlign: 'right', color: 'var(--muted)' }}>
                      {pct(p.pnl_delta_pct, 2)}
                    </td>
                    <td style={{ padding: 6, color: 'var(--muted)', fontSize: 12 }}>
                      {Object.entries(p.breakdown || {})
                        .filter(([, v]) => v !== 0)
                        .map(([k, v]) => `${k}: ${money(v)}`)
                        .join(' · ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div style={{ color: 'var(--muted)', fontStyle: 'italic' }}>
              No open positions to stress — paper account is flat.
            </div>
          )}
        </>
      )}
    </div>
  );
}
