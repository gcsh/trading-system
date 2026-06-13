import React from 'react';

const BROKERS = [
  ['local_paper', 'Local Paper (no signup)', 'Built-in simulator. Real market prices, fake cash, runs offline.'],
  ['alpaca_paper', 'Alpaca Paper', 'Free $100k paper account at alpaca.markets (needs API keys)'],
  ['alpaca_live', 'Alpaca Live', 'Real money via Alpaca'],
  ['robinhood', 'Robinhood', 'Unofficial API — use with caution'],
];

export default function BrokerSelector({ broker, paperCash, minConfidence, onChange }) {
  const setCash = (v) => {
    const n = Number(v);
    if (Number.isNaN(n)) return;
    onChange({ paper_cash_override: n });
  };
  const setConf = (v) => {
    const n = Number(v);
    if (Number.isNaN(n)) return;
    onChange({ min_confidence: Math.max(0, Math.min(1, n)) });
  };

  return (
    <div className="panel col-6">
      <h2>Broker &amp; Paper cash</h2>
      <label>Broker</label>
      <select value={broker || 'alpaca_paper'} onChange={(e) => onChange({ broker: e.target.value })}>
        {BROKERS.map(([key, label, desc]) => (
          <option key={key} value={key} title={desc}>{label}</option>
        ))}
      </select>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
        {BROKERS.find(([k]) => k === broker)?.[2]}
      </div>

      <div style={{ marginTop: 12 }}>
        <label>Effective cash to trade with ($)</label>
        <input
          type="number"
          min="50"
          step="50"
          value={paperCash ?? 1000}
          onChange={(e) => setCash(e.target.value)}
        />
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
          Caps how much of the paper account the bot may use. Useful when Alpaca paper gives you $100k but you want to simulate $1,000.
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <label>Minimum confidence to act ({((minConfidence ?? 0.6) * 100).toFixed(0)}%)</label>
        <input
          type="range"
          min="0.3"
          max="0.95"
          step="0.05"
          value={minConfidence ?? 0.6}
          onChange={(e) => setConf(e.target.value)}
        />
      </div>
    </div>
  );
}
