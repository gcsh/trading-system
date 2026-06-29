import React, { useEffect, useState } from 'react';
import { money, num, pct } from '../lib/format.js';

export default function OpenPositions() {
  const [positions, setPositions] = useState([]);
  const [err, setErr] = useState(null);

  const load = async () => {
    try {
      const res = await fetch('/portfolio/positions');
      if (!res.ok) throw new Error(res.status);
      setPositions(await res.json());
      setErr(null);
    } catch (e) {
      setErr(e.message);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Open positions</h2>
        <span className="panel-sub">{positions.length} held</span>
      </div>
      {err && <div style={{ color: 'var(--danger)' }}>{err}</div>}
      {positions.length === 0 ? (
        <div className="empty">No open positions.</div>
      ) : (
        <div className="scroll" style={{ maxHeight: 260 }}>
          <table>
            <thead>
              <tr>
                <th>Ticker</th>
                <th className="num">Qty</th>
                <th className="num">Avg cost</th>
                <th className="num">Last</th>
                <th className="num">Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const qty = num(p.quantity);
                const avg = num(p.avg_cost);
                const cur = p.current_price != null ? num(p.current_price) : null;
                const upnl = p.unrealized_pnl;
                const upnlPct = p.unrealized_pnl_pct;
                const cls = upnl == null ? '' : upnl >= 0 ? 'pos' : 'neg';
                return (
                  <tr key={p.id ?? `${p.ticker}-${p.kind}`}>
                    <td>
                      <strong>{p.ticker}</strong>{' '}
                      <span className="pill off" style={{ fontSize: 10 }}>{p.kind || 'stock'}</span>
                    </td>
                    <td className="num">{qty.toFixed(2)}</td>
                    <td className="num">{money(avg)}</td>
                    <td className="num">{cur != null ? money(cur) : '—'}</td>
                    <td className={`num ${cls}`}>
                      {upnl == null ? '—' : `${money(upnl, { showSign: true })} (${pct(upnlPct, 1, { showSign: true })})`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
