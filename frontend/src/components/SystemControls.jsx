/**
 * System Controls — Auto-execute / AI Brain / Meta-AI toggles.
 *
 * These determine WHAT THE BOT IS DOING, so they live on the
 * Command Center's "What is it doing?" section. Reads /copilot/briefing
 * (which exposes brain_enabled + meta_enabled + ai_available)
 * and writes via /copilot/brain, /copilot/meta, and /config.
 */
import React, { useCallback, useEffect, useState } from 'react';

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function Toggle({ checked, onChange, disabled }) {
  return (
    <label className="switch" style={{ opacity: disabled ? 0.5 : 1 }}>
      <input
        type="checkbox"
        checked={!!checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="slider" />
    </label>
  );
}

export default function SystemControls() {
  const [brief, setBrief] = useState(null);
  const [config, setConfig] = useState(null);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [b, c] = await Promise.all([
        api('/copilot/briefing'),
        api('/config'),
      ]);
      setBrief(b);
      setConfig(c);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 6000);
    return () => clearInterval(id);
  }, [load]);

  const setAutoExec = async (val) => {
    setBusy('auto');
    try {
      await api('/config', {
        method: 'POST',
        body: JSON.stringify({ ...config, auto_execute: val }),
      });
      await load();
    } catch (e) { setError(e.message); }
    setBusy(null);
  };

  const setBrain = async (val) => {
    setBusy('brain');
    try {
      await api('/copilot/brain', {
        method: 'POST',
        body: JSON.stringify({
          enabled: val,
          web_research: !!brief?.brain_web_research,
        }),
      });
      await load();
    } catch (e) { setError(e.message); }
    setBusy(null);
  };

  const setBrainWeb = async (val) => {
    setBusy('brainweb');
    try {
      await api('/copilot/brain', {
        method: 'POST',
        body: JSON.stringify({
          enabled: !!brief?.brain_enabled,
          web_research: val,
        }),
      });
      await load();
    } catch (e) { setError(e.message); }
    setBusy(null);
  };

  const setMeta = async (val) => {
    setBusy('meta');
    try {
      await api('/copilot/meta', {
        method: 'POST',
        body: JSON.stringify({ enabled: val }),
      });
      await load();
    } catch (e) { setError(e.message); }
    setBusy(null);
  };

  // Low-cost mode — one click flips all three Claude-burning features
  // off (brain, brain web research, meta). When toggled OFF, restores
  // brain + meta to ON, leaves web research OFF (it's rarely worth the
  // cost even in full mode). Useful when daily Anthropic spend is too
  // high and you want to fall back to rule-based strategies + council.
  const setLowCostMode = async (enableLowCost) => {
    setBusy('lowcost');
    try {
      if (enableLowCost) {
        // Save current state so we could restore later (optional UX).
        await api('/copilot/brain', {
          method: 'POST',
          body: JSON.stringify({ enabled: false, web_research: false }),
        });
        await api('/copilot/meta', {
          method: 'POST',
          body: JSON.stringify({ enabled: false }),
        });
      } else {
        await api('/copilot/brain', {
          method: 'POST',
          body: JSON.stringify({ enabled: true, web_research: false }),
        });
        await api('/copilot/meta', {
          method: 'POST',
          body: JSON.stringify({ enabled: true }),
        });
      }
      await load();
    } catch (e) { setError(e.message); }
    setBusy(null);
  };

  if (!brief || !config) {
    return (
      <div className="panel" style={{ fontSize: 12, color: 'var(--muted)' }}>
        {error || 'Loading controls…'}
      </div>
    );
  }

  const aiAvailable = !!brief.ai_available;
  const auto = !!config.auto_execute;
  const brain = !!brief.brain_enabled;
  const brainWeb = !!brief.brain_web_research;
  const meta = !!brief.meta_enabled;
  // Low-cost mode = all three Claude-burning toggles are off.
  const lowCost = !brain && !brainWeb && !meta;

  return (
    <div className="panel panel--system">
      <div className="panel-head">
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>System controls</div>
          <h3 style={{ margin: '4px 0 0' }}>What the bot is doing</h3>
        </div>
        {!aiAvailable && (
          <span className="pill warn" title="No Anthropic API key configured — AI features inactive">
            no AI key
          </span>
        )}
      </div>

      <div style={{ display: 'grid', gap: 0 }}>
        {/* Low-cost mode — master switch that flips brain + meta + web
            research off in one click. Visible at the top so the operator
            can shed Claude burn instantly when the bill spikes. */}
        <div className="toggle-row" style={{
          background: lowCost
            ? 'linear-gradient(135deg, rgba(91, 141, 239, 0.10), rgba(124, 107, 255, 0.06))'
            : 'transparent',
          borderRadius: 8,
          borderLeft: lowCost ? '3px solid var(--accent, #5B8DEF)' : '3px solid transparent',
          paddingLeft: 8,
          marginBottom: 4,
        }}>
          <div style={{ flex: 1 }}>
            <div className="lbl row" style={{ gap: 8 }}>
              <span>💰 Low-cost mode</span>
              <span className={lowCost ? 'pill on' : 'pill off'}>
                {lowCost ? 'on' : 'off'}
              </span>
            </div>
            <div className="hint">
              One click flips AI Brain · Web research · Meta-AI all OFF.
              Rule-based strategies + council still run. Use when daily
              Anthropic spend is too high or during low-conviction sessions.
            </div>
          </div>
          <Toggle
            checked={lowCost}
            onChange={setLowCostMode}
            disabled={!aiAvailable || busy === 'lowcost'}
          />
        </div>

        {/* Auto-execute */}
        <div className="toggle-row">
          <div style={{ flex: 1 }}>
            <div className="lbl row" style={{ gap: 8 }}>
              <span>🤖 Auto-execute</span>
              <span className={auto ? 'pill on' : 'pill off'}>
                {auto ? 'on' : 'off'}
              </span>
            </div>
            <div className="hint">
              When OFF the bot still analyzes but won't submit any orders. Trades will appear as "signal_only" events.
            </div>
          </div>
          <Toggle
            checked={auto}
            onChange={setAutoExec}
            disabled={busy === 'auto'}
          />
        </div>

        {/* AI Brain */}
        <div className="toggle-row">
          <div style={{ flex: 1 }}>
            <div className="lbl row" style={{ gap: 8 }}>
              <span>🧠 AI Brain</span>
              <span className={brain ? 'pill governance' : 'pill off'}>
                {brain ? 'on' : 'off'}
              </span>
              {brain && brainWeb && (
                <span className="pill purple">+ web research</span>
              )}
            </div>
            <div className="hint">
              Fully-autonomous Claude trader. Reasons over the full snapshot and decides directly, beyond the strategy list.
              {!aiAvailable && ' Requires an Anthropic API key.'}
            </div>
          </div>
          <Toggle
            checked={brain}
            onChange={setBrain}
            disabled={!aiAvailable || busy === 'brain'}
          />
        </div>

        {brain && aiAvailable && (
          <div className="toggle-row" style={{ paddingLeft: 28 }}>
            <div style={{ flex: 1 }}>
              <div className="lbl">↳ Web research</div>
              <div className="hint">Let the Brain use live web search when reasoning.</div>
            </div>
            <Toggle
              checked={brainWeb}
              onChange={setBrainWeb}
              disabled={busy === 'brainweb'}
            />
          </div>
        )}

        {/* Meta-AI */}
        <div className="toggle-row">
          <div style={{ flex: 1 }}>
            <div className="lbl row" style={{ gap: 8 }}>
              <span>🧭 Meta-AI</span>
              <span className={meta ? 'pill governance' : 'pill off'}>
                {meta ? 'on' : 'off'}
              </span>
            </div>
            <div className="hint">
              Portfolio-strategist veto + position-size modifier on every analytical decision.
              {!aiAvailable && ' Requires an Anthropic API key.'}
            </div>
          </div>
          <Toggle
            checked={meta}
            onChange={setMeta}
            disabled={!aiAvailable || busy === 'meta'}
          />
        </div>
      </div>
    </div>
  );
}
