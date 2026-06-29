import React, { useEffect, useState } from 'react';

const REGIME_STYLE = {
  long_gamma:  { bg: 'var(--accent-soft)', fg: 'var(--accent)', label: '🟢 LONG GAMMA' },
  short_gamma: { bg: 'var(--danger-soft)', fg: 'var(--danger)', label: '🔴 SHORT GAMMA' },
  unknown:     { bg: 'var(--panel-2)', fg: 'var(--muted)', label: '—' },
};

const PRESSURE_STYLE = {
  high:   { fg: 'var(--danger)' },
  normal: { fg: 'var(--text-soft)' },
  low:    { fg: 'var(--muted)' },
};

const DIRECTION_STYLE = {
  bullish: { fg: 'var(--accent)' },
  bearish: { fg: 'var(--danger)' },
  mixed:   { fg: 'var(--warn)' },
  neutral: { fg: 'var(--muted)' },
};

export default function FlowIQPanel({ ticker }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!ticker) return undefined;
    let active = true;
    const load = () => fetch(`/flowintel/${encodeURIComponent(ticker)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (active && d) setData(d); })
      .catch((e) => active && setErr(String(e)));
    load();
    const id = setInterval(load, 30000);
    return () => { active = false; clearInterval(id); };
  }, [ticker]);

  if (!data) {
    return (
      <div className="panel">
        <div className="panel-head"><h2>🧠 Flow IQ</h2><span className="panel-sub">{err ? 'unavailable' : 'loading…'}</span></div>
        <div className="empty">Dealer positioning + flow aggression analytics will appear here.</div>
      </div>
    );
  }

  const dp = data.dealer_positioning || {};
  const fp = data.flow_profile || {};
  const regime = REGIME_STYLE[dp.regime] || REGIME_STYLE.unknown;
  const pressure = PRESSURE_STYLE[dp.hedging_pressure] || PRESSURE_STYLE.normal;
  const direction = DIRECTION_STYLE[fp.direction] || DIRECTION_STYLE.neutral;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>🧠 Flow IQ — {data.ticker}</h2>
        <span style={{
          fontWeight: 700, fontSize: 11, padding: '2px 9px', borderRadius: 4,
          background: regime.bg, color: regime.fg,
        }}>{regime.label}</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 12 }}>
        <Section title="Dealer positioning">
          <KV label="Pinning probability" value={pct(dp.pinning_probability)} barPct={dp.pinning_probability * 100} barColor="var(--info)" />
          <KV label="Hedging pressure" value={dp.hedging_pressure || '—'} valueColor={pressure.fg} />
          <KV label="Dominant wall" value={dp.dominant_wall || '—'} />
          <KV label="Call wall" value={fmtPct(dp.call_wall_distance_pct, 'from spot')} />
          <KV label="Put wall" value={fmtPct(dp.put_wall_distance_pct, 'from spot')} />
          <KV label="Flip" value={fmtPct(dp.flip_distance_pct, 'from spot')} />
        </Section>
        <Section title="Institutional flow">
          <KV label="Direction" value={fp.direction || 'neutral'} valueColor={direction.fg} />
          <KV label="Sweep aggressiveness" value={pct(fp.sweep_aggressiveness)} barPct={fp.sweep_aggressiveness * 100} barColor="var(--warn)" />
          <KV label="Bullish sweeps (30m)" value={fp.bullish_sweeps ?? 0} />
          <KV label="Bearish sweeps (30m)" value={fp.bearish_sweeps ?? 0} />
          <KV label="Pre-market bullish" value={fp.premarket_bullish_sweeps ?? 0} />
          <KV label="Repeat orders" value={fp.repeat_orders ?? 0} />
          {fp.darkpool_confirms && <KV label="Dark-pool" value="✓ corroborates" valueColor="var(--info)" />}
        </Section>
      </div>

      {dp.notes?.length > 0 && (
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10, fontSize: 12.5, color: 'var(--text-soft)' }}>
          {dp.notes.map((n, i) => <div key={i} style={{ marginBottom: 2 }}>• {n}</div>)}
        </div>
      )}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600, marginBottom: 8 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function KV({ label, value, valueColor, barPct, barColor }) {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5 }}>
        <span style={{ color: 'var(--muted)' }}>{label}</span>
        <strong style={{ color: valueColor || 'var(--text)', fontFeatureSettings: '"tnum"' }}>{value}</strong>
      </div>
      {barPct != null && (
        <div style={{ height: 4, background: 'var(--panel-2)', borderRadius: 2, marginTop: 3, overflow: 'hidden' }}>
          <div style={{ width: `${Math.max(0, Math.min(100, barPct))}%`, height: '100%', background: barColor || 'var(--info)' }} />
        </div>
      )}
    </div>
  );
}

function pct(v) {
  const n = Number(v) || 0;
  return `${Math.round(n * 100)}%`;
}

function fmtPct(v, suffix) {
  if (v == null || isNaN(v)) return '—';
  const sign = v >= 0 ? '+' : '';
  return `${sign}${Number(v).toFixed(2)}% ${suffix || ''}`.trim();
}
