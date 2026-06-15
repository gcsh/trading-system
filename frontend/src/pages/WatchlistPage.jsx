import React, { useEffect, useState } from 'react';
import Watchlist from '../components/Watchlist.jsx';
import TheoryChart from '../components/TheoryChart.jsx';
import { useLivePrice } from '../lib/useLivePrice.js';
import { pickLiveBadge } from '../lib/liveBadge.js';
import { money } from '../lib/format.js';

// Chart standardization pass (Phase 19.x) — the watchlist preview chart
// now uses TheoryChart (lightweight-charts) instead of the legacy SVG
// CandleChart. The page owns the fetch + preset switcher so TheoryChart
// stays a pure renderer of bars.

const PRESETS = [
  { label: '1D · 5m',  period: '1d',  interval: '5m'  },
  { label: '5D · 15m', period: '5d',  interval: '15m' },
  { label: '1M · 30m', period: '1mo', interval: '30m' },
  { label: '3M · 1d',  period: '3mo', interval: '1d'  },
  { label: '1Y · 1d',  period: '1y',  interval: '1d'  },
];

function WatchlistChartPanel({ ticker }) {
  const [bars, setBars] = useState([]);
  const [preset, setPreset] = useState(PRESETS[1]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const live = useLivePrice(ticker, { enabled: !!ticker, intervalMs: 4000 });

  useEffect(() => {
    if (!ticker) {
      setBars([]);
      return undefined;
    }
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

  return (
    <div className="panel col-12">
      <div className="panel-head">
        <div>
          <h2 style={{ margin: 0, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            {ticker || 'Pick a ticker'}
            {live && live.price > 0 && (() => {
              const badge = pickLiveBadge(live);
              const color = {
                success: 'var(--accent)', warning: '#ffd166',
                danger: '#e8606e', muted: 'var(--muted)',
              }[badge.tone] || 'var(--muted)';
              return (
                <span title={badge.title} style={{
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                  fontSize: 11, color, fontWeight: 600,
                }}>
                  <span style={{
                    width: 6, height: 6, borderRadius: '50%',
                    background: color, display: 'inline-block',
                  }} />
                  {badge.label}
                </span>
              );
            })()}
          </h2>
          {ticker && last && (
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
              {money(last.close)}{' '}
              <span className={change >= 0 ? 'pos' : 'neg'}>
                {change >= 0 ? '+' : ''}{change.toFixed(2)}%
              </span>{' · '}{bars.length} candles
            </div>
          )}
        </div>
        {ticker && (
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
          </div>
        )}
      </div>
      <div style={{ height: 420 }}>
        {!ticker ? (
          <div className="empty">
            <div className="title">Pick a ticker</div>
            <div className="hint">Select a row in the watchlist to preview its chart.</div>
          </div>
        ) : error ? (
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
    </div>
  );
}

export default function WatchlistPage() {
  const [focus, setFocus] = useState(null);
  return (
    <div className="grid">
      <WatchlistChartPanel ticker={focus} />
      <Watchlist onSelect={setFocus} />
    </div>
  );
}
