/* MITS Phase 19 Stream 3 — Decision Cockpit v2 (/v2/decision/cockpit).
 *
 * Bloomberg-density 6-row layout that renders all 19 keys returned by
 *   /decision/cockpit/{identifier}
 * gracefully — every panel emits an EmptyState when its source key is
 * null/empty.  Existing Phase 18-FU Stream C execution panels are
 * re-used as-is (re-skin happens transparently inside their cards).
 *
 * Routes:
 *   /v2/decision/cockpit              → picker landing (no identifier)
 *   /v2/decision/cockpit/:identifier  → filled page
 *
 * identifier can be trade_id, provenance.id, or ticker.
 */
import React, { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Card, Pill, EmptyState, AlertBanner, Stat, Section,
} from '../../design/Components.jsx';
import useCockpit from '../hooks/useCockpit.js';
import useCounterfactual from '../hooks/useCounterfactual.js';

import PolicyResultPanel           from '../components/PolicyResultPanel.jsx';
import CouncilBreakdownPanel       from '../components/CouncilBreakdownPanel.jsx';
import ChairmanMemoPanel           from '../components/ChairmanMemoPanel.jsx';
import QualityScorePanel           from '../components/QualityScorePanel.jsx';
import SimulatorScenariosPanel     from '../components/SimulatorScenariosPanel.jsx';
import PortfolioImpactPanel        from '../components/PortfolioImpactPanel.jsx';
import CounterfactualWhatIfPanel   from '../components/CounterfactualWhatIfPanel.jsx';
import LearningInsightsV2Panel     from '../components/LearningInsightsV2Panel.jsx';

// Re-use existing Phase 18-FU Stream C execution panels.
import {
  FillSnapshotPanel, SizingChainPanel, ChainSelectionPanel, ExitPolicyResultPanel,
} from '../../components/ExecutionPanel.jsx';

/* ── helpers ─────────────────────────────────────────────────────────── */
function recommendationTone(r) {
  switch ((r || '').toLowerCase()) {
    case 'buy': case 'size_up':  case 'long':  return 'success';
    case 'sell': case 'short':                 return 'error';
    case 'size_down': case 'hold':             return 'warning';
    case 'abstain':                            return 'neutral';
    default:                                   return 'neutral';
  }
}
function qualityTone(v) {
  if (v == null) return 'neutral';
  const n = Number(v);
  if (!Number.isFinite(n)) return 'neutral';
  if (n < 40) return 'error';
  if (n < 60) return 'warning';
  return 'success';
}

/* ── Picker ───────────────────────────────────────────────────────────── */
function CockpitPicker({ provenance, onPick }) {
  const [input, setInput] = useState('');
  const items = Array.isArray(provenance?.items) ? provenance.items : [];

  function go() {
    const q = input.trim();
    if (!q) return;
    onPick(q);
  }

  return (
    <Section
      title="Pick a decision"
      subtitle="search by ticker (latest), provenance ID, or trade ID"
    >
      <Card>
        <div style={{
          display: 'flex', gap: 8, marginBottom: 12,
        }}>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') go(); }}
            placeholder="e.g. AAPL, 6917, 5726"
            style={{
              flex: 1, padding: '8px 10px', fontSize: 13,
              fontFamily: 'var(--font-mono)',
              background: 'var(--bg-primary)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border-default)',
              borderRadius: 4,
            }}
          />
          <button type="button" onClick={go}
            style={{
              background: 'var(--accent-cyan)', color: '#0a0e1a',
              border: 'none', borderRadius: 4, padding: '0 16px',
              fontWeight: 700, cursor: 'pointer', fontSize: 12,
            }}>
            OPEN
          </button>
        </div>
        <div style={{
          fontSize: 11, color: 'var(--accent-cyan)',
          textTransform: 'uppercase', letterSpacing: '0.06em',
          marginBottom: 6,
        }}>Recent decisions ({items.length})</div>
        {items.length === 0 ? (
          <EmptyState message="No recent decisions found in /decision/provenance." />
        ) : (
          <div style={{
            maxHeight: 480, overflowY: 'auto',
            border: '1px solid var(--border-subtle)', borderRadius: 4,
          }}>
            {items.map((it) => (
              <button
                key={it.id}
                type="button"
                onClick={() => onPick(String(it.id))}
                style={{
                  display: 'flex', justifyContent: 'space-between',
                  alignItems: 'center', width: '100%',
                  padding: '6px 10px', fontSize: 12,
                  background: 'transparent', border: 'none',
                  borderBottom: '1px solid var(--border-subtle)',
                  color: 'var(--text-secondary)', cursor: 'pointer',
                  textAlign: 'left', fontFamily: 'var(--font-mono)',
                }}>
                <span style={{
                  width: 60, color: 'var(--accent-cyan)',
                }}>#{it.id}</span>
                <span style={{
                  width: 60, color: 'var(--text-primary)', fontWeight: 600,
                }}>{it.ticker || '—'}</span>
                <span style={{ width: 100 }}>
                  <Pill tone={recommendationTone(it.event_status)}>
                    {it.event_status || '—'}
                  </Pill>
                </span>
                <span style={{
                  flex: 1, textAlign: 'right',
                  color: 'var(--text-tertiary)', fontSize: 11,
                }}>{it.decision_timestamp ? it.decision_timestamp.slice(0, 19).replace('T', ' ') : '—'}</span>
              </button>
            ))}
          </div>
        )}
      </Card>
    </Section>
  );
}

