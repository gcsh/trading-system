/**
 * MITS Phase 16.E — Decision Cockpit (unified per-decision operator page).
 *
 * Reads /decision/cockpit/{identifier} where identifier may be a
 * trade_id, a direct decision_provenance.id, or a ticker symbol. The
 * page composes six stacked panels off one read so the operator can
 * audit any single decision end-to-end without drill-downs:
 *
 *   1. Policy        — BlockingFactors + soft penalty total
 *   2. Council       — ConfidenceBreakdown + per-agent vote grid
 *   3. Chairman memo — decision + kill_condition + structured_why + main_risk
 *   4. Portfolio     — composition + candidate correlation result
 *   5. Decision quality — composite + 4 sub-scores
 *   6. Simulator     — 4-cluster scenario bars (continuation / fake_breakout
 *                      / stop_out / macro_shock)
 *
 * URL patterns:
 *   /decision-cockpit               → operator picks via input
 *   /decision-cockpit/NVDA          → latest decision for NVDA
 *   /decision-cockpit/{trade_id}    → that trade's decision
 *   /decision-cockpit/{decision_id} → direct provenance lookup
 */
import React, { useEffect, useState } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
// MITS Phase 18-FU Gap 13 — formatted panels for the 4 execution
// subkeys (17.B/C/D/E), the 18.B counterfactual bundle, and the 18.A-D
// learning insights digest. Before today, these 6 keys flowed off the
// /decision/cockpit endpoint but were silently dropped on the floor —
// see ExecutionPanel.jsx for the schema each panel expects.
import {
  FillSnapshotPanel,
  SizingChainPanel,
  ChainSelectionPanel,
  ExitPolicyResultPanel,
  CounterfactualsPanel,
  LearningInsightsPanel,
} from '../components/ExecutionPanel.jsx';

