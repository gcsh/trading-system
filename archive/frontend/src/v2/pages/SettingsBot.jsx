/* MITS Phase 19 Cluster D — Bot Config (/v2/settings/bot).
 *
 * Read-only viewer of /config TUNABLES. Categorises keys into Engine /
 * Risk / Sizing / Strategy / AI / Data / Catalyst panels, with a search
 * bar that filters every panel simultaneously.
 *
 * NOTE: /config is treated as read-only here — no PUT or POST. To change
 * a value, the operator edits /opt/trading-bot/.env and restarts the
 * service. The page does NOT call any edit endpoint.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Pill, Section, EmptyState, AlertBanner,
} from '../../design/Components.jsx';

const POLL_MS = 60_000;

// Group keys → human category + description. New keys we don't know are
// shown under "Other".
const CAT_MAP = {
  // Engine
  paper_mode:               { cat: 'Engine',   desc: 'When true, all execution routes through PaperExecutor (no live broker).' },
  auto_execute:             { cat: 'Engine',   desc: 'Master switch: when off, signals are logged but never sent to the broker.' },
  live_interval_sec:        { cat: 'Engine',   desc: 'Engine cycle period in seconds.' },
  force_run_when_closed:    { cat: 'Engine',   desc: 'Keep running the loop even when the cash market is closed.' },
  paper_cash_override:      { cat: 'Engine',   desc: 'Initial paper-account cash on next fresh_start().' },
  broker:                   { cat: 'Engine',   desc: 'Active broker adapter (local_paper / alpaca_paper / …).' },
  strategy:                 { cat: 'Engine',   desc: 'Default strategy slug used when the council does not pick one.' },
  // Risk
  'risk.max_position_size_usd': { cat: 'Risk', desc: 'Hard cap on USD notional per trade.' },
  'risk.max_open_positions':    { cat: 'Risk', desc: 'Hard cap on simultaneously-open positions.' },
  'risk.daily_loss_limit_usd':  { cat: 'Risk', desc: 'Trip the kill switch if today P&L drops below this (negative) USD.' },
  'risk.stop_loss_pct':         { cat: 'Risk', desc: 'Default stop-loss % per position when exit policy does not override.' },
  'risk.take_profit_pct':       { cat: 'Risk', desc: 'Default take-profit % per position when exit policy does not override.' },
  'risk.max_cash_usage_pct':    { cat: 'Risk', desc: 'Max % of account cash that may be deployed at once.' },
  min_confidence:               { cat: 'Risk', desc: 'Reject signals below this confidence score.' },
  // Strategy / Style
  asset_types:           { cat: 'Strategy', desc: 'Asset classes the engine is allowed to trade.' },
  trade_styles:          { cat: 'Strategy', desc: 'Holding-period styles enabled.' },
  tickers:               { cat: 'Strategy', desc: 'Scan universe.' },
  custom_rules:          { cat: 'Strategy', desc: 'Free-form operator rules forwarded to the AI agents.' },
  options_disabled:      { cat: 'Strategy', desc: 'Hard-disable any options instruments.' },
  // AI
  'ai.claude_enabled':      { cat: 'AI', desc: 'Allow the Claude agent to vote in the council.' },
  'ai.claude_weight':       { cat: 'AI', desc: 'Static weight applied to the Claude agent\'s vote.' },
  'ai.ml_enabled':          { cat: 'AI', desc: 'Allow the ML/Bayesian agent to vote.' },
  'ai.ml_weight':           { cat: 'AI', desc: 'Static weight applied to the ML agent\'s vote.' },
  'ai.brain_enabled':       { cat: 'AI', desc: 'Enable the autonomous brain layer (chairman + composer).' },
  'ai.brain_web_research':  { cat: 'AI', desc: 'Allow the brain to call web research tools.' },
  'ai.meta_enabled':        { cat: 'AI', desc: 'Enable the meta-agent (vote-aggregator with introspection).' },
  anthropic_api_key:        { cat: 'AI', desc: 'Anthropic API key (redacted; only the set/unset flag is shown here).' },
  anthropic_key_set:        { cat: 'AI', desc: 'True if ANTHROPIC_API_KEY is set in the environment.' },
  // Signal sources
  'signal_sources.technical':    { cat: 'Signals', desc: 'Technical-analysis signals (RSI/MACD/breakouts) enabled.' },
  'signal_sources.news':         { cat: 'Signals', desc: 'News-momentum signals enabled.' },
  'signal_sources.fundamentals': { cat: 'Signals', desc: 'Fundamental-quality signals enabled.' },
  'signal_sources.sentiment':    { cat: 'Signals', desc: 'Sentiment-based signals enabled.' },
  // Data
  'data_sources.finnhub':       { cat: 'Data', desc: 'Use Finnhub quote stream.' },
  'data_sources.alpaca_stream': { cat: 'Data', desc: 'Use Alpaca real-time websocket stream.' },
  // Analytics / event-risk
  'analytics.enabled':       { cat: 'Analytics', desc: 'Enable per-trade analytics grading.' },
  'analytics.min_grade':     { cat: 'Analytics', desc: 'Reject trades graded below this letter.' },
  'predictive.enabled':      { cat: 'Analytics', desc: 'Enable the predictive (Bayesian regime) layer.' },
  'predictive.weight':       { cat: 'Analytics', desc: 'Weight of the predictive layer in the final score.' },
  'event_risk.enabled':      { cat: 'Catalyst', desc: 'Enable the event-risk halt (block trading around earnings/FOMC).' },
};

const PANEL_ORDER = ['Engine', 'Risk', 'Sizing', 'Strategy', 'Signals', 'AI', 'Data', 'Analytics', 'Catalyst', 'Other'];

/* ── flatten /config into row objects ──────────────────────────────── */
function flatten(obj, prefix = '') {
  const out = [];
  for (const [k, v] of Object.entries(obj || {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      out.push(...flatten(v, key));
    } else {
      out.push({ key, value: v });
    }
  }
  return out;
}

function typeOf(v) {
  if (v === null || v === undefined) return 'null';
  if (Array.isArray(v)) return 'list';
  if (typeof v === 'boolean') return 'bool';
  if (typeof v === 'number') return Number.isInteger(v) ? 'int' : 'float';
  return typeof v;
}

function fmtValue(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (Array.isArray(v)) return v.length === 0 ? '[]' : v.join(', ');
  if (typeof v === 'string') {
    if (v.length > 80) return v.slice(0, 77) + '…';
    return v || '""';
  }
  return String(v);
}

function maskSensitive(key, value) {
  const lower = key.toLowerCase();
  if (lower.includes('key') || lower.includes('secret') || lower.includes('token')) {
    if (typeof value === 'string' && value.length > 0) return '••••••••';
  }
  return value;
}

/* ── Panel ─────────────────────────────────────────────────────────── */
function ConfigPanel({ title, rows, query }) {
  const filtered = useMemo(() => {
    if (!query) return rows;
    const q = query.toLowerCase();
    return rows.filter(r =>
      r.key.toLowerCase().includes(q)
      || (r.desc || '').toLowerCase().includes(q)
      || String(r.value).toLowerCase().includes(q)
    );
  }, [rows, query]);

  if (filtered.length === 0) return null;
  return (
    <Card>
      <h3 className="v2-cfg-h3">
        {title}
        <span className="v2-cfg-h3-sub mono">({filtered.length})</span>
      </h3>
      <table className="v2-table v2-cfg-tbl">
        <thead>
          <tr>
            <th>Key</th>
            <th>Type</th>
            <th>Value</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map(r => (
            <tr key={r.key}>
              <td className="mono v2-cfg-tbl__key">{r.key}</td>
              <td><Pill tone="neutral">{r.type}</Pill></td>
              <td className="mono v2-cfg-tbl__val">
                {r.type === 'bool' && r.value === true && <Pill tone="success">true</Pill>}
                {r.type === 'bool' && r.value === false && <Pill tone="error">false</Pill>}
                {r.type !== 'bool' && fmtValue(maskSensitive(r.key, r.value))}
              </td>
              <td className="v2-cfg-tbl__desc">{r.desc || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <style>{`
        .v2-cfg-h3 {
          font-size: var(--font-size-base);
          font-weight: 700;
          color: var(--text-primary);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin: 0 0 var(--space-3);
        }
        .v2-cfg-h3-sub {
          font-size: 11px; color: var(--text-tertiary);
          margin-left: 8px;
          font-weight: 500;
        }
        .v2-cfg-tbl__key {
          color: var(--accent-cyan);
          font-size: 12px;
          max-width: 280px;
          word-break: break-word;
        }
        .v2-cfg-tbl__val { max-width: 320px; word-break: break-word; }
        .v2-cfg-tbl__desc {
          font-size: 12px;
          color: var(--text-tertiary);
          line-height: 1.5;
        }
      `}</style>
    </Card>
  );
}

/* ── Page ──────────────────────────────────────────────────────────── */
export default function SettingsBot() {
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState(null);
  const [query, setQuery] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function fetchCfg() {
      try {
        const r = await fetch('/config');
        if (!r.ok) throw new Error(`${r.status}`);
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('json')) throw new Error('non-JSON');
        const j = await r.json();
        if (!cancelled) { setCfg(j); setErr(null); }
      } catch (e) {
        if (!cancelled) { setCfg(null); setErr(`/config endpoint failed: ${e.message}`); }
      }
    }
    fetchCfg();
    const id = setInterval(fetchCfg, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const rows = useMemo(() => {
    if (!cfg) return [];
    return flatten(cfg).map(r => {
      const meta = CAT_MAP[r.key] || {};
      return {
        ...r,
        type: typeOf(r.value),
        cat: meta.cat || 'Other',
        desc: meta.desc || '',
      };
    });
  }, [cfg]);

  const grouped = useMemo(() => {
    const m = {};
    for (const r of rows) {
      const cat = r.cat || 'Other';
      if (!m[cat]) m[cat] = [];
      m[cat].push(r);
    }
    return m;
  }, [rows]);

  return (
    <div className="v2-root v2-cfg">
      <Section title="Bot Configuration"
               subtitle={cfg ? `${rows.length} tunables` : 'Loading…'}
               actions={
                 <input type="search"
                        placeholder="Filter tunables…"
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                        className="v2-cfg-search"
                        aria-label="Filter tunables" />
               }>
        <AlertBanner severity="info">
          All values are read-only here. To change a value: edit
          <code className="mono"> /opt/trading-bot/.env </code> on EC2 and restart
          <code className="mono"> trading-bot.service</code>. Keys marked with
          <Pill tone="warning" size="sm">high impact</Pill> change live execution.
        </AlertBanner>

        {err && <AlertBanner severity="critical">{err}</AlertBanner>}

        {!cfg && !err && <EmptyState icon="⚙" message="Loading configuration…" />}

        {cfg && PANEL_ORDER
          .filter(cat => grouped[cat] && grouped[cat].length > 0)
          .map(cat => (
            <ConfigPanel
              key={cat}
              title={cat}
              rows={grouped[cat]}
              query={query}
            />
          ))}
      </Section>

      <style>{`
        .v2-cfg { padding: var(--space-4) var(--space-6); }
        .v2-cfg-search {
          background: var(--bg-primary);
          border: 1px solid var(--border-default);
          color: var(--text-primary);
          border-radius: var(--radius-md);
          padding: 6px 12px;
          font-family: var(--font-display);
          font-size: var(--font-size-sm);
          min-width: 240px;
        }
        .v2-cfg-search:focus { outline: none; border-color: var(--accent-cyan); }
      `}</style>
    </div>
  );
}