/* ── Header strip ────────────────────────────────────────────────────── */
function CockpitHeader({ cockpit, onChangeIdentifier }) {
  const [input, setInput] = useState('');
  function go(val) {
    const q = (val ?? input).trim();
    if (!q) return;
    onChangeIdentifier(q);
  }
  return (
    <Card>
      <div style={{
        display: 'flex', alignItems: 'center',
        gap: 12, flexWrap: 'wrap',
      }}>
        <div style={{
          fontSize: 13, fontWeight: 700, color: 'var(--accent-cyan)',
          textTransform: 'uppercase', letterSpacing: '0.1em',
          minWidth: 160,
        }}>Decision cockpit</div>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') go(); }}
          placeholder={cockpit ? `current: ${cockpit.ticker || '?'} #${cockpit.decision_id}` : 'identifier (ticker, prov id, trade id)'}
          style={{
            flex: 1, minWidth: 200, padding: '6px 10px', fontSize: 12,
            fontFamily: 'var(--font-mono)',
            background: 'var(--bg-primary)',
            color: 'var(--text-primary)',
            border: '1px solid var(--border-default)',
            borderRadius: 4,
          }}
        />
        <button type="button" onClick={() => go()}
          style={{
            background: 'var(--accent-cyan)', color: '#0a0e1a',
            border: 'none', borderRadius: 4, padding: '6px 14px',
            fontWeight: 700, cursor: 'pointer', fontSize: 11,
          }}>OPEN</button>
        {cockpit && (
          <>
            <span style={{
              fontSize: 11, color: 'var(--text-tertiary)',
            }}>·</span>
            <span style={{
              fontSize: 11, color: 'var(--text-secondary)',
              fontFamily: 'var(--font-mono)',
            }}>
              prov #{cockpit.decision_id} · {cockpit.ticker} · {cockpit.decision_timestamp ? cockpit.decision_timestamp.slice(0, 19).replace('T', ' ') : ''}
            </span>
            <Pill tone={recommendationTone(cockpit.event_status)} size="md">
              {cockpit.event_status || '—'}
            </Pill>
          </>
        )}
      </div>
    </Card>
  );
}

/* ── KPI strip ───────────────────────────────────────────────────────── */
function CockpitKPIStrip({ cockpit }) {
  if (!cockpit) return null;
  const dqs = cockpit.decision_quality_score || {};
  const consensus = cockpit.council_breakdown?.consensus || {};
  const policy = cockpit.policy_result || {};

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
      gap: 8, marginBottom: 12,
    }}>
      <Card>
        <Stat label="Status" value={
          <Pill tone={recommendationTone(cockpit.event_status)} size="md">
            {cockpit.event_status || '—'}
          </Pill>
        } />
      </Card>
      <Card>
        <Stat label="Ticker"
              value={<span className="mono" style={{ fontSize: 18 }}>{cockpit.ticker || '—'}</span>} />
      </Card>
      <Card>
        <Stat label="Quality" value={
          <span style={{
            fontFamily: 'var(--font-mono)', fontSize: 18,
            color: qualityTone(dqs.composite) === 'success' ? 'var(--accent-green)'
                 : qualityTone(dqs.composite) === 'warning' ? 'var(--accent-yellow)'
                 : qualityTone(dqs.composite) === 'error'   ? 'var(--accent-red)'
                 :                                            'var(--text-tertiary)',
          }}>{dqs.composite != null ? Number(dqs.composite).toFixed(1) : '—'}</span>
        } hint="Composite Decision Quality Score (0-100). Higher = better." />
      </Card>
      <Card>
        <Stat label="Consensus" value={
          <Pill tone={recommendationTone(consensus.recommendation)} size="md">
            {consensus.recommendation || '—'}
          </Pill>
        } hint="Aggregated council recommendation across all 8 agents." />
      </Card>
      <Card>
        <Stat label="Policy" value={
          <Pill tone={policy.eligible ? 'success' : 'error'} size="md">
            {policy.eligible ? 'eligible' : 'ineligible'}
          </Pill>
        } hint="Did the policy engine clear this trade through all 30 rules?" />
      </Card>
    </div>
  );
}

