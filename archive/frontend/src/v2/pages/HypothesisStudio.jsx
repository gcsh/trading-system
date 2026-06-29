/* MITS Phase 19 Cluster C — Hypothesis Studio v2 (/v2/hypothesis-studio).
 *
 * Complete redesign of /hypothesis-studio. Sections:
 *
 *   STATE banner      — current advisory/apply states of 5 flags
 *   1. Attribution     — 3 tabs (agents / axes / strategies) + recompute
 *   2. Counterfactual  — provenance picker + 3 cards + result history
 *   3. Policy Tuning   — 8 rule rows + approve/rollback + queued banner
 *   4. Weight Adapt.   — 8 agent rows + approve/rollback + queued banner
 *   5. Audit Ribbon    — filter chips + paginated table
 *
 * Approve / Rollback calls reuse the existing API contract:
 *   POST /learning/approve   {table, row_id}
 *   POST /learning/rollback  {table, row_id}
 *
 * All recommendations stay advisory until the operator flips a TB_*
 * env var on EC2. The STATE banner makes that obvious without the
 * operator opening a code file.
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Card, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import AttributionTable from '../components/AttributionTable.jsx';
import PolicyTuningCard from '../components/PolicyTuningCard.jsx';
import AgentWeightCard from '../components/AgentWeightCard.jsx';
import AuditRibbon from '../components/AuditRibbon.jsx';

const FLAG_META = [
  { key: 'decision_rollback_enabled',         label: 'Decision rollback hook',
    env: 'TB_DECISION_ROLLBACK_ENABLED' },
  { key: 'policy_tuning_advisory_enabled',    label: 'Policy tuning — advisory',
    env: 'TB_POLICY_TUNING_ADVISORY_ENABLED' },
  { key: 'policy_tuning_auto_apply_enabled',  label: 'Policy tuning — auto-apply',
    env: 'TB_POLICY_TUNING_AUTO_APPLY_ENABLED' },
  { key: 'adaptive_weights_advisory_enabled', label: 'Weight adapt — advisory',
    env: 'TB_ADAPTIVE_WEIGHTS_ADVISORY_ENABLED' },
  { key: 'adaptive_weights_apply_enabled',    label: 'Weight adapt — apply',
    env: 'TB_ADAPTIVE_WEIGHTS_APPLY_ENABLED' },
];

async function api(path, options = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error(`${path} -> ${r.status} ${t.slice(0, 200)}`);
  }
  return r.json();
}

/* ── State banner ─────────────────────────────────────────────────── */
function StateBanner({ flags }) {
  const anyOn = flags && Object.values(flags).some(v => v === true);
  return (
    <div className="v2-card" style={{
      marginBottom: 16,
      borderColor: anyOn ? 'var(--accent-yellow-dim)' : 'var(--accent-green-dim)',
      background: anyOn
        ? 'rgba(255,215,0,0.04)'
        : 'rgba(0,255,136,0.04)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <span className="v2-stat__label">Learning System State</span>
        <Pill tone={anyOn ? 'warning' : 'success'} size="md">
          {anyOn ? 'PARTIAL ACTIVE' : 'ADVISORY ONLY'}
        </Pill>
      </div>
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 8,
      }}>
        {FLAG_META.map(f => {
          const on = flags?.[f.key] === true;
          return (
            <div key={f.key} title={f.env}
                 style={{
                   display: 'flex', alignItems: 'center', gap: 6,
                   padding: '6px 10px',
                   borderRadius: 6,
                   background: on ? 'rgba(0,255,136,0.08)' : 'var(--bg-secondary)',
                   border: '1px solid ' + (on ? 'var(--accent-green-dim)' : 'var(--border-subtle)'),
                 }}>
              <span style={{
                width: 8, height: 8, borderRadius: 50,
                background: on ? 'var(--accent-green)' : 'var(--text-muted)',
              }}/>
              <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                {f.label}
              </span>
              <Pill tone={on ? 'success' : 'neutral'}>{on ? 'ON' : 'OFF'}</Pill>
            </div>
          );
        })}
      </div>
      <div style={{
        marginTop: 8,
        fontSize: 11, color: 'var(--text-tertiary)',
      }}>
        {anyOn
          ? 'Some adaptive layers are live. Approved recommendations may affect the engine.'
          : 'All flags OFF — system advisory only. Flip via /opt/trading-bot/.env on EC2 to activate.'}
      </div>
    </div>
  );
}

