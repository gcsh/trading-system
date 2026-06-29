import React, { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { money, num, shortTime, shortDate } from '../lib/format.js';
import TradeDetail from './TradeDetail.jsx';

const COLUMNS = [
  { key: 'timestamp', label: 'Time' },
  { key: 'ticker', label: 'Ticker' },
  { key: 'action', label: 'Action' },
  { key: 'instrument', label: 'Instrument' },
  { key: 'strategy', label: 'Strategy' },
  { key: 'quantity', label: 'Qty', align: 'num' },
  { key: 'price', label: 'Price', align: 'num' },
  { key: 'pnl', label: 'P&L', align: 'num' },
  { key: 'status', label: 'Status' },
];

function statusPill(status) {
  if (!status) return <span className="pill off">—</span>;
  if (status === 'closed') return <span className="pill on"><span className="dot" />closed</span>;
  if (status === 'open') return <span className="pill info"><span className="dot" />open</span>;
  if (status === 'partial') return <span className="pill warn"><span className="dot" />partial</span>;
  return <span className="pill off">{status}</span>;
}

function instrumentCell(t) {
  if (t.instrument === 'option') {
    return (
      <span>
        <span className={`pill ${t.option_type === 'call' ? 'on' : 'danger'}`}>{(t.option_type || '').toUpperCase()}</span>{' '}
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>
          {t.strike ? `$${num(t.strike).toFixed(0)}` : ''} {t.expiration || ''}
        </span>
      </span>
    );
  }
  if (t.instrument === 'spread') {
    return <span className="pill purple">spread{t.expiration ? ` · ${t.expiration}` : ''}</span>;
  }
  return <span className="pill off">stock</span>;
}

export default function TradesTable() {
  const [trades, setTrades] = useState([]);
  const [sort, setSort] = useState({ key: 'timestamp', dir: 'desc' });
  const [selected, setSelected] = useState(null);
  // Deep-link: ?id=123 opens (and is set by clicking) a trade — bookmarkable &
  // reload-safe. Query param, not a path, to avoid colliding with the
  // /trades/{id} API route.
  const [params, setParams] = useSearchParams();
  const openId = params.get('id');

  const load = async () => {
    try {
      const res = await fetch('/trades/list?limit=200');
      if (!res.ok) throw new Error(res.status);
      setTrades(await res.json());
    } catch (e) {
      console.warn('trades load failed', e);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  // Open / close the detail purely from the URL so reload + back/forward work.
  useEffect(() => {
    let active = true;
    if (!openId) { setSelected(null); return undefined; }
    // /detail includes decision-log analytics, execution telemetry, and
    // per-trade audit violations so the drawer shows the full back-story.
    fetch(`/trades/${encodeURIComponent(openId)}/detail`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (active) setSelected(d); })
      .catch(() => { if (active) setSelected(null); });
    return () => { active = false; };
  }, [openId]);

  const openDetail = (t) => {
    const next = new URLSearchParams(params);
    next.set('id', String(t.id));
    setParams(next);
  };
  const closeDetail = () => {
    const next = new URLSearchParams(params);
    next.delete('id');
    setParams(next);
  };

  const sorted = useMemo(() => {
    const arr = [...trades];
    arr.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'number' && typeof bv === 'number') {
        return sort.dir === 'asc' ? av - bv : bv - av;
      }
      return sort.dir === 'asc'
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
    return arr;
  }, [trades, sort]);

  const toggleSort = (key) =>
    setSort((s) => ({ key, dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc' }));

  return (
    <div className="panel col-12">
      <div className="panel-head">
        <h2>Trades</h2>
        <span className="panel-sub">{trades.length} total · click a row for full detail</span>
      </div>
      {trades.length === 0 ? (
        <div className="empty">
          <div className="title">No trades yet</div>
          <div className="hint">Run a cycle or use Force trade. Each trade opens a detail view with the chart and the reasoning.</div>
        </div>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    onClick={() => toggleSort(c.key)}
                    className={c.align === 'num' ? 'num' : ''}
                  >
                    {c.label}
                    {sort.key === c.key && (sort.dir === 'asc' ? ' ↑' : ' ↓')}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((t) => {
                const pnl = num(t.pnl, null);
                const pnlClass = pnl == null ? '' : pnl >= 0 ? 'pos' : 'neg';
                const qtyLabel = t.instrument === 'option' || t.instrument === 'spread'
                  ? `${t.contracts ?? t.quantity}x`
                  : num(t.quantity).toFixed(4);
                return (
                  <tr key={t.id} onClick={() => openDetail(t)} style={{ cursor: 'pointer' }}>
                    <td title={t.timestamp || ''}>
                      <div>{shortTime(t.timestamp)}</div>
                      <div style={{ color: 'var(--muted)', fontSize: 11 }}>{shortDate(t.timestamp)}</div>
                    </td>
                    <td><strong>{t.ticker}</strong></td>
                    <td>{(t.action || '').replace(/_/g, ' ')}</td>
                    <td>{instrumentCell(t)}</td>
                    <td><span style={{ color: 'var(--text-soft)' }}>{t.strategy || '—'}</span></td>
                    <td className="num">{qtyLabel}</td>
                    <td className="num">{money(t.price)}</td>
                    <td className={`num ${pnlClass}`}>
                      {pnl == null ? '—' : money(pnl, { showSign: true })}
                    </td>
                    <td>{statusPill(t.status)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {selected && <TradeDetail trade={selected} onClose={closeDetail} />}
    </div>
  );
}