/* ── Strategy Matrix mini-panel (top-3 candidates only) ──────────────── */
function StrategyMatrixMini({ matrix }) {
  if (!matrix || !Array.isArray(matrix.candidates) || matrix.candidates.length === 0) {
    return null;
  }
  const top = matrix.candidates.slice(0, 3);
  return (
    <Card>
      <div style={{
        fontSize: 11, color: 'var(--accent-cyan)',
        textTransform: 'uppercase', letterSpacing: '0.06em',
        marginBottom: 6, fontWeight: 600,
      }}>Strategy matrix · top 3</div>
      <div style={{ display: 'grid', gap: 6 }}>
        {top.map((c, i) => (
          <div key={i} style={{
            display: 'grid',
            gridTemplateColumns: '1fr auto auto',
            gap: 6, alignItems: 'center',
            padding: 6, background: 'var(--bg-tertiary)',
            borderRadius: 4, fontSize: 11,
          }}>
            <div>
              <div style={{
                color: 'var(--text-primary)', fontWeight: 600,
              }}>{c.label || c.strategy_name}</div>
              <div style={{
                color: 'var(--text-tertiary)', fontSize: 10,
                fontFamily: 'var(--font-mono)',
              }}>fit {Number(c.fit_score || 0).toFixed(3)} · final {Number(c.final_score || 0).toFixed(3)}</div>
            </div>
            <Pill tone={c.direction === 'long' ? 'success' : c.direction === 'short' ? 'error' : 'neutral'}>
              {c.direction}
            </Pill>
            <span className="mono" style={{
              color: 'var(--text-secondary)',
              fontSize: 10,
            }}>WR {(Number(c.cohort_win_rate || 0) * 100).toFixed(1)}% n={c.cohort_n || 0}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* ── Regime Vector mini-panel ────────────────────────────────────────── */
function RegimeVectorMini({ regime }) {
  if (!regime) return null;
  const dims = ['trend', 'volatility_state', 'iv_rank', 'iv_regime',
                'intraday_regime', 'gamma_state', 'macro_regime'];
  return (
    <Card>
      <div style={{
        fontSize: 11, color: 'var(--accent-cyan)',
        textTransform: 'uppercase', letterSpacing: '0.06em',
        marginBottom: 6, fontWeight: 600,
      }}>Regime vector
        <Pill tone={regime.health === 'green' ? 'success' : regime.health === 'yellow' ? 'warning' : 'error'} size="sm">
          {regime.health}
        </Pill>
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(90px, 1fr))',
        gap: 4, fontSize: 11,
      }}>
        {dims.map((d) => {
          const v = regime[d];
          if (!v) return null;
          const val = typeof v.value === 'object' ? (v.value?.regime || JSON.stringify(v.value)) : v.value;
          return (
            <div key={d} style={{
              padding: 4, background: 'var(--bg-tertiary)',
              borderRadius: 3,
            }}>
              <div style={{
                fontSize: 9, color: 'var(--text-tertiary)',
                textTransform: 'uppercase',
              }}>{d.replace(/_/g, ' ')}</div>
              <div style={{
                color: v.health === 'green' ? 'var(--accent-green)'
                     : v.health === 'yellow' ? 'var(--accent-yellow)'
                     :                         'var(--accent-red)',
                fontFamily: 'var(--font-mono)',
              }}>{String(val)}</div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/* ── Main page ───────────────────────────────────────────────────────── */
export default function V2DecisionCockpit() {
  const { identifier } = useParams();
  const navigate = useNavigate();

  const { cockpit, provenance, loading, error } = useCockpit(identifier);

  // CF hook needs a numeric prov ID. cockpit.decision_id is always numeric.
  const provId = cockpit?.decision_id || null;
  const initialCf = cockpit?.counterfactuals || null;
  const { cf, recompute, computing: cfComputing } = useCounterfactual(provId);

  // Merge cf state (initial from cockpit, then live updates from hook)
  const mergedCf = cf || initialCf;

  const policyBlockers = useMemo(() => {
    const arr = cockpit?.policy_result?.blocking_factors;
    return Array.isArray(arr) ? arr.map(b => b.rule).filter(Boolean) : [];
  }, [cockpit]);
  const knownAgents = useMemo(() => {
    return cockpit?.learning_insights?.active_weight_proposals?.known_agents || [];
  }, [cockpit]);

  function handleChangeIdentifier(q) {
    navigate(`/v2/decision/cockpit/${encodeURIComponent(q)}`);
  }

  /* ── Picker landing ─────────────────────────────────────────────── */
  if (!identifier) {
    return (
      <div style={{ padding: 16, display: 'grid', gap: 12 }}>
        <CockpitHeader cockpit={null} onChangeIdentifier={handleChangeIdentifier} />
        {error && <AlertBanner severity="warning">{error}</AlertBanner>}
        <CockpitPicker provenance={provenance} onPick={handleChangeIdentifier} />
      </div>
    );
  }

  /* ── Filled page ─────────────────────────────────────────────────── */
  return (
    <div style={{ padding: 16, display: 'grid', gap: 12 }}>
      <CockpitHeader cockpit={cockpit} onChangeIdentifier={handleChangeIdentifier} />

      {error && <AlertBanner severity="warning">{error}</AlertBanner>}
      {loading && !cockpit && (
        <Card><EmptyState icon="…" message="Loading cockpit data…" /></Card>
      )}
      {!loading && !cockpit && (
        <Card>
          <EmptyState
            message={`No cockpit data for identifier '${identifier}'. Try a ticker, provenance ID, or trade ID.`}
            action={
              <button type="button" onClick={() => navigate('/v2/decision/cockpit')}
                style={{
                  marginTop: 8, background: 'var(--accent-cyan)', color: '#0a0e1a',
                  border: 'none', borderRadius: 4, padding: '4px 12px',
                  fontWeight: 700, cursor: 'pointer', fontSize: 11,
                }}>← Back to picker</button>
            }
          />
        </Card>
      )}

      {cockpit && (
        <>
          {/* ROW 1 — KPI strip */}
          <CockpitKPIStrip cockpit={cockpit} />

          {/* ROW 2 — Policy, Council, Chairman */}
          <Section title="Decision rationale" subtitle="policy · council · chairman">
            <div style={{
              display: 'grid', gap: 12,
              gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
            }}>
              <PolicyResultPanel     policyResult={cockpit.policy_result} />
              <CouncilBreakdownPanel council={cockpit.council_breakdown} />
              <ChairmanMemoPanel     memo={cockpit.chairman_memo} />
            </div>
          </Section>

          {/* ROW 3 — Quality, Simulator, Portfolio */}
          <Section title="Quality + impact" subtitle="DQS · simulator · portfolio">
            <div style={{
              display: 'grid', gap: 12,
              gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
            }}>
              <QualityScorePanel       dqs={cockpit.decision_quality_score} />
              <SimulatorScenariosPanel scenarios={cockpit.simulator_scenarios}
                                       verdict={cockpit.simulator_verdict} />
              <PortfolioImpactPanel    impact={cockpit.portfolio_impact} />
            </div>
          </Section>

          {/* ROW 3.5 — Regime + Strategy (mini-panels) */}
          <Section title="State context" subtitle="regime · strategy candidates">
            <div style={{
              display: 'grid', gap: 12,
              gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
            }}>
              <RegimeVectorMini   regime={cockpit.regime_vector} />
              <StrategyMatrixMini matrix={cockpit.strategy_matrix} />
            </div>
          </Section>

          {/* ROW 4 — Execution panels (re-use Phase 18-FU Stream C components) */}
          <Section title="Execution provenance" subtitle="Phase 17 fill + sizing + chain + exit">
            <div style={{ display: 'grid', gap: 0 }}>
              <FillSnapshotPanel     snapshot={cockpit.execution?.fill_snapshot} />
              <SizingChainPanel      chain={cockpit.execution?.sizing_chain} />
              <ChainSelectionPanel   selection={cockpit.execution?.chain_selection} />
              <ExitPolicyResultPanel result={cockpit.execution?.exit_policy_result} />
            </div>
          </Section>

          {/* ROW 5 — Learning insights v2 */}
          <Section title="Learning insights" subtitle="Phase 18 attribution · tuning · funnel">
            <LearningInsightsV2Panel insights={cockpit.learning_insights} />
          </Section>

          {/* ROW 6 — Counterfactual what-if */}
          <Section title="Counterfactuals" subtitle="Phase 18.B interactive what-if">
            <CounterfactualWhatIfPanel
              provId={provId}
              cf={mergedCf}
              recompute={recompute}
              computing={cfComputing}
              policyBlockers={policyBlockers}
              knownAgents={knownAgents}
            />
          </Section>
        </>
      )}
    </div>
  );
}
