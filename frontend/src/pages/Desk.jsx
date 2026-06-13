import React, { useEffect, useState } from 'react';
import TheoryChart from '../components/TheoryChart.jsx';
import TickerSearch from '../components/TickerSearch.jsx';
import { useLivePrice } from '../lib/useLivePrice.js';
import { money } from '../lib/format.js';

// Chart standardization pass (Phase 19.x) — Desk per-stock cards now use
// TheoryChart (lightweight-charts) instead of the legacy SVG CandleChart.
// Each card keeps its own timeframe preset; live price ticks come from
// useLivePrice and flow into TheoryChart's `liveTick` prop so the
// rightmost forming candle updates without a re-fetch.

const DEFAULT_TICKERS = ['SPY', 'BTC-USD', 'NVDA', 'ETH-USD', 'AAPL', 'TSLA'];
const STORE_KEY = 'tb-desk-tickers';

const PRESETS = [
  { label: '1D · 5m',  period: '1d',  interval: '5m'  },
  { label: '5D · 15m', period: '5d',  interval: '15m' },
  { label: '1M · 30m', period: '1mo', interval: '30m' },
  { label: '3M · 1d',  period: '3mo', interval: '1d'  },
  { label: '1Y · 1d',  period: '1y',  interval: '1d'  },
];

function DeskStockCard({ ticker, onTickerChange, onRemove }) {
  const [bars, setBars] = useState([]);
  const [preset, setPreset] = useState(PRESETS[1]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const live = useLivePrice(ticker, { enabled: !!ticker, intervalMs: 4000 });

  useEffect(() => {
    if (!ticker) return undefined;
    let active = true;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await fetch(
          `/market/candles/${encodeURIComponent(ticker)}`
            + `?period=${preset.period}&interval=${preset.interval}`,
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        if (active) setBars(Array.isArray(data) ? data : []);
      } catch (e) {
        if (active) { setBars([]); setError(e.message); }
      } finally {
        if (active) setLoading(false);
      }
    };
    load();
    const refreshMs = preset.interval.endsWith('d') || preset.interval.endsWith('wk')
      ? 20000 : 7000;
    const id = setInterval(load, refreshMs);
    return () => { active = false; clearInterval(id); };
  }, [ticker, preset]);

  const last = bars[bars.length - 1];
  const first = bars[0];
  const change = (last && first)
    ? ((last.close - first.open) / first.open) * 100
    : 0;
  const liveTick = (live && live.price > 0)
    ? { price: live.price, ts: Date.now() / 1000 }
    : null;

  const chartHeight = expanded
    ? Math.max(420, window.innerHeight * 0.62)
    : 320;

  const header = (
    <div className="panel-head">
      <div>
        <h2 style={{ margin: 0, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontWeight: 700 }}>{ticker || '—'}</span>
            <span style={{ width: 140 }}>
              <TickerSearch
                onAdd={(s) => onTickerChange && onTickerChange(s)}
                placeholder="change…"
              />
            </span>
          </span>
          {live && live.price > 0 && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 11, color: 'var(--accent)', fontWeight: 600,
            }}>
              <span style={{
                width: 6, height: 6, borderRadius: '50%',
                background: 'var(--accent)', display: 'inline-block',
              }} />
              LIVE {money(live.price)}
            </span>
          )}
        </h2>
        {last && (
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            {money(last.close)}{' '}
            <span className={change >= 0 ? 'pos' : 'neg'}>
              {change >= 0 ? '+' : ''}{change.toFixed(2)}%
            </span>{' · '}{bars.length} candles
          </div>
        )}
      </div>
      <div className="row">
        {PRESETS.map((p) => (
          <button
            key={p.label}
            className={`btn small ${p === preset ? 'primary' : ''}`}
            onClick={() => setPreset(p)}
          >
            {p.label}
          </button>
        ))}
        {!expanded && (
          <button className="btn small" onClick={() => setExpanded(true)} title="Expand">
            ⤢ Expand
          </button>
        )}
        {expanded && (
          <button className="btn small" onClick={() => setExpanded(false)}>× Close</button>
        )}
        {onRemove && (
          <button
            className="btn small ghost"
            onClick={onRemove}
            title="Remove widget"
            style={{ color: 'var(--danger)' }}
          >×</button>
        )}
      </div>
    </div>
  );

  const body = (
    <div style={{ height: chartHeight }}>
      {error ? (
        <div className="empty">
          <div className="title" style={{ color: 'var(--danger)' }}>
            Couldn't load candles
          </div>
          <div className="hint">{error}</div>
        </div>
      ) : bars.length === 0 ? (
        <div className="empty">{loading ? 'Loading…' : 'No candles for this range.'}</div>
      ) : (
        <TheoryChart
          bars={bars}
          annotations={{}}
          palettes={{}}
          primaryTheory={null}
          liveTick={liveTick}
        />
      )}
    </div>
  );

  if (expanded) {
    return (
      <div
        onClick={() => setExpanded(false)}
        style={{
          position: 'fixed', inset: 0, background: 'rgba(13, 20, 36, 0.55)',
          zIndex: 100, padding: 24, display: 'grid', placeItems: 'center',
        }}
      >
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            background: 'var(--panel)',
            border: '1px solid var(--border)',
            borderRadius: 14,
            width: 'min(1400px, 96vw)',
            height: '90vh',
            padding: 18,
            boxShadow: 'var(--shadow-md)',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          {header}
          <div style={{ flex: 1, minHeight: 0 }}>{body}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      {header}
      {body}
    </div>
  );
}

export default function Desk() {
  const [tickers, setTickers] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(STORE_KEY));
      if (Array.isArray(saved) && saved.length) return saved;
    } catch { /* ignore */ }
    return DEFAULT_TICKERS;
  });

  useEffect(() => {
    localStorage.setItem(STORE_KEY, JSON.stringify(tickers));
  }, [tickers]);

  const setAt = (i, sym) => setTickers((t) => t.map((x, j) => (j === i ? sym.toUpperCase() : x)));
  const removeAt = (i) => setTickers((t) => t.filter((_, j) => j !== i));
  const add = (sym = 'QQQ') => setTickers((t) => (t.includes(sym) ? t : [...t, sym]));

  const QUICK_ADD = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'SPY', 'NVDA', 'AAPL'];

  return (
    <div>
      <div className="panel-head" style={{ marginBottom: 14 }}>
        <div>
          <h2 style={{ margin: 0 }}>Trading Desk — all your stocks, live, in one frame</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            Every panel is independent: search to swap the ticker, pick its own timeframe.
            Live price ticks on each. Your layout is saved.
          </div>
        </div>
        <div className="row">
          <button className="btn small" onClick={() => setTickers(DEFAULT_TICKERS)}>Reset layout</button>
          <button className="btn small primary" onClick={() => add('QQQ')}>+ Add widget</button>
        </div>
      </div>

      <div className="row" style={{ gap: 6, marginBottom: 12 }}>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>Quick add:</span>
        {QUICK_ADD.map((s) => (
          <button key={s} className="btn small" onClick={() => add(s)} disabled={tickers.includes(s)}>
            + {s}
          </button>
        ))}
      </div>

      {tickers.length === 0 ? (
        <div className="empty">
          <div className="title">No widgets</div>
          <div className="hint">Click "Add widget" to start tracking a stock.</div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(460px, 1fr))', gap: 14 }}>
          {tickers.map((tk, i) => (
            <DeskStockCard
              key={`${tk}-${i}`}
              ticker={tk}
              onTickerChange={(s) => setAt(i, s)}
              onRemove={() => removeAt(i)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
