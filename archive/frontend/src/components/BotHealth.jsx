import React, { useEffect, useState } from 'react';

function isMarketOpen(now = new Date()) {
  // Best-effort NY market hours check (9:30 - 16:00 ET, Mon-Fri).
  const day = now.getUTCDay();
  if (day === 0 || day === 6) return false;
  const utcMinutes = now.getUTCHours() * 60 + now.getUTCMinutes();
  // ET is UTC-5 (standard) / UTC-4 (DST). Approximate with UTC-4 — close enough.
  const etMinutes = (utcMinutes - 4 * 60 + 24 * 60) % (24 * 60);
  return etMinutes >= 9 * 60 + 30 && etMinutes < 16 * 60;
}

export default function BotHealth({ config, status, updateConfig, onStart, onForceTrade }) {
  const [diag, setDiag] = useState(null);
  const [busy, setBusy] = useState(false);

  const loadDiag = async () => {
    try {
      const r = await fetch('/diagnostics/cycle');
      if (r.ok) setDiag(await r.json());
    } catch (e) {
      /* ignore */
    }
  };

  useEffect(() => {
    loadDiag();
    const id = setInterval(loadDiag, 20000);
    return () => clearInterval(id);
  }, []);

  const checks = [
    {
      ok: !!status?.running,
      label: 'Bot is running',
      hint: 'Press Start in the top bar to begin the live loop.',
      fix: status?.running ? null : { label: 'Start bot', action: onStart },
    },
    {
      ok: !!config?.auto_execute,
      label: 'Auto-execute is ON',
      hint: 'When OFF, signals fire but no orders are sent (signal-only mode).',
      fix: config?.auto_execute
        ? null
        : { label: 'Turn ON', action: () => updateConfig({ auto_execute: true }) },
    },
    {
      ok: (config?.tickers?.length ?? 0) > 0,
      label: `Tickers configured (${config?.tickers?.length ?? 0})`,
      hint: 'Add tickers in Settings → Assets & style.',
      fix: null,
    },
    {
      ok: (diag?.actionable_count ?? 0) > 0,
      label: `${diag?.actionable_count ?? 0} of ${diag?.tickers_scanned ?? 0} tickers have actionable signals`,
      hint:
        diag && diag.actionable_count === 0
          ? `Every strategy returned HOLD or scored below ${(diag.min_confidence * 100).toFixed(0)}%. Lower min-confidence, or force a trade to test the executor.`
          : 'A signal must beat min_confidence to trigger an order.',
      fix:
        diag && diag.actionable_count === 0
          ? {
              label: `Lower threshold to ${Math.max(0.2, (config?.min_confidence ?? 0.4) - 0.1).toFixed(2)}`,
              action: () =>
                updateConfig({
                  min_confidence: Math.max(0.2, (config?.min_confidence ?? 0.4) - 0.1),
                }),
            }
          : null,
    },
    {
      ok: isMarketOpen(),
      label: 'US market is open',
      hint: 'Outside 9:30-16:00 ET, free-tier data stops updating intraday — strategies will use last-close values.',
      fix: null,
    },
  ];

  const passing = checks.filter((c) => c.ok).length;
  const allGood = passing === checks.length;

  const force = async () => {
    setBusy(true);
    try {
      const r = await fetch('/bot/force-trade', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
      const body = await r.json();
      if (onForceTrade) onForceTrade(body);
      await loadDiag();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="panel col-12"
      style={{
        borderLeft: `4px solid ${allGood ? 'var(--accent)' : 'var(--warn)'}`,
        marginBottom: 16,
      }}
    >
      <div className="panel-head">
        <h2>
          {allGood ? '🟢 Bot is healthy' : '🟡 Bot health'}{' '}
          <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
            {passing} of {checks.length} checks passing
          </span>
        </h2>
        <div className="row">
          <button
            className="btn small primary"
            onClick={force}
            disabled={busy}
            title="Submits the best signal across all strategies regardless of threshold"
          >
            {busy ? 'Sending…' : '⚡ Force trade now'}
          </button>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8 }}>
        {checks.map((c, i) => (
          <div
            key={i}
            style={{
              background: c.ok ? 'var(--accent-soft)' : 'var(--warn-soft)',
              border: `1px solid ${c.ok ? '#c4e3d4' : '#f0d99a'}`,
              borderRadius: 8,
              padding: 10,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, fontSize: 12.5 }}>
              <span>{c.ok ? '✓' : '!'}</span>
              <span>{c.label}</span>
            </div>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4, lineHeight: 1.4 }}>{c.hint}</div>
            {c.fix && (
              <button className="btn small" style={{ marginTop: 6 }} onClick={c.fix.action}>
                {c.fix.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