/* ── Section 1 — Attribution (3 tabs) ─────────────────────────────── */
function AttributionSection({ refreshTick, bumpRefresh }) {
  const [tab, setTab] = useState('agents');
  const [agents, setAgents] = useState(null);
  const [axes, setAxes] = useState(null);
  const [strategies, setStrategies] = useState(null);
  const [err, setErr] = useState(null);
  const [recomputing, setRecomputing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [a, x, s] = await Promise.all([
        api('/learning/attribution/agents'),
        api('/learning/attribution/axes'),
        api('/learning/attribution/strategies'),
      ]);
      setAgents(a); setAxes(x); setStrategies(s); setErr(null);
    } catch (e) { setErr(String(e.message || e)); }
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  const onRecompute = async () => {
    setRecomputing(true);
    try {
      await api('/learning/attribution/recompute', { method: 'POST' });
      await load();
      bumpRefresh();
    } catch (e) { setErr(String(e.message || e)); }
    finally { setRecomputing(false); }
  };

  const tabRows =
    tab === 'agents' ? agents?.agents
    : tab === 'axes' ? axes?.axes
    : strategies?.strategies;
  const minN =
    tab === 'agents' ? agents?.min_n
    : tab === 'axes' ? axes?.min_n
    : strategies?.min_n;
  const winDays =
    tab === 'agents' ? agents?.window_days
    : tab === 'axes' ? axes?.window_days
    : strategies?.window_days;

  return (
    <Section
      title="1. Attribution"
      subtitle="Per-agent / per-axis / per-strategy calibration scoreboard."
      actions={
        <button
          onClick={onRecompute}
          disabled={recomputing}
          data-testid="recompute-attribution"
          style={{
            background: 'var(--bg-elevated)',
            color: 'var(--accent-cyan)',
            border: '1px solid var(--accent-cyan-dim)',
            borderRadius: 4,
            padding: '4px 12px',
            fontSize: 11,
            cursor: recomputing ? 'wait' : 'pointer',
          }}>
          {recomputing ? 'Recomputing…' : 'Recompute'}
        </button>
      }
    >
      {err && (
        <div className="v2-alert v2-alert--critical" style={{ marginBottom: 8 }}>{err}</div>
      )}
      <div style={{ display: 'flex', gap: 4, marginBottom: 10 }}>
        {[
          ['agents', `Agents (${agents?.count ?? 0})`],
          ['axes', `Axes (${axes?.count ?? 0})`],
          ['strategies', `Strategies (${strategies?.count ?? 0})`],
        ].map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            data-testid={`attribution-tab-${k}`}
            style={{
              background: tab === k ? 'var(--bg-elevated)' : 'transparent',
              color: tab === k ? 'var(--accent-cyan)' : 'var(--text-tertiary)',
              border: '1px solid ' + (tab === k ? 'var(--accent-cyan-dim)' : 'var(--border-subtle)'),
              borderBottom: 'none',
              borderRadius: '6px 6px 0 0',
              padding: '6px 14px',
              fontSize: 11.5,
              fontWeight: 600,
              cursor: 'pointer',
            }}>
            {label}
          </button>
        ))}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 6 }}>
        Window: {winDays || 90}d · min_n {minN || '—'} · rows below the threshold are dimmed and cannot be approved.
      </div>
      <Card>
        <AttributionTable rows={tabRows || []} onMutate={load} />
      </Card>
    </Section>
  );
}

