import React from 'react';

export default function AutoExecToggle({ value, onChange }) {
  const on = !!value;
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Auto-execute</h2>
        <span className={`pill ${on ? 'on' : 'off'}`}>
          <span className="dot" />
          {on ? 'live execution' : 'signal-only'}
        </span>
      </div>
      <p style={{ color: 'var(--muted)', marginTop: 0 }}>
        When ON, the bot places real orders (per current broker). When OFF, it fires
        alerts only — perfect for testing strategies and AI confidence without risk.
      </p>
      <button
        className={`btn ${on ? 'danger' : 'primary'}`}
        onClick={() => onChange(!on)}
      >
        {on ? 'Turn auto-execute OFF' : 'Turn auto-execute ON'}
      </button>
    </div>
  );
}
