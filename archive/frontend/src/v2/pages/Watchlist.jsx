/* MITS Phase 19 Cluster A — Watchlist Manager v2 (/v2/watchlist).
 *
 * Operator's dedicated page for managing the engine scan universe.
 *
 *   ROW 0  Add-ticker form + folder selector
 *   ROW 1  KPI strip — Total / Live / Stale / Up / Down / Avg Change
 *   ROW 2  Grid of ticker tiles (5 cols desktop, 1 col mobile)
 *           - live price (useLivePrices)
 *           - change %
 *           - sparkline (last 60 bars / fallback flat)
 *           - View Detail + Remove buttons
 *
 * Data sources:
 *   GET    /watchlist/folders
 *   GET    /watchlist/items?list_name=…
 *   POST   /watchlist             body {ticker, list_name?, notes?}
 *   DELETE /watchlist/{item_id}   (NOTE: uses item id, NOT ticker)
 *   /quote/{ticker}               via useLivePrices hook
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import WatchlistTickerRow from '../components/WatchlistTickerRow.jsx';
import { useLivePrices } from '../hooks/useLivePrice.js';

const POLL_LIST_MS = 30_000;

function fmtN(v) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toLocaleString();
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}%`;
}

/* ── Add Ticker form ────────────────────────────────────────────────── */
function AddTickerForm({ folder, onAdd, error, busy }) {
  const [ticker, setTicker] = useState('');
  const [notes, setNotes] = useState('');
  function submit(e) {
    e.preventDefault();
    if (!ticker.trim()) return;
    onAdd({ ticker: ticker.trim().toUpperCase(), list_name: folder, notes });
    setTicker(''); setNotes('');
  }
  return (
    <form onSubmit={submit} className="v2-wl-add">
      <input
        type="text"
        placeholder="Ticker (e.g. AAPL)"
        value={ticker}
        onChange={(e) => setTicker(e.target.value)}
        className="v2-wl-add__input v2-wl-add__input--ticker"
        maxLength={10}
        autoCapitalize="characters"
      />
      <input
        type="text"
        placeholder="Notes (optional)"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        className="v2-wl-add__input"
        maxLength={120}
      />
      <button
        type="submit"
        className="v2-wl-add__btn"
        disabled={!ticker.trim() || busy}
      >
        {busy ? 'Adding…' : '+ Add'}
      </button>
      {error && (
        <span className="v2-wl-add__err">{error}</span>
      )}
    </form>
  );
}

/* ── KPI strip ──────────────────────────────────────────────────────── */
function KPIStrip({ items, livePrices }) {
  const total = items.length;
  const withQuote = items.filter(i => i.quote?.price != null).length;
  const stale = items.filter(i => {
    const lp = livePrices[i.ticker];
    if (!lp) return true;
    const ageSec = lp.age_seconds ?? 0;
    return ageSec > 60;
  }).length;
  const changes = items
    .map(i => i.quote?.change_pct)
    .filter(c => c != null && isFinite(c))
    .map(Number);
  const ups = changes.filter(c => c >= 0).length;
  const downs = changes.filter(c => c < 0).length;
  const avgChange = changes.length
    ? changes.reduce((s, x) => s + x, 0) / changes.length
    : null;

  return (
    <div className="v2-wl-kpi">
      <Card><Stat label="Total Tickers" value={fmtN(total)}
        hint="Tickers in this folder"
      /></Card>
      <Card><Stat label="Live Quotes" value={fmtN(withQuote)}
        deltaPositive={withQuote === total && total > 0}
        delta={withQuote === total ? 'all live' : `${total - withQuote} stale`}
        hint="Tickers with a fresh quote"
      /></Card>
      <Card><Stat label="Stale" value={fmtN(stale)}
        deltaPositive={stale === 0}
        delta={stale === 0 ? 'clean' : 'review'}
        hint="Tickers without a quote within the last 60s"
      /></Card>
      <Card><Stat label="Up Today" value={fmtN(ups)}
        deltaPositive={ups >= downs}
        hint="Tickers with positive change %"
      /></Card>
      <Card><Stat label="Down Today" value={fmtN(downs)}
        deltaPositive={downs <= ups}
        hint="Tickers with negative change %"
      /></Card>
      <Card><Stat label="Avg Change"
        value={fmtPctSigned(avgChange)}
        deltaPositive={avgChange != null && avgChange >= 0}
        delta={avgChange != null && avgChange >= 0 ? 'risk-on' : 'risk-off'}
        hint="Mean change % across watchlist"
      /></Card>
    </div>
  );
}

