import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import CorpusStatusChip from './CorpusStatusChip.jsx';
import TickerSearch from './TickerSearch.jsx';
import { money, num, pct } from '../lib/format.js';

export default function Watchlist({ onSelect }) {
  const [items, setItems] = useState([]);
  const [folders, setFolders] = useState(['default']);
  const [folder, setFolder] = useState('default');
  const [newFolder, setNewFolder] = useState('');
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState(null);

  const loadFolders = async () => {
    try {
      const r = await fetch('/watchlist/folders');
      if (r.ok) setFolders(await r.json());
    } catch (e) { /* ignore */ }
  };

  const load = async () => {
    try {
      const r = await fetch(`/watchlist/items?list_name=${encodeURIComponent(folder)}`);
      if (!r.ok) throw new Error(r.status);
      setItems(await r.json());
      setErr(null);
    } catch (e) {
      setErr(e.message);
    }
  };

  useEffect(() => { loadFolders(); }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 8000);
    return () => clearInterval(id);
  }, [folder]);

  const add = async (ticker) => {
    if (!ticker) return;
    try {
      await fetch('/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker: ticker.toUpperCase(), list_name: folder }),
      });
      load();
      loadFolders();
    } catch (e) {
      setErr(e.message);
    }
  };

  const remove = async (id) => {
    await fetch(`/watchlist/${id}`, { method: 'DELETE' });
    load();
  };

  const createFolder = () => {
    const name = newFolder.trim();
    if (!name) return;
    setFolders((f) => Array.from(new Set([...f, name])));
    setFolder(name);
    setNewFolder('');
    setCreating(false);
    // Folder is created lazily on first ticker add (it's just a list_name).
  };

  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Watchlist</h2>
        <span className="panel-sub">{items.length} in “{folder}”</span>
      </div>

      {/* Folder tabs */}
      <div className="row" style={{ marginBottom: 12, gap: 4 }}>
        {folders.map((f) => (
          <button
            key={f}
            className={`btn small ${folder === f ? 'primary' : ''}`}
            onClick={() => setFolder(f)}
          >
            {f}
          </button>
        ))}
        {creating ? (
          <span className="row" style={{ gap: 4 }}>
            <input
              type="text"
              autoFocus
              value={newFolder}
              onChange={(e) => setNewFolder(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && createFolder()}
              placeholder="folder name"
              style={{ width: 120, padding: '4px 8px' }}
            />
            <button className="btn small primary" onClick={createFolder}>Add</button>
            <button className="btn small ghost" onClick={() => setCreating(false)}>✕</button>
          </span>
        ) : (
          <button className="btn small ghost" onClick={() => setCreating(true)}>+ folder</button>
        )}
      </div>

      <div style={{ marginBottom: 12 }}>
        <TickerSearch onAdd={(symbol) => add(symbol)} placeholder={`Add ticker to “${folder}”…`} />
      </div>
      {err && <div style={{ color: 'var(--danger)', fontSize: 12 }}>{err}</div>}
      {items.length === 0 ? (
        <div className="empty">No tickers in this folder yet — add one above.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th className="num">Price</th>
              <th className="num">Change</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => {
              const q = it.quote || {};
              const price = num(q.price);
              const change = num(q.change_pct);
              const cls = change >= 0 ? 'pos' : 'neg';
              return (
                <tr
                  key={it.id}
                  onClick={() => onSelect && onSelect(it.ticker)}
                  style={{ cursor: onSelect ? 'pointer' : 'default' }}
                >
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center',
                                          gap: 6, flexWrap: 'wrap' }}>
                      <strong>{it.ticker}</strong>
                      {/* MITS Phase 1 — per-ticker corpus bootstrap status
                          pill. Renders only when a CorpusStatus row exists
                          for the ticker; auto-polls while building. */}
                      <CorpusStatusChip ticker={it.ticker} />
                    </div>
                  </td>
                  <td className="num">{price ? money(price) : '—'}</td>
                  <td className={`num ${cls}`}>{q.price ? pct(change, 2, { showSign: true }) : '—'}</td>
                  <td>
                    <div className="row" style={{ gap: 4, justifyContent: 'flex-end' }}>
                      <Link
                        to={`/analysis/${encodeURIComponent(it.ticker)}`}
                        onClick={(e) => e.stopPropagation()}
                        className="btn small ghost"
                        style={{ textDecoration: 'none', padding: '2px 6px' }}
                        title="Open the per-ticker analysis page"
                      >
                        analyze
                      </Link>
                      <button
                        className="btn small ghost"
                        onClick={(e) => { e.stopPropagation(); remove(it.id); }}
                      >
                        ✕
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
