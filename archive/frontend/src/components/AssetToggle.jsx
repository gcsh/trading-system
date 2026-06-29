import React, { useEffect, useState } from 'react';
import TickerSearch from './TickerSearch.jsx';

const ASSET_TYPES = ['stocks', 'options', 'crypto'];
const TRADE_STYLES = ['intraday', 'swing', 'scalp'];

export default function AssetToggle({ assetTypes, tradeStyles, tickers, onChange }) {
  const [draftTickers, setDraftTickers] = useState((tickers || []).join(', '));

  // Keep the comma-text view in sync when tickers change from outside (e.g. add via search).
  useEffect(() => {
    setDraftTickers((tickers || []).join(', '));
  }, [tickers]);

  const toggle = (list, key) => (list.includes(key) ? list.filter((x) => x !== key) : [...list, key]);

  const commitTickers = () => {
    const parsed = draftTickers
      .split(',')
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean);
    onChange({ tickers: parsed });
  };

  const addTicker = (symbol) => {
    const upper = symbol.toUpperCase();
    if (!tickers?.includes(upper)) {
      onChange({ tickers: [...(tickers || []), upper] });
    }
  };

  const removeTicker = (symbol) => {
    onChange({ tickers: (tickers || []).filter((t) => t !== symbol) });
  };

  return (
    <div className="panel col-6">
      <h2>Assets &amp; tickers</h2>

      <label style={{ marginTop: 4 }}>Asset types</label>
      <div className="row" style={{ marginBottom: 12 }}>
        {ASSET_TYPES.map((key) => (
          <span
            key={key}
            className={`pill ${assetTypes?.includes(key) ? 'on' : 'off'}`}
            style={{ cursor: 'pointer' }}
            onClick={() => onChange({ asset_types: toggle(assetTypes || [], key) })}
          >
            {key}
          </span>
        ))}
      </div>

      <label>Trade style</label>
      <div className="row" style={{ marginBottom: 14 }}>
        {TRADE_STYLES.map((key) => (
          <span
            key={key}
            className={`pill ${tradeStyles?.includes(key) ? 'on' : 'off'}`}
            style={{ cursor: 'pointer' }}
            onClick={() => onChange({ trade_styles: toggle(tradeStyles || [], key) })}
          >
            {key}
          </span>
        ))}
      </div>

      <label>Add ticker (search or type symbol)</label>
      <TickerSearch onAdd={addTicker} placeholder="Search symbol or company…" />

      <div style={{ marginTop: 12 }}>
        <label>
          Active tickers ({tickers?.length ?? 0})
        </label>
        <div className="row">
          {(tickers || []).length === 0 ? (
            <span style={{ color: 'var(--muted)', fontSize: 12 }}>No tickers — add some via the search above.</span>
          ) : (
            (tickers || []).map((t) => (
              <span
                key={t}
                className="pill"
                style={{ cursor: 'pointer', paddingRight: 4 }}
                onClick={() => removeTicker(t)}
                title="Click to remove"
              >
                {t} <span style={{ color: 'var(--muted)', marginLeft: 4 }}>✕</span>
              </span>
            ))
          )}
        </div>
      </div>

      <details style={{ marginTop: 12 }}>
        <summary style={{ fontSize: 11, color: 'var(--muted)', cursor: 'pointer' }}>
          Bulk edit (comma-separated)
        </summary>
        <input
          type="text"
          style={{ marginTop: 6 }}
          value={draftTickers}
          onChange={(e) => setDraftTickers(e.target.value)}
          onBlur={commitTickers}
        />
      </details>
    </div>
  );
}
