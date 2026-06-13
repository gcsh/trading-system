/**
 * Stage-20-UI · Council Overview.
 *
 * Cross-trade view of the council:
 *   • Live MarketInternalsScore (read off the most recent consensus)
 *   • Last N Chairman decisions across recent trades
 *   • Quorum / silent rate / dissent rate aggregates
 *   • Per-agent dynamic weight + recent stance distribution
 *
 * No new backend — all reads come from /trades/list + /agents/consensus/{id}
 * + /agents/list + /agents/weights endpoints we already have.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const DECISION_CLASS = {
  EXECUTE: 'decision-tag execute',
  SIZE_DOWN: 'decision-tag size-down',
  MONITOR: 'decision-tag monitor',
  ABSTAIN: 'decision-tag abstain',
};

const VERDICT_PILL = {
  risk_on: 'pill on',
  risk_off: 'pill danger',
  mixed: 'pill warn',
  unknown: 'pill off',
};

function CategoryBar({ label, value }) {
  if (value == null) {
    return (
      <div className="gauge">
        <div className="gauge-label">
          <span>{label}</span><span style={{ color: 'var(--muted)' }}>—</span>
        </div>
        <div className="gauge-track"><div className="gauge-fill" style={{ width: 0 }} /></div>
      </div>
    );
  }
  const pct = Math.abs(value) * 100;
  const positive = value >= 0;
  return (
    <div className={`gauge ${positive ? '' : 'danger'}`}>
      <div className="gauge-label">
        <span>{label}</span>
        <span className={positive ? 'pos' : 'neg'}>
          {value >= 0 ? '+' : ''}{value.toFixed(2)}
        </span>
      </div>
      <div className="gauge-track">
        <div className="gauge-fill" style={{
          width: `${pct}%`,
          background: positive
            ? 'linear-gradient(90deg, var(--accent), var(--data))'
            : 'linear-gradient(90deg, var(--danger), var(--heat))',
        }} />
      </div>
    </div>
  );
}

function MarketInternalsCard({ internals }) {
  if (!internals || !internals.verdict) {
    return (
      <div className="panel panel--data">
        <h3>🌐 Market Internals</h3>
        <div className="empty">
          <div className="title">No internals yet</div>
          <div className="hint">Run a cycle to compute the shared market view.</div>
        </div>
      </div>
    );
  }
  const cats = [
    ['macro_liquidity', 'Macro liquidity'],
    ['credit', 'Credit'],
    ['breadth', 'Breadth'],
    ['positioning', 'Positioning'],
    ['volatility', 'Volatility'],
    ['fundamentals', 'Fundamentals'],
    ['insider_flow', 'Insider flow'],
    ['price_structure', 'Price structure'],
    ['microstructure_flow', 'Microstructure'],
  ];
  return (
    <div className="panel panel--data">
      <div className="panel-head">
        <h2>🌐 Market Internals</h2>
        <div className="row" style={{ gap: 8 }}>
          <span className={VERDICT_PILL[internals.verdict] || 'pill info'}>
            verdict · {internals.verdict.replace('_', ' ')}
          </span>
          <span className="pill data">
            composite {Number(internals.composite ?? 0).toFixed(2)}
          </span>
          <span className="pill info">{internals.sources_available || 0} sources</span>
        </div>
      </div>
      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div>{cats.slice(0, 5).map(([k, l]) => <CategoryBar key={k} label={l} value={internals[k]} />)}</div>
        <div>{cats.slice(5).map(([k, l]) => <CategoryBar key={k} label={l} value={internals[k]} />)}</div>
      </div>
      {(internals.notes || []).length > 0 && (
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)' }}>
          {internals.notes.slice(0, 5).join(' · ')}
        </div>
      )}
    </div>
  );
}

function CouncilDecisionsTable({ decisions }) {
  if (!decisions.length) {
    return (
      <div className="panel panel--governance">
        <h3>🎓 Recent Chairman Decisions</h3>
        <div className="empty">
          <div className="title">No structured consensus yet</div>
          <div className="hint">Restart the backend with Stage 20a/b live, then force a trade.</div>
        </div>
      </div>
    );
  }
  return (
    <div className="panel panel--governance">
      <h3>🎓 Recent Chairman Decisions</h3>
      <div className="scroll" style={{ maxHeight: 420 }}>
        <table>
          <thead>
            <tr>
              <th>Trade</th>
              <th>Ticker</th>
              <th>Decision</th>
              <th>Reason</th>
              <th className="num">Conviction</th>
              <th className="num">Size ×</th>
              <th>Sources</th>
              <th>Dissent</th>
            </tr>
          </thead>
          <tbody>
            {decisions.map((d) => (
              <tr key={d.trade_id}>
                <td>
                  <Link to={`/mission-control?id=${d.trade_id}`}>#{d.trade_id}</Link>
                </td>
                <td><strong>{d.ticker}</strong></td>
                <td><span className={DECISION_CLASS[d.decision] || 'decision-tag abstain'}>{d.decision}</span></td>
                <td style={{ color: 'var(--muted)', fontSize: 12 }}>
                  {(d.reason || '').replace(/_/g, ' ')}
                </td>
                <td className="num">{Math.round((d.conviction || 0) * 100)}%</td>
                <td className="num">{(d.position_size_modifier ?? 1).toFixed(2)}</td>
                <td style={{ fontSize: 11, color: 'var(--muted)' }}>
                  {(d.sources_cited || []).slice(0, 3).join(', ')}
                  {(d.sources_cited || []).length > 3 ? ` +${d.sources_cited.length - 3}` : ''}
                </td>
                <td>
                  {d.primary_dissenter ? (
                    <span className="pill danger" title={`${Math.round((d.dissent_share || 0) * 100)}% panel weight`}>
                      {d.primary_dissenter}
                    </span>
                  ) : <span className="accent-muted">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CouncilStatsCard({ decisions }) {
  const stats = useMemo(() => {
    if (!decisions.length) return null;
    const total = decisions.length;
    const by_decision = {};
    let sumConviction = 0;
    let withDissent = 0;
    let withSilent = 0;
    for (const d of decisions) {
      by_decision[d.decision] = (by_decision[d.decision] || 0) + 1;
      sumConviction += d.conviction || 0;
      if (d.primary_dissenter) withDissent += 1;
      if ((d.silent_agents || []).length > 0) withSilent += 1;
    }
    return {
      total,
      avg_conviction: sumConviction / total,
      by_decision,
      dissent_rate: withDissent / total,
      silent_rate: withSilent / total,
    };
  }, [decisions]);
  if (!stats) return null;
  return (
    <div className="panel panel--governance">
      <h3>📊 Council Behavior · last {stats.total}</h3>
      <div className="kpi-row">
        <div className="kpi">
          <div className="kpi-label">Avg conviction</div>
          <div className="kpi-value">{Math.round(stats.avg_conviction * 100)}%</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Dissent rate</div>
          <div className="kpi-value">{Math.round(stats.dissent_rate * 100)}%</div>
          <div className="kpi-sub">≥ 1 dissenter</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Silent rate</div>
          <div className="kpi-value">{Math.round(stats.silent_rate * 100)}%</div>
          <div className="kpi-sub">≥ 1 agent silent</div>
        </div>
      </div>
      <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {Object.entries(stats.by_decision).map(([dec, n]) => (
          <span key={dec} className={DECISION_CLASS[dec] || 'decision-tag abstain'}>
            {dec} · {n}
          </span>
        ))}
      </div>
    </div>
  );
}

function AgentRosterCard({ agents, weights }) {
  if (!agents.length) return null;
  return (
    <div className="panel panel--intel">
      <h3>🤝 5-Agent Roster</h3>
      <div style={{ display: 'grid', gap: 10 }}>
        {agents.map((a) => {
          const w = weights?.[a.agent];
          return (
            <div key={a.agent} className="row" style={{
              justifyContent: 'space-between',
              padding: '8px 12px',
              background: 'var(--panel-2)',
              borderRadius: 8,
              border: '1px solid var(--border)',
            }}>
              <div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{a.role}</div>
                <div style={{ color: 'var(--muted)', fontSize: 11 }}>{a.agent}</div>
              </div>
              {w != null && (
                <span className={`pill ${w >= 1.0 ? 'on' : w >= 0.5 ? 'warn' : 'danger'}`}>
                  weight {Number(w).toFixed(2)}×
                </span>
              )}
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 10, fontSize: 11, color: 'var(--muted)' }}>
        Dynamic weights tighten when an agent has been right historically, slacken when wrong.
      </div>
    </div>
  );
}

export default function CouncilOverview() {
  const [latestInternals, setLatestInternals] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [agents, setAgents] = useState([]);
  const [weights, setWeights] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const trades = await api('/trades/list?limit=25');
      const [agentsRes, weightsRes] = await Promise.allSettled([
        api('/agents/list'),
        api('/agents/weights'),
      ]);
      setAgents(agentsRes.status === 'fulfilled' ? (agentsRes.value.agents || []) : []);
      setWeights(weightsRes.status === 'fulfilled'
        ? (weightsRes.value.weights || weightsRes.value || {})
        : {});

      // Pull consensus for every recent trade in parallel. The endpoint
      // 404s for trades that have no consensus persisted — silently skip.
      const consResults = await Promise.allSettled(
        trades.map((t) =>
          api(`/agents/consensus/${t.id}`).then((r) => ({ trade: t, consensus: r.consensus }))
        ),
      );

      const rows = [];
      let latest = null;
      for (const r of consResults) {
        if (r.status !== 'fulfilled') continue;
        const { trade, consensus } = r.value;
        if (!consensus) continue;
        const cr = consensus.chairman_report;
        if (cr && Object.keys(cr).length > 0) {
          rows.push({
            trade_id: trade.id,
            ticker: trade.ticker,
            decision: cr.decision,
            reason: cr.decision_reason,
            conviction: cr.conviction,
            position_size_modifier: cr.position_size_modifier,
            sources_cited: cr.sources_cited || [],
            primary_dissenter: cr.dissent?.primary_dissenter,
            dissent_share: cr.dissent?.dissent_share,
            silent_agents: consensus.silent_agents || [],
          });
        }
        if (!latest && consensus.market_internals && Object.keys(consensus.market_internals).length > 0) {
          latest = consensus.market_internals;
        }
      }
      setLatestInternals(latest);
      setDecisions(rows);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div>
      {error && (
        <div className="panel panel--bear" style={{ marginBottom: 16 }}>
          <div className="accent-bear">{error}</div>
        </div>
      )}

      <div className="hero" style={{ marginBottom: 24 }}>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div className="accent-governance" style={{
              fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
              fontWeight: 600, marginBottom: 6,
            }}>Stage 20 · Master Council</div>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              5 agents · 1 Chairman · 1 shared market view
            </h2>
            <div style={{ color: 'var(--muted)', marginTop: 8, fontSize: 13, maxWidth: 640 }}>
              The council reconciles agent votes via Jaccard overlap on source categories so five agents
              citing the same evidence don't count as five confirmations. The Chairman summarizes via
              lossless concatenation — it never invents reasoning the council didn't produce.
            </div>
          </div>
          <button className="btn small" onClick={load} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="grid">
        <div className="col-8" style={{ display: 'grid', gap: 16 }}>
          <MarketInternalsCard internals={latestInternals} />
          <CouncilDecisionsTable decisions={decisions} />
        </div>
        <div className="col-4" style={{ display: 'grid', gap: 16 }}>
          <CouncilStatsCard decisions={decisions} />
          <AgentRosterCard agents={agents} weights={weights} />
        </div>
      </div>
    </div>
  );
}
