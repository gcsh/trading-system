import React from 'react';
import { num, pct } from '../lib/format.js';

function Slider({ label, value, onChange, min = 0, max = 1, step = 0.05 }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
        <span style={{ color: 'var(--muted)' }}>{label}</span>
        <span style={{ fontFeatureSettings: '"tnum"' }}>{(num(value) * 100).toFixed(0)}%</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={num(value, 0.5)}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        style={{ width: '100%' }}
      />
    </div>
  );
}

function Toggle({ label, value, onChange, hint }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '8px 0',
        borderBottom: '1px solid var(--border)',
      }}
    >
      <div>
        <div style={{ fontWeight: 500 }}>{label}</div>
        {hint && <div style={{ fontSize: 11, color: 'var(--muted)' }}>{hint}</div>}
      </div>
      <button
        className={`btn small ${value ? 'primary' : ''}`}
        onClick={() => onChange(!value)}
      >
        {value ? 'ON' : 'OFF'}
      </button>
    </div>
  );
}

function ComponentRow({ name, comp }) {
  if (!comp) return null;
  const conf = num(comp.confidence) * 100;
  const action = comp.action || 'HOLD';
  const tone = action.startsWith('BUY') ? 'pos' : action.startsWith('SELL') ? 'neg' : '';
  return (
    <tr>
      <td><strong style={{ textTransform: 'capitalize' }}>{name}</strong></td>
      <td className={tone}>{action.replace(/_/g, ' ')}</td>
      <td className="num">{conf.toFixed(0)}%</td>
      <td style={{ color: 'var(--muted)', fontSize: 12 }}>{comp.reason || ''}</td>
    </tr>
  );
}

export default function AIInsights({ ai, onChange, lastBlend }) {
  const cfg = ai || {};
  const set = (patch) => onChange({ ...cfg, ...patch });
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>AI / ML signal blending</h2>
        <span className="panel-sub">claude + lightgbm + rule</span>
      </div>
      <Toggle
        label="Claude narrative analysis"
        value={cfg.claude_enabled}
        onChange={(v) => set({ claude_enabled: v })}
        hint="Reads news + snapshot, returns directional view"
      />
      <Toggle
        label="Local ML model"
        value={cfg.ml_enabled}
        onChange={(v) => set({ ml_enabled: v })}
        hint="LightGBM next-bar probability — needs trained model file"
      />
      <div style={{ marginTop: 14 }}>
        <Slider label="Claude weight" value={cfg.claude_weight} onChange={(v) => set({ claude_weight: v })} />
        <Slider label="ML weight" value={cfg.ml_weight} onChange={(v) => set({ ml_weight: v })} />
      </div>

      {lastBlend && lastBlend.ai_components && (
        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>
            Last blend on <strong>{lastBlend.ticker}</strong>
          </div>
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Action</th>
                <th className="num">Conf</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              <ComponentRow name="rule" comp={lastBlend.ai_components.rule} />
              <ComponentRow name="claude" comp={lastBlend.ai_components.claude} />
              <ComponentRow name="ml" comp={lastBlend.ai_components.ml} />
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
