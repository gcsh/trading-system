import React, { useEffect, useState } from 'react';
import { money, num, pct } from '../lib/format.js';

function Podium({ row, rank }) {
  if (!row) {
    return (
      <div className="panel-2" style={{ background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 8, padding: 12, minHeight: 90 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>#{rank}</div>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>—</div>
      </div>
    );
  }
  const medal = ['🥇', '🥈', '🥉'][rank - 1];
  const pnl = num(row.total_pnl);
  return (
    <div
      style={{
        background: pnl >= 0 ? 'var(--accent-soft)' : 'var(--danger-soft)',
        border: `1px solid ${pnl >= 0 ? '#c4e3d4' : '#f1c3ca'}`,
        borderRadius: 8,
        padding: 12,
      }}
    >
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {medal} #{rank} best
      </div>
      <div style={{ fontSize: 14, fontWeight: 600, textTransform: 'capitalize', marginTop: 4 }}>
        {(row.strategy || '').replace(/_/g, ' ')}
      </div>
      <div style={{ marginTop: 6, display: 'flex', gap: 12 }}>
        <div className={pnl >= 0 ? 'pos' : 'neg'} style={{ fontSize: 16, fontWeight: 600 }}>
          {money(pnl, { showSign: true })}
        </div>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          {row.trade_count} trades · {pct(num(row.win_rate) * 100, 0)} win
        </div>
      </div>
    </div>
  );
}

export default function StrategyBreakdown() {
  const [rows, setRows] = useState([]);

  const load = async () => {
    try {
      const r = await fetch('/portfolio/by-strategy');
      if (!r.ok) throw new Error(r.status);
      setRows(await r.json());
    } catch (e) {
      /* ignore */
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 7000);
    return () => clearInterval(id);
  }, []);

  // rows already sorted by total_pnl desc on the server, but be defensive.
  const sorted = [...rows].sort((a, b) => num(b.total_pnl) - num(a.total_pnl));
  const top3 = [sorted[0], sorted[1], sorted[2]];
  const totalPnl = sorted.reduce((acc, r) => acc + num(r.total_pnl), 0);

  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Best P&amp;L by strategy</h2>
        <span className="panel-sub">
          {rows.length} strategies traded · net {money(totalPnl, { showSign: true })}
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="empty">
          <div className="title">No closed trades yet</div>
          <div className="hint">Once trades close, P&amp;L by strategy will show up here.</div>
        </div>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 8, marginBottom: 12 }}>
            {[1, 2, 3].map((r) => <Podium key={r} row={top3[r - 1]} rank={r} />)}
          </div>

          <div className="scroll" style={{ maxHeight: 220 }}>
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th className="num">Trades</th>
                  <th className="num">Win rate</th>
                  <th className="num">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((r, idx) => {
                  const pnl = num(r.total_pnl);
                  return (
                    <tr key={r.strategy}>
                      <td>
                        {idx < 3 && (
                          <span style={{ marginRight: 4 }}>{['🥇', '🥈', '🥉'][idx]}</span>
                        )}
                        <strong style={{ textTransform: 'capitalize' }}>
                          {(r.strategy || '').replace(/_/g, ' ')}
                        </strong>
                      </td>
                      <td className="num">{r.trade_count}</td>
                      <td className="num">{pct(num(r.win_rate) * 100, 0)}</td>
                      <td className={`num ${pnl >= 0 ? 'pos' : 'neg'}`}>
                        {money(pnl, { showSign: true })}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
