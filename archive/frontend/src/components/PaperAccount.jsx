import React, { useEffect, useState } from 'react';

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export default function PaperAccount({ broker }) {
  const [state, setState] = useState(null);
  const [positions, setPositions] = useState([]);
  const [error, setError] = useState(null);
  const [resetAmount, setResetAmount] = useState(1000);

  const active = broker === 'local_paper';

  const refresh = async () => {
    if (!active) return;
    try {
      const [s, p] = await Promise.all([api('/paper/state'), api('/paper/positions')]);
      setState(s);
      setPositions(p);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    refresh();
    if (!active) return;
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [active]);

  const reset = async () => {
    if (!window.confirm(`Reset paper account to $${resetAmount}? All positions will be cleared.`)) return;
    await api('/paper/reset', { method: 'POST', body: JSON.stringify({ starting_cash: Number(resetAmount) }) });
    await refresh();
  };

  if (!active) {
    return (
      <div className="panel col-6">
        <h2>Paper account</h2>
        <div style={{ color: 'var(--muted)' }}>
          Switch broker to <code>Local Paper</code> to use the built-in simulator.
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="panel col-6">
        <h2>Paper account</h2>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }

  if (!state) return <div className="panel col-6"><h2>Paper account</h2>loading…</div>;

  const num = (v, fallback = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
  const equity = num(state.portfolio_value ?? state.last_portfolio_value);
  const startingCash = num(state.starting_cash, 1);
  const cash = num(state.cash);
  const realized = num(state.realized_pnl);
  const pnl = equity - startingCash;
  const pnlPct = startingCash ? (pnl / startingCash) * 100 : 0;
  const pnlColor = pnl >= 0 ? 'var(--accent)' : 'var(--danger)';

  return (
    <div className="panel col-6">
      <h2>Paper account</h2>
      <div className="row" style={{ gap: 16, marginBottom: 12 }}>
        <div>
          <div className="metric">${equity.toFixed(2)}</div>
          <div className="sub">equity</div>
        </div>
        <div>
          <div className="metric" style={{ color: pnlColor }}>
            {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} ({pnlPct.toFixed(1)}%)
          </div>
          <div className="sub">unrealized</div>
        </div>
        <div>
          <div className="metric">${cash.toFixed(2)}</div>
          <div className="sub">cash</div>
        </div>
        <div>
          <div className="metric">${realized.toFixed(2)}</div>
          <div className="sub">realized</div>
        </div>
      </div>

      {positions.length > 0 && (
        <table style={{ marginBottom: 12 }}>
          <thead>
            <tr><th>Ticker</th><th>Kind</th><th>Qty</th><th>Avg cost</th></tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.id}>
                <td>{p.ticker}</td>
                <td>{p.kind}</td>
                <td>{num(p.quantity).toFixed(2)}</td>
                <td>${num(p.avg_cost).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="row" style={{ gap: 8 }}>
        <input
          type="number"
          min="50"
          step="50"
          value={resetAmount}
          onChange={(e) => setResetAmount(e.target.value)}
          style={{ width: 120 }}
        />
        <button className="btn" onClick={reset}>Reset to ${resetAmount}</button>
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>
        Click <strong>Start bot</strong> in the header after switching brokers to apply the change.
      </div>
    </div>
  );
}
