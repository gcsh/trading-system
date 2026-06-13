/**
 * Telegram notifier configuration page.
 *
 * Wiring:
 *   GET  /notifications/telegram/status  → connection + queue + errors
 *   GET  /notifications/telegram/config  → current filter config
 *   PUT  /notifications/telegram/config  → save filter config
 *   POST /notifications/telegram/test    → fire a known test message
 *
 * Persisted via `bot_config.telegram_filters` so the operator's tweaks
 * survive a service restart. Defaults come from `Tunables` on first
 * load — env-var overrides apply automatically.
 */
import React, { useCallback, useEffect, useState } from 'react';

const SEVERITIES = ['info', 'success', 'warning', 'danger', 'critical'];

// Drawn from the existing categories the engine emits. Operator can
// pick which ones to silence; selection is multi-select (deny-list).
const CATEGORIES = [
  { value: 'signal',   label: 'signals (entry/exit ideas)' },
  { value: 'order',    label: 'orders (submitted / filled)' },
  { value: 'risk',     label: 'risk (rejected / breaker)' },
  { value: 'system',   label: 'system (warnings)' },
  { value: 'ai',       label: 'AI brain (digests, narrative)' },
];

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...opts,
  });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function StatusChip({ health }) {
  if (!health) return <span className="pill off">loading…</span>;
  const map = {
    enabled: 'pill on',
    disabled: 'pill off',
    degraded: 'pill warn',
  };
  const cls = map[health.status] || 'pill off';
  return <span className={cls}>{health.status}</span>;
}

