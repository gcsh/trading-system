/**
 * ScanUniverseStrip — shows the exact ticker list the engine will scan
 * next cycle, sourced from union(config.tickers, watchlist). Surfaces
 * which came from config vs watchlist so the operator knows where to
 * add new symbols.
 */
import React, { useCallback, useEffect, useState } from 'react';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

export default function ScanUniverseStrip() {
  const [universe, setUniverse] = useState(null);

  const load = useCallback(async () => {
    try { setUniverse(await api('/authority/scan-universe')); }
    catch { /* ignore */ }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 12000);
    return () => clearInterval(id);
  }, [load]);

  if (!universe) return null;
  const details = universe.details || [];

  return (
    <div className="panel" style={{ background: 'var(--panel-2)' }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10, alignItems: 'flex-start', flexWrap: 'wrap', gap: 8 }}>
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>Scan universe · next cycle</div>
          <div style={{ fontSize: 14, fontWeight: 600, marginTop: 4 }}>
            {universe.count} ticker{universe.count === 1 ? '' : 's'} ·{' '}
            <span className="accent-data">
              {universe.from_config?.length || 0} from config
            </span>
            {' + '}
            <span className="accent-intel">
              {universe.from_watchlist?.length || 0} from watchlist
            </span>
          </div>
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted)', maxWidth: 360, textAlign: 'right' }}>
          The engine scans union(config tickers, watchlist) every cycle.
          Add tickers via <strong>Watchlist</strong> or <strong>Settings → Tickers</strong>.
        </div>
      </div>
      {/* Chips wrap onto multiple lines; max-height + scroll guards
          against very long watchlists pushing the page tall. */}
      <div style={{
        display: 'flex', gap: 6, flexWrap: 'wrap',
        maxHeight: 96, overflowY: 'auto', paddingRight: 4,
      }}>
        {details.map((d) => (
          <span
            key={d.ticker}
            className={d.source === 'config' ? 'pill data' : 'pill purple'}
            style={{ fontWeight: 600, flexShrink: 0 }}
            title={`source: ${d.source}`}
          >
            {d.ticker}
          </span>
        ))}
      </div>
    </div>
  );
}
