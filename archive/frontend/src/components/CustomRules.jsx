import React, { useEffect, useState } from 'react';

const PLACEHOLDER = `# one rule per line, e.g.
buy call when RSI < 30 and price crosses 50MA
buy stock when sentiment > 0.5
sell stock when RSI > 70`;

export default function CustomRules({ value, onChange }) {
  const [draft, setDraft] = useState(value ?? '');
  useEffect(() => setDraft(value ?? ''), [value]);

  return (
    <div className="panel col-6">
      <h2>Custom rules</h2>
      <textarea
        value={draft}
        placeholder={PLACEHOLDER}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => onChange(draft)}
      />
      <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 8 }}>
        Active when strategy is set to <code>custom</code>. Rules without matching tokens are skipped.
      </div>
    </div>
  );
}