function HealthPanel({ health, onTest, testing, lastTest }) {
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Bot connection</h2>
        <StatusChip health={health} />
      </div>
      <div style={{ display: 'grid', gap: 8, fontSize: 13 }}>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--muted)' }}>token set</span>
          <span>{health?.token_set ? 'yes' : 'no'}</span>
        </div>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--muted)' }}>chat_id set</span>
          <span>{health?.chat_id_set ? 'yes' : 'no'}</span>
        </div>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--muted)' }}>queue depth</span>
          <span>{health?.queue_depth ?? '—'}</span>
        </div>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--muted)' }}>errors (24h)</span>
          <span>{health?.errors_24h ?? '—'}</span>
        </div>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--muted)' }}>last send</span>
          <span>{health?.last_send_at || 'never'}</span>
        </div>
      </div>
      <div style={{ marginTop: 14 }}>
        <button
          className="btn primary"
          onClick={onTest}
          disabled={testing}
        >
          {testing ? 'sending…' : 'Send test message'}
        </button>
        {lastTest && (
          <div style={{
            fontSize: 12, marginTop: 8,
            color: lastTest.ok ? 'var(--accent)' : 'var(--danger)',
          }}>
            {lastTest.ok
              ? 'test delivered (check your phone)'
              : `test failed${lastTest.enabled
                  ? ''
                  : ' — notifier disabled (no creds configured)'}`}
          </div>
        )}
      </div>
      {!!health?.recent_errors?.length && (
        <details style={{ marginTop: 12 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--muted)' }}>
            Recent errors ({health.recent_errors.length})
          </summary>
          <div style={{ marginTop: 8, display: 'grid', gap: 4 }}>
            {health.recent_errors.map((e, i) => (
              <div key={i} style={{
                fontSize: 11, padding: '6px 8px',
                background: 'var(--panel-2)', borderRadius: 6,
                fontFamily: 'ui-monospace, monospace',
              }}>
                <div style={{ color: 'var(--muted)' }}>{e.timestamp}</div>
                <div>{e.error}</div>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function FilterPanel({ config, setConfig }) {
  if (!config) return <div className="panel col-12">loading filters…</div>;
  const toggleCategory = (cat) => {
    const deny = new Set(config.category_deny_list || []);
    if (deny.has(cat)) deny.delete(cat);
    else deny.add(cat);
    setConfig({ ...config, category_deny_list: Array.from(deny) });
  };
  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Filters</h2>
        <span className="panel-sub">noise control</span>
      </div>

      <label>Minimum severity</label>
      <select
        value={config.min_severity}
        onChange={(e) => setConfig({ ...config, min_severity: e.target.value })}
      >
        {SEVERITIES.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>
      <div className="hint" style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 4 }}>
        Alerts at or above this severity are sent. Choose <code>critical</code> for
        an "only on fire" mode.
      </div>

      <div style={{ marginTop: 16 }}>
        <label>Deny categories</label>
        <div style={{ display: 'grid', gap: 4, marginTop: 4 }}>
          {CATEGORIES.map((c) => {
            const denied = (config.category_deny_list || []).includes(c.value);
            return (
              <label key={c.value} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '4px 8px', cursor: 'pointer',
                background: denied ? 'var(--panel-2)' : 'transparent',
                borderRadius: 6, fontSize: 12.5,
              }}>
                <input
                  type="checkbox"
                  checked={denied}
                  onChange={() => toggleCategory(c.value)}
                />
                <code>{c.value}</code>
                <span style={{ color: 'var(--muted)', fontSize: 11 }}>
                  {c.label}
                </span>
              </label>
            );
          })}
        </div>
      </div>

      <div style={{ marginTop: 16, display: 'grid',
                    gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <div>
          <label>Max per window</label>
          <input
            type="number"
            min="1"
            max="20"
            value={config.rate_limit_per_window}
            onChange={(e) => setConfig({
              ...config,
              rate_limit_per_window: Number(e.target.value),
            })}
          />
        </div>
        <div>
          <label>Window (minutes)</label>
          <input
            type="number"
            min="1"
            max="60"
            value={config.rate_limit_window_minutes}
            onChange={(e) => setConfig({
              ...config,
              rate_limit_window_minutes: Number(e.target.value),
            })}
          />
        </div>
      </div>

      <div style={{ marginTop: 16, display: 'grid',
                    gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <div>
          <label>Quiet hours start</label>
          <input
            type="time"
            value={config.quiet_hours_start}
            onChange={(e) => setConfig({
              ...config,
              quiet_hours_start: e.target.value,
            })}
          />
        </div>
        <div>
          <label>Quiet hours end</label>
          <input
            type="time"
            value={config.quiet_hours_end}
            onChange={(e) => setConfig({
              ...config,
              quiet_hours_end: e.target.value,
            })}
          />
        </div>
      </div>
      <div className="hint" style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 4 }}>
        Only <code>critical</code> alerts pierce quiet hours.
        Timezone: <code>{config.quiet_hours_tz}</code>
      </div>
    </div>
  );
}

export default function TelegramSettings() {
  const [config, setLocalConfig] = useState(null);
  const [original, setOriginal] = useState(null);
  const [health, setHealth] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [testing, setTesting] = useState(false);
  const [lastTest, setLastTest] = useState(null);

  const loadHealth = useCallback(async () => {
    try {
      const h = await api('/notifications/telegram/status');
      setHealth(h);
    } catch { /* silent — page still useful with stale health */ }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const c = await api('/notifications/telegram/config');
      setLocalConfig(c);
      setOriginal(JSON.stringify(c));
    } catch (e) {
      console.error('telegram config load failed', e);
    }
  }, []);

  useEffect(() => {
    loadConfig();
    loadHealth();
    const id = setInterval(loadHealth, 8000);
    return () => clearInterval(id);
  }, [loadConfig, loadHealth]);

  const setConfig = (next) => {
    setLocalConfig(next);
  };

  const save = async () => {
    if (!config) return;
    setSaving(true);
    try {
      const saved = await api('/notifications/telegram/config', {
        method: 'PUT',
        body: JSON.stringify(config),
      });
      setLocalConfig(saved);
      setOriginal(JSON.stringify(saved));
      setSavedAt(new Date());
    } catch (e) {
      console.error('telegram config save failed', e);
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const result = await api('/notifications/telegram/test', {
        method: 'POST',
      });
      setLastTest(result);
      loadHealth();
    } catch (e) {
      setLastTest({ ok: false, enabled: false });
    } finally {
      setTesting(false);
    }
  };

  const dirty = original !== JSON.stringify(config);

  return (
    <div className="grid">
      <div className="panel col-12" style={{
        background: 'var(--panel-2)',
        borderLeft: '3px solid var(--accent)',
      }}>
        <div className="row" style={{ gap: 12, alignItems: 'flex-start' }}>
          <div style={{ flex: 1 }}>
            <h2 style={{ margin: '0 0 6px' }}>Telegram notifier</h2>
            <div style={{ fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.5 }}>
              Operator gets pushed every alert that passes the filters
              below. Credentials live in env (
              <code>TB_TELEGRAM_BOT_TOKEN</code>,
              <code>TB_TELEGRAM_CHAT_ID</code>) — the bot is a graceful
              no-op when either is unset.
            </div>
          </div>
        </div>
      </div>

      <HealthPanel
        health={health}
        onTest={sendTest}
        testing={testing}
        lastTest={lastTest}
      />
      <FilterPanel config={config} setConfig={setConfig} />

      <div className="panel col-12">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            {dirty
              ? 'unsaved changes'
              : savedAt
                ? `saved ${savedAt.toLocaleTimeString()}`
                : 'no pending changes'}
          </div>
          <button
            className="btn primary"
            disabled={!dirty || saving}
            onClick={save}
          >
            {saving ? 'saving…' : 'Save filters'}
          </button>
        </div>
      </div>
    </div>
  );
}