/* ── Page ───────────────────────────────────────────────────────────── */
export default function Watchlist() {
  const [folders, setFolders] = useState(['default']);
  const [folder, setFolder] = useState('default');
  const [items, setItems] = useState(null);  // null = loading
  const [err, setErr] = useState(null);
  const [addBusy, setAddBusy] = useState(false);
  const [addErr, setAddErr] = useState(null);
  const [search, setSearch] = useState('');

  // Fetch list
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [fRes, iRes] = await Promise.all([
          fetch('/watchlist/folders'),
          fetch(`/watchlist/items?list_name=${encodeURIComponent(folder)}`),
        ]);
        if (cancelled) return;
        if (fRes.ok) {
          const fJson = await fRes.json();
          if (Array.isArray(fJson)) setFolders(fJson);
        }
        if (!iRes.ok) throw new Error(`HTTP ${iRes.status}`);
        const iJson = await iRes.json();
        setItems(Array.isArray(iJson) ? iJson : []);
        setErr(null);
      } catch (e) {
        if (!cancelled) { setErr(e.message); setItems([]); }
      }
    }
    load();
    const id = setInterval(load, POLL_LIST_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [folder]);

  const tickers = useMemo(
    () => (items || []).map(i => i.ticker).filter(Boolean),
    [items],
  );
  const { ticks: livePrices } = useLivePrices(tickers, tickers.length > 0);

  // Apply search filter
  const filtered = useMemo(() => {
    if (!items) return [];
    if (!search.trim()) return items;
    const q = search.trim().toUpperCase();
    return items.filter(i =>
      i.ticker.includes(q) ||
      (i.notes || '').toUpperCase().includes(q)
    );
  }, [items, search]);

  async function addTicker({ ticker, list_name, notes }) {
    setAddBusy(true);
    setAddErr(null);
    try {
      const r = await fetch('/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, list_name, notes }),
      });
      if (!r.ok) {
        const text = await r.text();
        throw new Error(text || `HTTP ${r.status}`);
      }
      const j = await r.json();
      // Optimistic insert
      setItems(prev => {
        if (!prev) return [j];
        if (prev.some(i => i.id === j.id)) return prev;
        return [...prev, j];
      });
    } catch (e) {
      setAddErr(e.message);
    } finally {
      setAddBusy(false);
    }
  }

  async function removeTicker(itemId, ticker) {
    if (!itemId) return;
    if (!window.confirm(`Remove ${ticker} from watchlist?`)) return;
    try {
      const r = await fetch(`/watchlist/${itemId}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setItems(prev => (prev || []).filter(i => i.id !== itemId));
    } catch (e) {
      setErr(`Could not remove ${ticker}: ${e.message}`);
    }
  }

  return (
    <div className="v2-root v2-wl-page">
      <Section
        title="Watchlist Manager"
        subtitle="Engine scan universe — add, monitor, manage"
        actions={
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <label style={{
              fontSize: 'var(--font-size-xs)',
              color: 'var(--text-tertiary)',
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
            }}>Folder</label>
            <select
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
              className="v2-wl-folder"
            >
              {folders.map(f => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
        }
      >
        <Card variant="default">
          <AddTickerForm
            folder={folder}
            onAdd={addTicker}
            error={addErr}
            busy={addBusy}
          />
        </Card>

        <div style={{ marginTop: 16 }}>
          <KPIStrip items={items || []} livePrices={livePrices} />
        </div>

        <Card variant="default" style={{ marginTop: 16 }}>
          <div className="v2-wl-toolbar">
            <input
              type="search"
              placeholder="Filter by ticker or notes…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="v2-wl-search"
            />
            <span className="v2-wl-toolbar__count mono">
              {filtered.length} / {(items || []).length}
            </span>
          </div>
        </Card>

        {err && (
          <Card variant="outlined" style={{ marginTop: 16,
            borderColor: 'var(--accent-red-dim)',
            color: 'var(--accent-red)' }}>
            {err}
          </Card>
        )}

        <div className="v2-wl-grid" style={{ marginTop: 16 }}>
          {items != null && filtered.length === 0 && !err && (
            <Card style={{ gridColumn: '1 / -1' }}>
              <EmptyState
                icon="◉"
                message={
                  items.length === 0
                    ? `No tickers in "${folder}". Add one above to get started.`
                    : 'No matches for that filter.'
                }
              />
            </Card>
          )}
          {filtered.map(item => {
            const lp = livePrices[item.ticker];
            const ageSec = lp?.age_seconds ?? null;
            const stale = ageSec != null ? ageSec > 60 : false;
            return (
              <WatchlistTickerRow
                key={item.id}
                item={item}
                livePrice={lp}
                stale={stale}
                onRemove={removeTicker}
              />
            );
          })}
        </div>
      </Section>
    </div>
  );
}
