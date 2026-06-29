import React, { useCallback, useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import BrokerSelector from '../components/BrokerSelector.jsx';
import SignalToggles from '../components/SignalToggles.jsx';
import AssetToggle from '../components/AssetToggle.jsx';
import { num } from '../lib/format.js';

function AIKeyPanel({ config, updateConfig }) {
  const [key, setKey] = useState('');
  const [saved, setSaved] = useState(false);
  const connected = !!config.anthropic_key_set;
  const save = () => {
    const k = key.trim();
    if (!k) return;
    updateConfig({ anthropic_api_key: k });
    setKey('');
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  };
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>🤖 AI Copilot & Brain</h2>
        <span className="panel-sub">Anthropic (Claude) API key</span>
      </div>
      <div className="hint" style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 10 }}>
        Powers the live chat copilot and the autonomous AI Brain. Stored locally on your machine and used at runtime — no restart needed.
      </div>
      <label>
        API key {connected
          ? <span style={{ color: 'var(--accent)' }}>· connected ✓</span>
          : <span style={{ color: 'var(--muted)' }}>· not connected</span>}
      </label>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder={connected ? '•••••••• (set — type to replace)' : 'sk-ant-…'}
          style={{ flex: 1 }}
        />
        <button className="btn primary" onClick={save} disabled={!key.trim()}>Save</button>
      </div>
      {saved && <div style={{ fontSize: 11.5, color: 'var(--accent)', marginTop: 6 }}>Saved — the copilot is live.</div>}
      <a href="https://console.anthropic.com/" target="_blank" rel="noreferrer" style={{ fontSize: 11, color: 'var(--info)', display: 'inline-block', marginTop: 8 }}>Get an API key →</a>
    </div>
  );
}

function LiveSettings({ config, updateConfig }) {
  const interval = num(config.live_interval_sec, 30);
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Live loop</h2>
        <span className="panel-sub">how often run_cycle fires</span>
      </div>
      <label>Cycle interval (seconds)</label>
      <input
        type="number"
        min="10"
        max="3600"
        step="5"
        value={interval}
        onChange={(e) => updateConfig({ live_interval_sec: Number(e.target.value) })}
      />
      <div className="hint" style={{ marginTop: 6, fontSize: 11.5, color: 'var(--muted)' }}>
        Default 30s. Lower = more reactive but more API load. Restart the bot after changing.
      </div>

      <div style={{ marginTop: 18 }}>
        <label>Minimum confidence to act</label>
        <input
          type="number"
          step="0.05"
          min="0"
          max="1"
          value={num(config.min_confidence, 0.6)}
          onChange={(e) => updateConfig({ min_confidence: Number(e.target.value) })}
        />
        <div className="hint" style={{ marginTop: 6, fontSize: 11.5, color: 'var(--muted)' }}>
          0–1. Signals below this are reported but not acted on.
        </div>
      </div>
    </div>
  );
}

function CouncilContractPanel() {
  const [contract, setContract] = useState(null);
  const [copied, setCopied] = useState(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch('/agents/contract');
      if (r.ok) setContract(await r.json());
    } catch (e) { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const copy = (env, value) => {
    const cmd = `export ${env}=${value}`;
    navigator.clipboard?.writeText(cmd).then(() => {
      setCopied(env);
      setTimeout(() => setCopied(null), 1500);
    });
  };

  if (!contract) return null;

  const auth = contract.chairman_authoritative;
  const knobs = [
    {
      label: 'Chairman authoritative',
      env: contract.chairman_authoritative_env,
      value: auth,
      display: auth ? 'true · Chairman drives decisions' : 'false · shadow mode (legacy decides)',
      pill: auth ? 'pill on' : 'pill off',
      hint: 'When ON, the engine consumes chairman_report["decision"] instead of the legacy recommendation. Watch the Shadow Comparison page before flipping.',
    },
    {
      label: 'Min confidence for contribution',
      env: contract.min_confidence_env,
      value: contract.min_confidence_for_contribution,
      display: `${(contract.min_confidence_for_contribution * 100).toFixed(0)}%`,
      pill: 'pill governance',
      hint: 'Floor below which an agent may emit empty key_drivers and set reasoning_type=insufficient_signal (i.e., go silent).',
    },
    {
      label: 'Agent quorum minimum',
      env: contract.agent_quorum_env,
      value: contract.agent_quorum_min,
      display: `${contract.agent_quorum_min} of 5 agents`,
      pill: 'pill purple',
      hint: 'Required non-silent agents (contributing OR dissenting) before any recommendation other than abstain. Below quorum → insufficient_council_quorum.',
    },
  ];

  return (
    <div className="panel panel--governance col-12">
      <div className="panel-head">
        <h2>🎓 Council Contract</h2>
        <div className="row" style={{ gap: 8 }}>
          <span className="pill governance">Stage 20a / b / c</span>
          <span className="pill off">env-only · restart to change</span>
        </div>
      </div>
      <div className="hint" style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 14 }}>
        These three knobs govern the multi-agent council. They live in <code>config.py</code>{' '}
        TUNABLES and are env-overridable — copy the suggested export, paste it into{' '}
        <code>.env</code>, and restart the backend.
      </div>
      <div style={{ display: 'grid', gap: 10 }}>
        {knobs.map((k) => (
          <div key={k.env} className="row" style={{
            justifyContent: 'space-between',
            padding: '12px 14px',
            background: 'var(--panel-2)',
            borderRadius: 10,
            border: '1px solid var(--border)',
            alignItems: 'flex-start',
            gap: 14,
          }}>
            <div style={{ flex: 1 }}>
              <div className="row" style={{ gap: 8 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{k.label}</div>
                <span className={k.pill}>{k.display}</span>
              </div>
              <div style={{ color: 'var(--muted)', fontSize: 11.5, marginTop: 4, lineHeight: 1.5 }}>
                {k.hint}
              </div>
              <div style={{ marginTop: 6, fontSize: 11, color: 'var(--muted-2)' }}>
                env: <code>{k.env}</code>
              </div>
            </div>
            <button
              className="btn small ghost"
              onClick={() => copy(k.env, k.value)}
              style={{ flexShrink: 0 }}
            >
              {copied === k.env ? '✓ copied' : 'copy export'}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function Settings() {
  const { config, updateConfig } = useOutletContext();
  return (
    <div className="grid">
      <BrokerSelector
        broker={config.broker}
        paperCash={config.paper_cash_override}
        minConfidence={config.min_confidence}
        onChange={(patch) => updateConfig(patch)}
      />
      <LiveSettings config={config} updateConfig={updateConfig} />
      <AIKeyPanel config={config} updateConfig={updateConfig} />
      <SignalToggles
        value={config.signal_sources}
        onChange={(signal_sources) => updateConfig({ signal_sources })}
      />
      <AssetToggle
        assetTypes={config.asset_types}
        tradeStyles={config.trade_styles}
        tickers={config.tickers}
        onChange={(patch) => updateConfig(patch)}
      />
      <CouncilContractPanel />
    </div>
  );
}
