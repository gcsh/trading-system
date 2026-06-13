import React from 'react';

const SIGNALS = ['technical', 'news', 'fundamentals', 'sentiment'];

export default function SignalToggles({ value, onChange }) {
  const flip = (key) => onChange({ ...value, [key]: !value?.[key] });
  return (
    <div className="panel col-6">
      <h2>Signal sources</h2>
      <div className="row">
        {SIGNALS.map((key) => (
          <span
            key={key}
            className={`pill ${value?.[key] ? 'on' : 'off'}`}
            style={{ cursor: 'pointer' }}
            onClick={() => flip(key)}
          >
            {key}
          </span>
        ))}
      </div>
    </div>
  );
}