async function fetchJson(path) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    const err = new Error(`${path} -> ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function Pill({ tone = 'info', children, title }) {
  const palette = {
    on: { bg: '#064e3b', fg: '#6ee7b7', border: '#10b981' },
    off: { bg: '#1f2937', fg: '#9ca3af', border: '#374151' },
    info: { bg: '#1e3a8a', fg: '#93c5fd', border: '#3b82f6' },
    warn: { bg: '#78350f', fg: '#fcd34d', border: '#f59e0b' },
    danger: { bg: '#7f1d1d', fg: '#fca5a5', border: '#ef4444' },
    purple: { bg: '#4c1d95', fg: '#c4b5fd', border: '#8b5cf6' },
  };
  const c = palette[tone] || palette.info;
  return (
    <span
      title={title || undefined}
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: 12,
        fontSize: 11,
        fontWeight: 600,
        background: c.bg,
        color: c.fg,
        border: `1px solid ${c.border}`,
        marginRight: 4,
      }}>
      {children}
    </span>
  );
}

function PanelHeader({ icon, title, right }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between',
      alignItems: 'center', marginBottom: 12,
    }}>
      <h3 style={{ margin: 0, fontSize: 16, color: '#e5e7eb' }}>
        {icon} {title}
      </h3>
      <div>{right}</div>
    </div>
  );
}

function Panel({ children }) {
  return (
    <div style={{
      background: '#111827',
      borderRadius: 8,
      padding: 16,
      border: '1px solid #1f2937',
      marginBottom: 16,
    }}>
      {children}
    </div>
  );
}

function PolicyPanel({ policy_result }) {
  const eligible = policy_result?.eligible;
  const factors = policy_result?.blocking_factors || [];
  const softTotal = policy_result?.soft_penalties_total_pct || 0;
  return (
    <Panel>
      <PanelHeader
        icon="(P)"
        title="Policy"
        right={
          <>
            <Pill tone={eligible ? 'on' : 'danger'}>
              {eligible ? 'eligible' : 'blocked'}
            </Pill>
            {softTotal > 0 && (
              <Pill tone="warn">soft penalty {softTotal.toFixed(2)}%</Pill>
            )}
          </>
        }
      />
      {factors.length === 0 ? (
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          No blocking factors fired — every registered rule passed.
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 6 }}>
          {factors.map((f, i) => (
            <div key={i} style={{
              padding: '8px 12px',
              background: '#0a0a0a',
              borderRadius: 6,
              border: '1px solid #1f2937',
            }}>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', marginBottom: 4,
              }}>
                <div style={{ fontWeight: 600, color: '#e5e7eb' }}>
                  {f.rule}
                </div>
                <div>
                  <Pill tone={f.severity === 'hard' ? 'danger' : 'warn'}>
                    {f.severity}
                  </Pill>
                  <Pill tone="info">{f.category}</Pill>
                </div>
              </div>
              <div style={{ color: '#9ca3af', fontSize: 12 }}>
                {f.reason}
              </div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

const AXIS_LABELS = {
  market_structure: 'Market structure',
  technical: 'Technical',
  options: 'Options',
  historical_analog: 'Historical analog',
  simulator: 'Simulator',
  macro: 'Macro',
};

function ConfidenceBreakdown({ breakdown }) {
  if (!breakdown || typeof breakdown !== 'object') return null;
  const axes = [
    'market_structure', 'technical', 'options',
    'historical_analog', 'simulator', 'macro',
  ];
  const health = breakdown.axis_health || {};
  const n = breakdown.axis_n || {};
  const composite = breakdown.composite || 0;
  return (
    <div style={{
      marginBottom: 12, padding: 12,
      background: '#0a0a0a', borderRadius: 6,
      border: '1px solid #1f2937',
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        marginBottom: 8, fontSize: 13,
      }}>
        <div style={{ fontWeight: 600, color: '#e5e7eb' }}>
          Confidence breakdown
        </div>
        <div style={{ color: '#9ca3af' }}>
          composite <strong>{Math.round(composite * 100)}%</strong>
        </div>
      </div>
      <div style={{ display: 'grid', gap: 4 }}>
        {axes.map((ax) => {
          const v = Number(breakdown[ax] || 0);
          const pct = Math.max(0, Math.min(100, v * 100));
          const h = health[ax];
          const color = h === 'green' ? '#10b981'
                        : h === 'yellow' ? '#f59e0b'
                        : '#ef4444';
          return (
            <div key={ax} style={{
              display: 'flex', alignItems: 'center',
              gap: 8, fontSize: 12,
            }}>
              <div style={{ minWidth: 140, color: '#9ca3af' }}>
                {AXIS_LABELS[ax]}
              </div>
              <div style={{
                flex: 1, height: 6, background: '#1f2937',
                borderRadius: 3, overflow: 'hidden',
              }}>
                <div style={{
                  width: `${pct}%`, height: '100%', background: color,
                }} />
              </div>
              <div style={{
                minWidth: 42, textAlign: 'right', color: '#e5e7eb',
              }}>
                {pct.toFixed(0)}%
              </div>
              <Pill tone="off" title={`${n[ax] || 0} contributing source(s)`}>
                n={n[ax] || 0}
              </Pill>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CouncilPanel({ council_breakdown }) {
  if (!council_breakdown || !council_breakdown.consensus) {
    return (
      <Panel>
        <PanelHeader icon="(C)" title="Council" />
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          No consensus persisted for this decision.
        </div>
      </Panel>
    );
  }
  const consensus = council_breakdown.consensus;
  const breakdown = consensus.confidence_breakdown;
  const outputs = council_breakdown.agent_outputs || [];
  return (
    <Panel>
      <PanelHeader
        icon="(C)"
        title="Council"
        right={
          <>
            <Pill tone="info">
              stance {consensus.stance}
            </Pill>
            <Pill tone="info">
              {Math.round((consensus.confidence || 0) * 100)}% conf
            </Pill>
            <Pill tone="off">
              disagree {(consensus.disagreement_score || 0).toFixed(2)}
            </Pill>
          </>
        }
      />
      <ConfidenceBreakdown breakdown={breakdown} />
      <div style={{
        display: 'grid', gap: 8,
        gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
      }}>
        {outputs.map((out, i) => (
          <div key={i} style={{
            padding: 10,
            background: '#0a0a0a',
            borderRadius: 6,
            border: '1px solid #1f2937',
            fontSize: 12,
          }}>
            <div style={{
              display: 'flex', justifyContent: 'space-between',
              alignItems: 'center', marginBottom: 4,
            }}>
              <div style={{ fontWeight: 600, color: '#e5e7eb' }}>
                {out.role || out.agent}
              </div>
              <Pill tone={out.stance === 'buy' ? 'on'
                          : out.stance === 'sell' ? 'danger'
                          : 'off'}>
                {out.stance}
              </Pill>
            </div>
            <div style={{ color: '#9ca3af' }}>
              {out.confidence}% conf · weight {(out.weight || 0).toFixed(2)}
            </div>
            {(out.supporting_factors || []).slice(0, 2).map((s, j) => (
              <div key={j} style={{
                marginTop: 4, color: '#9ca3af', fontSize: 11,
              }}>
                + {s}
              </div>
            ))}
            {(out.concerns || []).slice(0, 2).map((s, j) => (
              <div key={j} style={{
                marginTop: 4, color: '#fca5a5', fontSize: 11,
              }}>
                - {s}
              </div>
            ))}
          </div>
        ))}
      </div>
    </Panel>
  );
}

const DECISION_TONE = {
  EXECUTE: 'on',
  SIZE_DOWN: 'purple',
  MONITOR: 'info',
  ABSTAIN: 'off',
};

function ChairmanPanel({ chairman_memo }) {
  if (!chairman_memo || !chairman_memo.decision) {
    return (
      <Panel>
        <PanelHeader icon="(M)" title="Chairman memo" />
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          No chairman memo persisted for this decision.
        </div>
      </Panel>
    );
  }
  const m = chairman_memo;
  const decisionTone = DECISION_TONE[m.decision] || 'info';
  const conf = m.confidence_pct != null
    ? `${m.confidence_pct}%`
    : (m.conviction != null
       ? `${Math.round(m.conviction * 100)}%`
       : '—');
  return (
    <Panel>
      <PanelHeader
        icon="(M)"
        title="Chairman memo"
        right={
          <>
            <Pill tone={decisionTone}>{m.decision}</Pill>
            <Pill tone="info">{conf}</Pill>
            {m.position_size_modifier != null && (
              <Pill tone="purple">
                size × {Number(m.position_size_modifier).toFixed(2)}
              </Pill>
            )}
          </>
        }
      />

      {/* confidence meter */}
      {m.confidence_pct != null && (
        <div style={{ marginBottom: 12 }}>
          <div style={{
            height: 8, background: '#1f2937',
            borderRadius: 4, overflow: 'hidden',
          }}>
            <div style={{
              width: `${Math.max(0, Math.min(100, m.confidence_pct))}%`,
              height: '100%',
              background: m.confidence_pct >= 70 ? '#10b981'
                          : m.confidence_pct >= 50 ? '#06b6d4'
                          : m.confidence_pct >= 30 ? '#f59e0b'
                          : '#ef4444',
            }} />
          </div>
        </div>
      )}

      {/* kill_condition callout */}
      {m.kill_condition && (
        <div style={{
          marginBottom: 12,
          padding: '10px 12px',
          background: '#1f2937',
          borderLeft: '3px solid #ef4444',
          borderRadius: 4,
          fontSize: 13,
          color: '#e5e7eb',
        }}>
          <div style={{
            fontWeight: 600, color: '#fca5a5',
            fontSize: 11, textTransform: 'uppercase',
            letterSpacing: '0.05em', marginBottom: 4,
          }}>
            Kill condition
          </div>
          {m.kill_condition}
        </div>
      )}

      {/* structured_why bullet list */}
      {(m.structured_why || []).length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{
            fontWeight: 600, color: '#10b981',
            fontSize: 11, textTransform: 'uppercase',
            letterSpacing: '0.05em', marginBottom: 6,
          }}>
            Why
          </div>
          <ul style={{
            margin: 0, paddingLeft: 18, color: '#9ca3af',
            fontSize: 13, lineHeight: 1.5,
          }}>
            {m.structured_why.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}

      {/* main_risk warning box */}
      {m.main_risk && (
        <div style={{
          marginBottom: 12,
          padding: '10px 12px',
          background: '#1f2937',
          borderLeft: '3px solid #f59e0b',
          borderRadius: 4,
          fontSize: 13,
          color: '#e5e7eb',
        }}>
          <div style={{
            fontWeight: 600, color: '#fcd34d',
            fontSize: 11, textTransform: 'uppercase',
            letterSpacing: '0.05em', marginBottom: 4,
          }}>
            Main risk
          </div>
          {m.main_risk}
        </div>
      )}

      <div style={{ fontSize: 11, color: '#6b7280' }}>
        {m.evidence_correlation && (
          <>
            evidence: <strong>{m.evidence_correlation}</strong>
            {' · '}
          </>
        )}
        {m.independent_signal_count != null && (
          <>independent signals: <strong>{m.independent_signal_count}</strong></>
        )}
      </div>
    </Panel>
  );
}

function PortfolioPanel({ portfolio_impact }) {
  const pctx = portfolio_impact?.portfolio_context;
  const corr = portfolio_impact?.correlation_cap;
  if (!pctx && !corr) {
    return (
      <Panel>
        <PanelHeader icon="(F)" title="Portfolio impact" />
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          No portfolio context persisted (pre-consensus block).
        </div>
      </Panel>
    );
  }
  return (
    <Panel>
      <PanelHeader
        icon="(F)"
        title="Portfolio impact"
        right={
          corr ? (
            <>
              <Pill tone={
                corr.hard_block ? 'danger'
                : corr.sizing_multiplier < 1 ? 'warn'
                : 'on'
              }>
                {corr.hard_block ? 'blocked'
                  : corr.sizing_multiplier < 1 ? 'soft cap'
                  : 'pass'}
              </Pill>
              <Pill tone="info">
                size × {(corr.sizing_multiplier ?? 1).toFixed(2)}
              </Pill>
            </>
          ) : null
        }
      />
      {pctx && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 4 }}>
            Equity: <strong style={{ color: '#e5e7eb' }}>
              ${Number(pctx.equity || 0).toLocaleString()}
            </strong>
            {pctx.long_pct != null && (
              <>
                {' · '}long <strong style={{ color: '#e5e7eb' }}>
                  {(pctx.long_pct * 100).toFixed(0)}%
                </strong>
              </>
            )}
            {pctx.short_pct != null && (
              <>
                {' · '}short <strong style={{ color: '#e5e7eb' }}>
                  {(pctx.short_pct * 100).toFixed(0)}%
                </strong>
              </>
            )}
          </div>
          {pctx.by_sector && Object.keys(pctx.by_sector).length > 0 && (
            <div style={{ fontSize: 12, color: '#9ca3af' }}>
              By sector:{' '}
              {Object.entries(pctx.by_sector).map(([k, v]) => (
                <span key={k} style={{ marginRight: 10 }}>
                  {k} <strong style={{ color: '#e5e7eb' }}>
                    {(Number(v) * 100).toFixed(0)}%
                  </strong>
                </span>
              ))}
            </div>
          )}
        </div>
      )}
      {corr && (
        <div style={{
          padding: 10, background: '#0a0a0a',
          borderRadius: 6, border: '1px solid #1f2937',
          fontSize: 12,
        }}>
          <div style={{ color: '#9ca3af', marginBottom: 4 }}>
            Correlation cap result
          </div>
          <div style={{ color: '#e5e7eb' }}>
            worst peer:{' '}
            <strong>{corr.worst_peer || 'none'}</strong>
            {' · '}|rho|:{' '}
            <strong>{Math.abs(corr.worst_rho ?? 0).toFixed(3)}</strong>
            {' · '}direction:{' '}
            <strong>{corr.candidate_direction}</strong>
          </div>
          <div style={{ color: '#9ca3af', marginTop: 4 }}>
            {corr.reason}
          </div>
        </div>
      )}
    </Panel>
  );
}

function ScoreBar({ label, value }) {
  const v = value == null ? 0 : Math.max(0, Math.min(100, Number(value)));
  const color = v >= 70 ? '#10b981'
                : v >= 50 ? '#06b6d4'
                : v >= 30 ? '#f97316'
                : '#ef4444';
  return (
    <div style={{
      flex: '1 1 180px', minWidth: 160,
      padding: 12, background: '#0a0a0a',
      borderRadius: 6, border: '1px solid #1f2937',
    }}>
      <div style={{
        fontSize: 11, color: '#9ca3af',
        textTransform: 'uppercase', letterSpacing: '0.05em',
        marginBottom: 4,
      }}>
        {label}
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, color }}>
        {value == null ? '—' : Math.round(v)}
      </div>
      <div style={{
        marginTop: 6, height: 6,
        background: '#1f2937', borderRadius: 3,
      }}>
        <div style={{
          width: `${v}%`, height: '100%',
          background: color, borderRadius: 3,
        }} />
      </div>
    </div>
  );
}

function ScorecardPanel({ decision_quality_score }) {
  if (!decision_quality_score) {
    return (
      <Panel>
        <PanelHeader icon="(S)" title="Decision quality" />
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          No decision quality score persisted for this decision.
        </div>
      </Panel>
    );
  }
  const s = decision_quality_score;
  const compColor = s.composite >= 70 ? '#10b981'
                    : s.composite >= 50 ? '#06b6d4'
                    : s.composite >= 30 ? '#f97316'
                    : '#ef4444';
  return (
    <Panel>
      <PanelHeader
        icon="(S)"
        title="Decision quality"
        right={
          <Pill tone={
            s.composite >= 70 ? 'on'
            : s.composite >= 50 ? 'info'
            : s.composite >= 30 ? 'warn'
            : 'danger'
          }>
            composite {Math.round(s.composite)}
          </Pill>
        }
      />
      <div style={{
        display: 'flex', gap: 12, flexWrap: 'wrap',
      }}>
        <ScoreBar label="Analysis" value={s.analysis_quality} />
        <ScoreBar label="Council" value={s.council_agreement} />
        <ScoreBar label="Risk" value={s.risk_quality} />
        <ScoreBar label="Execution" value={s.execution_quality} />
      </div>
      <div style={{
        marginTop: 12, padding: 10,
        background: '#0a0a0a', borderRadius: 6,
        border: '1px solid #1f2937',
      }}>
        <div style={{
          fontSize: 11, color: '#9ca3af',
          textTransform: 'uppercase', letterSpacing: '0.05em',
          marginBottom: 6,
        }}>
          Composite
        </div>
        <div style={{ fontSize: 28, fontWeight: 700, color: compColor }}>
          {s.composite == null ? '—' : s.composite.toFixed(1)}
        </div>
      </div>
    </Panel>
  );
}

const SCENARIO_TONE = {
  continuation: { color: '#10b981', label: 'Continuation' },
  fake_breakout: { color: '#f59e0b', label: 'Fake breakout' },
  stop_out: { color: '#fb923c', label: 'Stop out' },
  macro_shock: { color: '#ef4444', label: 'Macro shock' },
};

function SimulatorPanel({ simulator_scenarios, simulator_verdict }) {
  const scenarios = simulator_scenarios || [];
  if (!scenarios.length && !simulator_verdict) {
    return (
      <Panel>
        <PanelHeader icon="(V)" title="Simulator scenarios" />
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          No simulator verdict persisted for this decision.
        </div>
      </Panel>
    );
  }
  const verdict = simulator_verdict || {};
  return (
    <Panel>
      <PanelHeader
        icon="(V)"
        title="Simulator scenarios"
        right={
          <>
            {verdict.mode && <Pill tone="info">{verdict.mode}</Pill>}
            {verdict.p_win != null && (
              <Pill tone={verdict.p_win >= 0.5 ? 'on' : 'warn'}>
                p_win {(verdict.p_win * 100).toFixed(0)}%
              </Pill>
            )}
            {verdict.conviction_score != null && (
              <Pill tone="purple">
                conviction {(verdict.conviction_score * 100).toFixed(0)}%
              </Pill>
            )}
            {verdict.reject_reason && (
              <Pill tone="danger" title={verdict.reject_reason}>
                vetoed
              </Pill>
            )}
          </>
        }
      />
      {scenarios.length === 0 ? (
        <div style={{ color: '#9ca3af', fontSize: 13 }}>
          Verdict came from Monte Carlo only — no analog cohort to
          decompose into scenario clusters.
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          {scenarios.map((sc, i) => {
            const tone = SCENARIO_TONE[sc.label] || {
              color: '#9ca3af', label: sc.label,
            };
            const prob = (sc.probability || 0) * 100;
            const payoff = sc.expected_payoff;
            return (
              <div key={i} style={{
                padding: 10, background: '#0a0a0a',
                borderRadius: 6, border: '1px solid #1f2937',
              }}>
                <div style={{
                  display: 'flex', justifyContent: 'space-between',
                  marginBottom: 6, fontSize: 13,
                }}>
                  <div style={{ color: tone.color, fontWeight: 600 }}>
                    {tone.label}
                  </div>
                  <div style={{ color: '#9ca3af' }}>
                    prob <strong style={{ color: '#e5e7eb' }}>
                      {prob.toFixed(1)}%
                    </strong>
                    {payoff != null && (
                      <>
                        {' · '}E[payoff]{' '}
                        <strong style={{
                          color: payoff >= 0 ? '#10b981' : '#ef4444',
                        }}>
                          {payoff >= 0 ? '+' : ''}{Number(payoff).toFixed(2)}
                        </strong>
                      </>
                    )}
                    {sc.n_analogs != null && (
                      <>
                        {' · '}n=<strong style={{ color: '#e5e7eb' }}>
                          {sc.n_analogs}
                        </strong>
                      </>
                    )}
                  </div>
                </div>
                <div style={{
                  height: 6, background: '#1f2937',
                  borderRadius: 3, overflow: 'hidden',
                }}>
                  <div style={{
                    width: `${Math.max(0, Math.min(100, prob))}%`,
                    height: '100%',
                    background: tone.color,
                  }} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function OpportunityPanel({ opportunity_committee }) {
  if (!opportunity_committee) return null;
  const oc = opportunity_committee;
  return (
    <Panel>
      <PanelHeader
        icon="(O)"
        title="Opportunity committee"
        right={
          <Pill tone={oc.approved ? 'on' : 'danger'}>
            {oc.approved ? 'approved' : 'rejected'}
          </Pill>
        }
      />
      {oc.reviewers && oc.reviewers.length > 0 && (
        <div style={{ display: 'grid', gap: 6 }}>
          {oc.reviewers.map((r, i) => (
            <div key={i} style={{
              padding: '6px 10px', fontSize: 12,
              background: '#0a0a0a', borderRadius: 6,
              border: '1px solid #1f2937',
            }}>
              <strong style={{ color: '#e5e7eb' }}>{r.role}</strong>
              {' · '}
              <Pill tone={r.verdict === 'approve' ? 'on' : 'danger'}>
                {r.verdict}
              </Pill>
              <span style={{ color: '#9ca3af', marginLeft: 8 }}>
                {r.rationale}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function CockpitHeader({ data, onPick, picked, setPicked }) {
  const navigate = useNavigate();
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap',
      justifyContent: 'space-between',
      alignItems: 'baseline', marginBottom: 16, gap: 12,
    }}>
      <div>
        <h1 style={{ fontSize: 22, margin: 0 }}>Decision Cockpit</h1>
        {data && (
          <div style={{ fontSize: 12, color: '#9ca3af', marginTop: 4 }}>
            decision #{data.decision_id}
            {data.trade_id != null && (
              <> · trade #{data.trade_id}</>
            )}
            {' · '}
            <strong style={{ color: '#e5e7eb' }}>{data.ticker}</strong>
            {' · '}
            <Pill tone={
              data.event_status === 'submitted' ? 'on'
              : data.event_status === 'decision_stale' ? 'warn'
              : 'off'
            }>
              {data.event_status}
            </Pill>
            {data.decision_timestamp && (
              <span style={{ marginLeft: 6 }}>
                {new Date(data.decision_timestamp).toLocaleString()}
              </span>
            )}
          </div>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {/* MITS Phase 18.E — drill-in to Hypothesis Studio so the
            operator can jump from a per-decision audit straight to
            the per-learning-table approve/rollback console. */}
        <Link to="/hypothesis-studio" title="Open Hypothesis Studio" style={{
          color: '#93c5fd', textDecoration: 'none', fontSize: 12,
          border: '1px solid #1f2937', padding: '6px 12px',
          borderRadius: 6, background: '#0a0a0a',
        }}>
          🔬 Hypothesis Studio →
        </Link>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const v = (picked || '').trim();
            if (v) navigate(`/decision-cockpit/${v}`);
          }}>
          <input
            type="text"
            value={picked || ''}
            onChange={(e) => setPicked(e.target.value)}
            placeholder="trade_id, decision_id, or ticker"
            style={{
              background: '#0a0a0a', color: '#e5e7eb',
              border: '1px solid #1f2937', borderRadius: 6,
              padding: '6px 10px', fontSize: 13, minWidth: 240,
            }}
          />
          <button type="submit" style={{
            marginLeft: 6, background: '#1e3a8a', color: '#93c5fd',
            border: '1px solid #3b82f6', borderRadius: 6,
            padding: '6px 14px', fontSize: 13, cursor: 'pointer',
          }}>
            Open
          </button>
        </form>
      </div>
    </div>
  );
}

export default function DecisionCockpit() {
  const params = useParams();
  const identifier = params.identifier;
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [picked, setPicked] = useState(identifier || '');

  useEffect(() => {
    setPicked(identifier || '');
    if (!identifier) {
      setData(null);
      setErr(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setErr(null);
    fetchJson(`/decision/cockpit/${encodeURIComponent(identifier)}`)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setErr(String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [identifier]);

  return (
    <div style={{ padding: 16 }}>
      <CockpitHeader
        data={data}
        picked={picked}
        setPicked={setPicked}
      />

      {!identifier && (
        <div style={{
          background: '#111827', borderRadius: 8,
          padding: 24, border: '1px solid #1f2937',
          textAlign: 'center', color: '#9ca3af',
        }}>
          Pick a decision: enter a trade id, a decision_provenance id,
          or a ticker symbol (latest decision).
        </div>
      )}

      {loading && (
        <div style={{ color: '#9ca3af', padding: 24 }}>Loading…</div>
      )}

      {err && (
        <div style={{
          background: '#7f1d1d', border: '1px solid #ef4444',
          color: '#fca5a5', borderRadius: 8, padding: 12,
          marginBottom: 12,
        }}>
          {err}
        </div>
      )}

      {data && !loading && (
        <>
          <PolicyPanel policy_result={data.policy_result} />
          <CouncilPanel council_breakdown={data.council_breakdown} />
          <ChairmanPanel chairman_memo={data.chairman_memo} />
          <PortfolioPanel portfolio_impact={data.portfolio_impact} />
          <ScorecardPanel
            decision_quality_score={data.decision_quality_score}
          />
          <SimulatorPanel
            simulator_scenarios={data.simulator_scenarios}
            simulator_verdict={data.simulator_verdict}
          />
          <OpportunityPanel
            opportunity_committee={data.opportunity_committee}
          />
          {/* MITS Phase 18-FU Gap 13 — surface the 4 execution sub-keys
              + counterfactuals + learning insights. Each panel renders
              "no data yet" if its slot is NULL so the cockpit still
              loads cleanly on pre-execution decisions. */}
          <FillSnapshotPanel
            snapshot={data.execution?.fill_snapshot}
          />
          <SizingChainPanel
            chain={data.execution?.sizing_chain}
          />
          <ChainSelectionPanel
            selection={data.execution?.chain_selection}
          />
          <ExitPolicyResultPanel
            result={data.execution?.exit_policy_result}
          />
          <CounterfactualsPanel cf={data.counterfactuals} />
          <LearningInsightsPanel insights={data.learning_insights} />
        </>
      )}
    </div>
  );
}