/* ── Section 2 — Counterfactual ───────────────────────────────────── */
function CounterfactualSection() {
  const [provs, setProvs] = useState([]);
  const [picked, setPicked] = useState('');
  const [bundle, setBundle] = useState(null);
  const [err, setErr] = useState(null);
  const [history, setHistory] = useState({ sizing: [], policy: [], consensus: [] });
  const [busy, setBusy] = useState(null);

  // Picker form state for policy / consensus
  const [policyRule, setPolicyRule] = useState('signal_hold');
  const [consensusAgent, setConsensusAgent] = useState('market');
  const [consensusStance, setConsensusStance] = useState('buy');
  const [consensusConf, setConsensusConf] = useState(0.7);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await api('/decision/provenance?limit=30');
        const items = r?.items || r?.rows || [];
        if (alive) {
          setProvs(items);
          if (items.length && !picked) setPicked(String(items[0].id));
        }
      } catch (e) {
        if (alive) setErr(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const loadBundle = useCallback(async () => {
    if (!picked) { setBundle(null); return; }
    try {
      const b = await api(`/learning/counterfactual/${picked}`);
      setBundle(b);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, [picked]);

  useEffect(() => { loadBundle(); }, [loadBundle]);

  async function runCF(kind, body) {
    setBusy(kind); setErr(null);
    try {
      const r = await api(`/learning/counterfactual/${picked}/recompute`, {
        method: 'POST',
        body: JSON.stringify({ kind, ...body }),
      });
      setHistory(h => ({
        ...h,
        [kind]: [{ ts: new Date().toISOString(), ...r }, ...h[kind].slice(0, 4)],
      }));
      await loadBundle();
    } catch (e) {
      setErr(String(e.message || e));
    } finally { setBusy(null); }
  }

  const wrap = bundle?.counterfactuals || bundle?.bundle || bundle || {};

  return (
    <Section title="2. Counterfactual What-If"
             subtitle="Pick a recent decision; explore sizing / policy / consensus alternatives.">
      {err && <div className="v2-alert v2-alert--critical" style={{ marginBottom: 8 }}>{err}</div>}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 12 }}>
        <span className="v2-stat__label">Decision</span>
        <select
          value={picked}
          onChange={(e) => setPicked(e.target.value)}
          data-testid="cf-decision-picker"
          style={{
            background: 'var(--bg-elevated)', color: 'var(--text-primary)',
            border: '1px solid var(--border-default)', borderRadius: 6,
            padding: '5px 10px', fontSize: 12, minWidth: 280,
          }}
        >
          <option value="">— pick a recent decision —</option>
          {provs.map(p => (
            <option key={p.id} value={p.id}>
              #{p.id} · {p.ticker || '?'} · {p.event_status || '?'}
            </option>
          ))}
        </select>
        {provs.length === 0 && (
          <span style={{ color: 'var(--text-tertiary)', fontSize: 11 }}>
            No provenance rows yet — engine must run at least one cycle.
          </span>
        )}
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px,1fr))',
        gap: 12,
      }}>
        {/* Sizing card */}
        <Card>
          <div className="v2-stat__label">Sizing</div>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4, marginBottom: 8 }}>
            Replay realized P&L across sizing factors 0.25× / 0.5× / 1× / 1.5× / 2×.
          </div>
          <button
            onClick={() => runCF('sizing', { factors: [0.25, 0.5, 1.0, 1.5, 2.0] })}
            disabled={!picked || busy === 'sizing'}
            data-testid="cf-recompute-sizing"
            style={{
              background: 'var(--accent-cyan-dim)', color: 'var(--bg-primary)',
              border: 'none', borderRadius: 4, padding: '6px 14px',
              fontSize: 11.5, fontWeight: 700, cursor: busy === 'sizing' ? 'wait' : 'pointer',
              opacity: !picked ? 0.4 : 1,
            }}
          >{busy === 'sizing' ? 'Recomputing…' : 'Recompute'}</button>
          <div style={{ marginTop: 10, fontSize: 11 }}>
            {wrap?.sizing
              ? <pre className="mono" style={{
                  background: 'var(--bg-secondary)', borderRadius: 4,
                  padding: 8, fontSize: 10.5, overflow: 'auto',
                  margin: 0, color: 'var(--text-secondary)',
                  maxHeight: 140,
                }}>{JSON.stringify(wrap.sizing, null, 2)}</pre>
              : <span style={{ color: 'var(--text-tertiary)' }}>No bundle yet.</span>}
          </div>
          {history.sizing.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div className="v2-stat__label">History</div>
              {history.sizing.map((h, i) => (
                <div key={i} className="mono" style={{
                  fontSize: 10, color: 'var(--text-tertiary)', marginTop: 2,
                }}>
                  {h.ts.slice(11, 19)} · OK
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Policy card */}
        <Card>
          <div className="v2-stat__label">Policy</div>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4, marginBottom: 8 }}>
            Override one rule and see the next blocking factor.
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
            <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>Override rule:</span>
            <select
              value={policyRule}
              onChange={(e) => setPolicyRule(e.target.value)}
              style={{
                background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                border: '1px solid var(--border-default)', borderRadius: 4,
                padding: '3px 8px', fontSize: 11.5,
              }}
            >
              {['signal_hold', 'low_confidence', 'risk_manager_rejected', 'correlation_cap_block', 'iv_too_rich', 'simulator_veto', 'catalyst_gate'].map(r =>
                <option key={r} value={r}>{r}</option>
              )}
            </select>
          </div>
          <button
            onClick={() => runCF('policy', { rule: policyRule })}
            disabled={!picked || busy === 'policy'}
            data-testid="cf-recompute-policy"
            style={{
              background: 'var(--accent-cyan-dim)', color: 'var(--bg-primary)',
              border: 'none', borderRadius: 4, padding: '6px 14px',
              fontSize: 11.5, fontWeight: 700, cursor: busy === 'policy' ? 'wait' : 'pointer',
              opacity: !picked ? 0.4 : 1,
            }}
          >{busy === 'policy' ? 'Recomputing…' : 'Recompute'}</button>
          <div style={{ marginTop: 10, fontSize: 11 }}>
            {wrap?.policy
              ? <pre className="mono" style={{
                  background: 'var(--bg-secondary)', borderRadius: 4,
                  padding: 8, fontSize: 10.5, overflow: 'auto',
                  margin: 0, color: 'var(--text-secondary)',
                  maxHeight: 140,
                }}>{JSON.stringify(wrap.policy, null, 2)}</pre>
              : <span style={{ color: 'var(--text-tertiary)' }}>No bundle yet.</span>}
          </div>
        </Card>

        {/* Consensus card */}
        <Card>
          <div className="v2-stat__label">Consensus</div>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4, marginBottom: 8 }}>
            Force one agent to a different stance/confidence.
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 6, fontSize: 11.5 }}>
            <label>
              <span style={{ color: 'var(--text-tertiary)' }}>Agent:</span>{' '}
              <select value={consensusAgent} onChange={(e) => setConsensusAgent(e.target.value)}
                      style={{ background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                               border: '1px solid var(--border-default)', borderRadius: 4,
                               padding: '2px 6px', fontSize: 11 }}>
                {['market', 'microstructure', 'macro', 'portfolio_risk', 'mechanical_trend', 'thesis_health', 'simulator', 'devils_advocate'].map(a =>
                  <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <label>
              <span style={{ color: 'var(--text-tertiary)' }}>Stance:</span>{' '}
              <select value={consensusStance} onChange={(e) => setConsensusStance(e.target.value)}
                      style={{ background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                               border: '1px solid var(--border-default)', borderRadius: 4,
                               padding: '2px 6px', fontSize: 11 }}>
                {['buy', 'sell', 'hold', 'abstain'].map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label>
              <span style={{ color: 'var(--text-tertiary)' }}>Confidence:</span>{' '}
              <input type="number" min={0} max={1} step={0.05}
                     value={consensusConf}
                     onChange={(e) => setConsensusConf(Number(e.target.value))}
                     style={{ background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                              border: '1px solid var(--border-default)', borderRadius: 4,
                              padding: '2px 6px', fontSize: 11, width: 80 }}/>
            </label>
          </div>
          <button
            onClick={() => runCF('consensus', { agent: consensusAgent, stance: consensusStance, confidence: consensusConf })}
            disabled={!picked || busy === 'consensus'}
            data-testid="cf-recompute-consensus"
            style={{
              background: 'var(--accent-cyan-dim)', color: 'var(--bg-primary)',
              border: 'none', borderRadius: 4, padding: '6px 14px',
              fontSize: 11.5, fontWeight: 700, cursor: busy === 'consensus' ? 'wait' : 'pointer',
              opacity: !picked ? 0.4 : 1,
            }}
          >{busy === 'consensus' ? 'Recomputing…' : 'Recompute'}</button>
          <div style={{ marginTop: 10, fontSize: 11 }}>
            {wrap?.consensus
              ? <pre className="mono" style={{
                  background: 'var(--bg-secondary)', borderRadius: 4,
                  padding: 8, fontSize: 10.5, overflow: 'auto',
                  margin: 0, color: 'var(--text-secondary)',
                  maxHeight: 140,
                }}>{JSON.stringify(wrap.consensus, null, 2)}</pre>
              : <span style={{ color: 'var(--text-tertiary)' }}>No bundle yet.</span>}
          </div>
        </Card>
      </div>
    </Section>
  );
}

/* ── Section 3 — Policy tuning ────────────────────────────────────── */
function PolicyTuningSection({ refreshTick, autoApply }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await api('/learning/policy-tuning');
      setData(d); setErr(null);
    } catch (e) { setErr(String(e.message || e)); }
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  const meta = data?.tunable_rules || [];
  const rows = data?.rows || [];
  const byRule = new Map(rows.map(r => [r.rule_name, r]));

  return (
    <Section title="3. Policy Tuning Advisor"
             subtitle="Threshold recommendations for the 8 tunable policy rules.">
      <div className="v2-alert v2-alert--warning" style={{ marginBottom: 12 }}>
        Auto-apply {autoApply ? 'ON' : 'OFF'} — set <code>TB_POLICY_TUNING_AUTO_APPLY_ENABLED=1</code> on EC2 to activate approved rows.
      </div>
      {err && <div className="v2-alert v2-alert--critical" style={{ marginBottom: 8 }}>{err}</div>}
      {meta.length === 0 && (
        <EmptyState icon="∅" message="No tunable rule meta returned." />
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {meta.map(rm => (
          <PolicyTuningCard
            key={rm.rule_name}
            ruleMeta={rm}
            row={byRule.get(rm.rule_name) || null}
            autoApply={autoApply}
            onMutate={load}
          />
        ))}
      </div>
    </Section>
  );
}

/* ── Section 4 — Weight adaptation ────────────────────────────────── */
function WeightAdaptationSection({ refreshTick, applyEnabled }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await api('/learning/weights');
      setData(d); setErr(null);
    } catch (e) { setErr(String(e.message || e)); }
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  const agents = data?.known_agents || [];
  const base = data?.base_weights || {};
  const rows = data?.rows || [];
  const byAgent = new Map(rows.map(r => [r.agent_name, r]));

  return (
    <Section title="4. Weight Adaptation"
             subtitle="Bayesian-shrunk per-agent multipliers based on calibration vs base.">
      <div className="v2-alert v2-alert--warning" style={{ marginBottom: 12 }}>
        Apply {applyEnabled ? 'ON' : 'OFF'} — set <code>TB_ADAPTIVE_WEIGHTS_APPLY_ENABLED=1</code> on EC2 to activate approved rows.
      </div>
      {err && <div className="v2-alert v2-alert--critical" style={{ marginBottom: 8 }}>{err}</div>}
      {agents.length === 0 && (
        <EmptyState icon="∅" message="No agents returned from /learning/weights." />
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {agents.map(a => (
          <AgentWeightCard
            key={a}
            agentName={a}
            baseWeight={Number(base[a] ?? 1.0)}
            row={byAgent.get(a) || null}
            applyEnabled={applyEnabled}
            onMutate={load}
          />
        ))}
      </div>
    </Section>
  );
}

/* ── Page ─────────────────────────────────────────────────────────── */
export default function HypothesisStudioV2() {
  const [flags, setFlags] = useState(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const f = await api('/learning/flags');
        if (alive) setFlags(f);
      } catch (e) {
        if (alive) setErr(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, [refreshTick]);

  const bumpRefresh = () => setRefreshTick(t => t + 1);

  return (
    <div style={{ padding: 'var(--space-6)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 16, gap: 16 }}>
        <h1 style={{
          fontSize: 'var(--font-size-xl)', fontWeight: 800,
          color: 'var(--text-primary)', margin: 0,
          letterSpacing: '0.02em', textTransform: 'uppercase',
        }}>Hypothesis Studio</h1>
        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>
          Operator review surface for learning attribution + policy / weight tuning.
        </div>
        <button
          onClick={bumpRefresh}
          style={{
            marginLeft: 'auto',
            background: 'var(--bg-elevated)', color: 'var(--accent-cyan)',
            border: '1px solid var(--accent-cyan-dim)', borderRadius: 4,
            padding: '4px 12px', fontSize: 11, cursor: 'pointer',
          }}>Refresh All</button>
      </div>

      {err && (
        <div className="v2-alert v2-alert--critical" style={{ marginBottom: 16 }}>{err}</div>
      )}

      <StateBanner flags={flags} />

      <AttributionSection refreshTick={refreshTick} bumpRefresh={bumpRefresh} />
      <CounterfactualSection />
      <PolicyTuningSection
        refreshTick={refreshTick}
        autoApply={flags?.policy_tuning_auto_apply_enabled === true} />
      <WeightAdaptationSection
        refreshTick={refreshTick}
        applyEnabled={flags?.adaptive_weights_apply_enabled === true} />

      <Section title="5. Operator Audit Log"
               subtitle="Every approve / rollback action is recorded.">
        <Card>
          <AuditRibbon refreshTick={refreshTick} />
        </Card>
      </Section>
    </div>
  );
}
