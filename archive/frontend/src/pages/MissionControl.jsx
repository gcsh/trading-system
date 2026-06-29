/**
 * Stage-11.4 Mission Control — per-trade decision-centric view.
 *
 * Pulls the three Stage-11 surfaces side-by-side for a single trade:
 *   • Agent Consensus  → /agents/consensus/{trade_id}
 *   • Trade Memo       → /memo/trade/{trade_id} (with regenerate fallback)
 *   • Decision Lineage → /lineage/trade/{trade_id}
 *
 * The page is the operator's "30-second decision audit" — one trade in
 * focus, every layer of reasoning visible without drill-downs.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import AgentScorecards from '../components/AgentScorecards.jsx';
import EvidencePanel from '../components/EvidencePanel.jsx';

async function fetchJson(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = new Error(`${path} → ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const STANCE_PILL = {
  buy: 'pill on',
  sell: 'pill danger',
  abstain: 'pill off',
  hold: 'pill info',
};

const RECOMMENDATION_PILL = {
  execute: 'pill on',
  size_down: 'pill purple',
  abstain: 'pill off',
};

// Stage-20b — Chairman decision palette + reason-tag → plain-English copy.
const DECISION_PILL = {
  EXECUTE: 'decision-tag execute',
  SIZE_DOWN: 'decision-tag size-down',
  MONITOR: 'decision-tag monitor',
  ABSTAIN: 'decision-tag abstain',
};

const DECISION_REASON_COPY = {
  insufficient_council_quorum:
    'The council is structurally under-informed — too many agents lacked data to vote.',
  no_structured_votes:
    'This trade was decided before the new contract shipped. No Chairman view available.',
  panel_split:
    'The council split roughly 50/50 — no clear winner.',
  low_conviction:
    'Even the supporters were below the conviction floor.',
  correlated_evidence_thin:
    'All supporters cited the same one or two source categories — not independent confirmation.',
  dissent_or_overlap:
    'Either a meaningful dissenter or overlapping evidence — confidence trimmed.',
  majority_abstain:
    'Most agents abstained — not enough conviction in the council to act.',
  all_contributing_abstain:
    'Every voting agent abstained — no side to take.',
  no_votes:
    'No votes were cast.',
};

// Stage-20a — reasoning_type → small badge + tooltip.
const REASONING_PILL = {
  contributing: { className: 'pill on', label: 'aligned' },
  dissenting: { className: 'pill danger', label: 'dissent' },
  insufficient_signal: { className: 'pill off', label: 'silent' },
  legacy: { className: 'pill info', label: 'legacy' },
};

const RISK_PILL = {
  LOW: 'pill on',
  MEDIUM: 'pill info',
  HIGH: 'pill danger',
  UNKNOWN: 'pill off',
};

const CORRELATION_COPY = {
  independent: 'independent sources',
  mixed: 'partly overlapping',
  correlated: 'highly correlated — fewer independent signals than agents',
};

const CONFIDENCE_PILL = {
  very_high: 'pill on',
  high: 'pill info',
  medium: 'pill purple',
  low: 'pill off',
};

function Pill({ className = 'pill info', children }) {
  return <span className={className}>{children}</span>;
}

function ConfidenceBar({ value }) {
  const pct = Math.round((Number(value) || 0) * 100);
  return (
    <div style={{
      background: 'var(--panel-2)', borderRadius: 6, overflow: 'hidden',
      height: 6, width: 80,
    }}>
      <div style={{
        width: `${pct}%`, height: '100%',
        background: 'var(--accent)',
      }} />
    </div>
  );
}

function VoteCard({ vote }) {
  const reasoning_type = vote.reasoning_type || 'legacy';
  const rt = REASONING_PILL[reasoning_type] || REASONING_PILL.legacy;
  const drivers = vote.key_drivers || [];
  const invalidators = vote.invalidators || [];
  const risk = vote.risk_level || 'UNKNOWN';
  return (
    <div className="panel" style={{ padding: 12 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
        <div style={{ fontWeight: 600 }}>{vote.role}</div>
        <div className="row" style={{ gap: 6 }}>
          <Pill className={STANCE_PILL[vote.stance] || 'pill info'}>{vote.stance}</Pill>
          <Pill className={rt.className} title={`reasoning: ${reasoning_type}`}>{rt.label}</Pill>
        </div>
      </div>
      <div className="row" style={{ gap: 8, marginTop: 6, alignItems: 'center' }}>
        <ConfidenceBar value={vote.confidence} />
        <span style={{ color: 'var(--muted)', fontSize: 12 }}>
          {(vote.confidence * 100).toFixed(0)}% conf · w={vote.weight.toFixed(1)}
          {risk && risk !== 'UNKNOWN' ? (
            <> · <Pill className={RISK_PILL[risk] || 'pill info'}>{risk}</Pill></>
          ) : null}
        </span>
      </div>
      <div style={{ marginTop: 8, fontSize: 13, color: 'var(--muted)', lineHeight: 1.4 }}>
        {vote.reasoning}
      </div>
      {drivers.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 12 }}>
          <div style={{ color: 'var(--accent)', fontWeight: 600, marginBottom: 4 }}>
            Drivers
          </div>
          <ul style={{ margin: 0, paddingLeft: 16, color: 'var(--muted)', lineHeight: 1.5 }}>
            {drivers.map((d, i) => (
              <li key={i} title={`category: ${d.source_category} · direction: ${d.direction} · weight: ${d.weight?.toFixed?.(2)}`}>
                {d.description}
                {d.time_sensitive ? ' ⚡' : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
      {invalidators.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 11, color: 'var(--muted)' }}>
          <span style={{ fontWeight: 600 }}>Flips if: </span>
          {invalidators.join(' · ')}
        </div>
      )}
    </div>
  );
}

function ChairmanPanel({ consensus }) {
  // Empty-state path: trade has no persisted consensus at all
  // (engine path that bypassed agents, or pre-Stage-11.3 trade).
  if (!consensus) {
    return (
      <div className="panel panel--governance">
        <h3 style={{ margin: 0 }}>🎓 Chairman</h3>
        <div style={{ color: 'var(--muted)', marginTop: 6, fontSize: 13 }}>
          No consensus persisted for this trade — engine path bypassed agents.
        </div>
      </div>
    );
  }
  const report = consensus.chairman_report;
  // Pre-20b trades have no chairman_report field at all.
  if (!report || Object.keys(report).length === 0) {
    return (
      <div className="panel">
        <h3 style={{ margin: 0 }}>🎓 Chairman</h3>
        <div style={{ color: 'var(--muted)', marginTop: 6, fontSize: 13 }}>
          No Chairman report — this trade predates Stage 20b.
        </div>
      </div>
    );
  }

  const decision = report.decision || 'ABSTAIN';
  const reason = report.decision_reason || '';
  const conviction = report.conviction || 0;
  const sizeMod = report.position_size_modifier;
  const dissent = report.dissent || {};
  const overlapPct = (report.overlap_coefficient || 0) * 100;
  const correlation = report.evidence_correlation || 'independent';
  const sources = report.sources_cited || [];
  const signalCount = report.independent_signal_count || 0;
  const bull = report.bull_case || '';
  const bear = report.bear_case || '';
  const critical = report.critical_risk || '';
  const whyNow = report.why_now || '';
  const disagreementAxes = report.disagreement_axes || [];
  const quorumLine = consensus.quorum_required != null
    ? `Quorum ${consensus.quorum_count || 0}/${consensus.quorum_required}${consensus.quorum_met ? ' ✓' : ' ✗'}`
    : null;
  const reasonCopy = reason && DECISION_REASON_COPY[reason]
    ? DECISION_REASON_COPY[reason]
    : (reason ? reason.replace(/_/g, ' ') : '');

  // Internals (Stage-20a) — also surface verdict if present.
  const internals = consensus.market_internals || {};
  const internalsVerdict = internals.verdict;
  const internalsSources = internals.sources_available;

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>🎓 Chairman</h3>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <Pill className={DECISION_PILL[decision] || 'pill info'}>
            decision · {decision}
          </Pill>
          <Pill className="pill info">{Math.round(conviction * 100)}% conviction</Pill>
          {sizeMod != null && (
            <Pill className="pill purple">size × {Number(sizeMod).toFixed(2)}</Pill>
          )}
          {quorumLine && (
            <Pill className={consensus.quorum_met ? 'pill on' : 'pill off'}>{quorumLine}</Pill>
          )}
        </div>
      </div>

      {reasonCopy && (
        <div style={{
          marginBottom: 12, fontSize: 13, color: 'var(--muted)',
          borderLeft: '3px solid var(--accent)', paddingLeft: 10,
        }}>
          {reasonCopy}
        </div>
      )}

      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 10 }}>
        <div>
          <div style={{ color: 'var(--accent)', fontWeight: 600, marginBottom: 4 }}>
            What's working for it
          </div>
          <div style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
            {bull || '—'}
          </div>
        </div>
        <div>
          <div style={{ color: 'var(--danger)', fontWeight: 600, marginBottom: 4 }}>
            What's working against it
          </div>
          <div style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
            {bear || '—'}
          </div>
        </div>
      </div>

      <div style={{ marginBottom: 10, fontSize: 12, color: 'var(--muted)' }}>
        <div>
          <span style={{ fontWeight: 600 }}>Independent signals: </span>
          {signalCount}
          {' · '}
          <span style={{ fontWeight: 600 }}>overlap: </span>
          {overlapPct.toFixed(0)}% ({CORRELATION_COPY[correlation] || correlation})
        </div>
        {sources.length > 0 && (
          <div style={{ marginTop: 4 }}>
            <span style={{ fontWeight: 600 }}>Sources cited: </span>
            {sources.join(', ')}
          </div>
        )}
        {dissent.primary_dissenter && (
          <div style={{ marginTop: 4 }}>
            <span style={{ fontWeight: 600 }}>Primary dissenter: </span>
            {dissent.primary_dissenter}
            {' '}({Math.round((dissent.dissent_share || 0) * 100)}% of the panel weight)
          </div>
        )}
        {(consensus.silent_agents || []).length > 0 && (
          <div style={{ marginTop: 4 }}>
            <span style={{ fontWeight: 600 }}>Silent (no signal): </span>
            {consensus.silent_agents.join(', ')}
          </div>
        )}
      </div>

      {(critical || whyNow) && (
        <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 8 }}>
          {critical && (
            <div style={{ fontSize: 12 }}>
              <div style={{ color: 'var(--danger)', fontWeight: 600, marginBottom: 4 }}>
                Critical risk
              </div>
              <div style={{ color: 'var(--muted)', lineHeight: 1.5 }}>{critical}</div>
            </div>
          )}
          {whyNow && (
            <div style={{ fontSize: 12 }}>
              <div style={{ color: 'var(--accent)', fontWeight: 600, marginBottom: 4 }}>
                Why now ⚡
              </div>
              <div style={{ color: 'var(--muted)', lineHeight: 1.5 }}>{whyNow}</div>
            </div>
          )}
        </div>
      )}

      {disagreementAxes.length > 0 && (
        <div style={{ marginTop: 6 }}>
          <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>Disagreement axes</div>
          <div style={{ display: 'grid', gap: 6 }}>
            {disagreementAxes.map((ax, i) => (
              <div key={i} style={{
                fontSize: 12, color: 'var(--muted)',
                background: 'var(--panel-2)', padding: '6px 10px', borderRadius: 6,
              }}>
                <span style={{ fontWeight: 600 }}>{ax.agent}</span>
                {' '}({Math.round((ax.confidence || 0) * 100)}% conf, voted {ax.stance}):
                {' '}{ax.reasoning}
              </div>
            ))}
          </div>
        </div>
      )}

      {internalsVerdict && (
        <div style={{ marginTop: 10, fontSize: 11, color: 'var(--muted)' }}>
          Shared market view: <strong>{internalsVerdict}</strong>
          {internalsSources ? ` · ${internalsSources} sources` : ''}
        </div>
      )}
    </div>
  );
}

const AXIS_HEALTH_COLOR = {
  green: '#3aaa64',
  yellow: '#d4a72c',
  red: '#c8503a',
};

const AXIS_LABELS = {
  market_structure: 'Market structure',
  technical: 'Technical',
  options: 'Options',
  historical_analog: 'Historical analog',
  simulator: 'Simulator',
  macro: 'Macro',
};

function ConfidenceBreakdownPanel({ breakdown }) {
  if (!breakdown || Object.keys(breakdown).length === 0) return null;
  const axes = ['market_structure', 'technical', 'options',
                'historical_analog', 'simulator', 'macro'];
  const health = breakdown.axis_health || {};
  const n = breakdown.axis_n || {};
  return (
    <div style={{
      marginBottom: 12,
      padding: 10,
      background: 'var(--panel-2)',
      borderRadius: 6,
      border: '1px solid var(--border)',
    }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ fontWeight: 600 }}>Confidence breakdown</div>
        <div style={{ fontSize: 13 }}>
          Composite: <strong>{Math.round((breakdown.composite || 0) * 100)}%</strong>
        </div>
      </div>
      <div style={{ display: 'grid', gap: 4 }}>
        {axes.map((ax) => {
          const v = Number(breakdown[ax] || 0);
          const pct = Math.max(0, Math.min(100, v * 100));
          const color = AXIS_HEALTH_COLOR[health[ax]] || AXIS_HEALTH_COLOR.red;
          return (
            <div key={ax} className="row" style={{ alignItems: 'center', gap: 8, fontSize: 12 }}>
              <div style={{ minWidth: 130, color: 'var(--muted)' }}>{AXIS_LABELS[ax]}</div>
              <div style={{
                flex: 1,
                height: 8,
                background: 'var(--panel)',
                borderRadius: 4,
                overflow: 'hidden',
              }}>
                <div style={{
                  width: `${pct}%`,
                  height: '100%',
                  background: color,
                }} />
              </div>
              <div style={{ minWidth: 42, textAlign: 'right' }}>{pct.toFixed(0)}%</div>
              <Pill className="pill off" title={`${n[ax] || 0} contributing source(s)`}>n={n[ax] || 0}</Pill>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ConsensusPanel({ consensus }) {
  if (!consensus) {
    return <div className="panel"><div style={{ color: 'var(--muted)' }}>No agent consensus persisted for this trade.</div></div>;
  }
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>🎯 Agent Consensus</h3>
        <div className="row" style={{ gap: 8 }}>
          <Pill className={STANCE_PILL[consensus.stance] || 'pill info'}>stance · {consensus.stance}</Pill>
          <Pill className={RECOMMENDATION_PILL[consensus.recommendation] || 'pill info'}>{consensus.recommendation}</Pill>
          <Pill className="pill purple">size × {consensus.size_multiplier?.toFixed(2)}</Pill>
          <Pill className="pill info">{Math.round(consensus.confidence * 100)}% conf</Pill>
          <Pill className="pill off">disagreement {consensus.disagreement_score?.toFixed(2)}</Pill>
        </div>
      </div>
      <ConfidenceBreakdownPanel breakdown={consensus.confidence_breakdown} />
      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 10 }}>
        {(consensus.votes || []).map((v) => (
          <VoteCard key={v.agent} vote={v} />
        ))}
      </div>
      {(consensus.supporters?.length || consensus.dissenters?.length) ? (
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--muted)' }}>
          Supporters: {consensus.supporters.join(', ') || '—'} ·
          Dissenters: {consensus.dissenters.join(', ') || '—'} ·
          Abstain: {consensus.abstain_count}
        </div>
      ) : null}
    </div>
  );
}

function MemoPanel({ memo, onRegenerate, regenerating }) {
  if (!memo) {
    return (
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0 }}>📝 Trade Memo</h3>
          <button className="btn small" onClick={onRegenerate} disabled={regenerating}>
            {regenerating ? 'Generating…' : 'Generate memo'}
          </button>
        </div>
        <div style={{ color: 'var(--muted)', marginTop: 8 }}>
          No memo persisted for this trade (likely pre-Stage-11). Click "Generate memo" to build one.
        </div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>📝 Trade Memo</h3>
        <div className="row" style={{ gap: 8 }}>
          <Pill className={CONFIDENCE_PILL[memo.confidence] || 'pill info'}>
            {memo.confidence?.replace('_', ' ')}
          </Pill>
          <Pill className="pill info">{memo.source}</Pill>
          <button className="btn small" onClick={onRegenerate} disabled={regenerating}>
            {regenerating ? 'Regenerating…' : 'Regenerate'}
          </button>
        </div>
      </div>
      <div style={{ fontSize: 16, fontWeight: 500, lineHeight: 1.4, marginBottom: 12 }}>
        {memo.thesis}
      </div>
      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
        <div>
          <div style={{ color: 'var(--accent)', fontWeight: 600, marginBottom: 4 }}>Bull case</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--muted)', fontSize: 13 }}>
            {(memo.bull_case || []).map((b, i) => <li key={i}>{b}</li>)}
          </ul>
        </div>
        <div>
          <div style={{ color: 'var(--danger)', fontWeight: 600, marginBottom: 4 }}>Bear case</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--muted)', fontSize: 13 }}>
            {(memo.bear_case || []).map((b, i) => <li key={i}>{b}</li>)}
          </ul>
        </div>
      </div>
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontWeight: 600 }}>Invalidation</div>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>{memo.invalidation}</div>
      </div>
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontWeight: 600 }}>Exit plan</div>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>{memo.exit_plan}</div>
      </div>
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontWeight: 600 }}>Risk factors</div>
        <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--muted)', fontSize: 13 }}>
          {(memo.risk_factors || []).map((r, i) => <li key={i}>{r}</li>)}
        </ul>
      </div>
      <div>
        <div style={{ fontWeight: 600 }}>Regime context</div>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>{memo.regime_context}</div>
      </div>
    </div>
  );
}

const QUALITY_PILL = {
  high: 'pill on',
  mid: 'pill info',
  low: 'pill danger',
  'n/a': 'pill off',
};

function AttributionPanel({ attribution }) {
  if (!attribution || !attribution.attributions?.length) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>🧬 Feature Attribution</h3>
        <div style={{ color: 'var(--muted)' }}>
          No model attribution available — train a model from /ai to see which features mattered.
        </div>
      </div>
    );
  }
  const { attributions, model_version, model_type, method } = attribution;
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>🧬 Feature Attribution</h3>
        <div className="row" style={{ gap: 8 }}>
          {model_version && <Pill className="pill purple">model {model_version}</Pill>}
          {model_type && <Pill className="pill info">{model_type}</Pill>}
          <Pill className={method === 'permutation' ? 'pill on' : 'pill off'}>{method}</Pill>
        </div>
      </div>
      <div style={{ display: 'grid', gap: 6 }}>
        {attributions.map((a) => (
          <div key={a.feature} className="row" style={{ alignItems: 'center', gap: 10 }}>
            <div style={{ minWidth: 140, fontWeight: 600 }}>{a.feature}</div>
            <div style={{
              flex: 1, maxWidth: 240,
              background: 'var(--panel-2)', borderRadius: 4, height: 8,
              overflow: 'hidden',
            }}>
              <div style={{
                width: `${Math.min(100, (a.importance || 0) * 100 * 4)}%`,
                height: '100%', background: 'var(--accent)',
              }} />
            </div>
            <span style={{ color: 'var(--muted)', fontSize: 12, minWidth: 60 }}>
              {(a.importance * 100).toFixed(2)}%
            </span>
            <span style={{ fontSize: 13, minWidth: 80 }}>{
              a.value == null ? '—' :
                typeof a.value === 'number' ? a.value.toFixed(3) : String(a.value)
            }</span>
            <Pill className={QUALITY_PILL[a.quality] || 'pill off'}>{a.quality}</Pill>
          </div>
        ))}
      </div>
    </div>
  );
}

function LineageRow({ stage, value }) {
  const [open, setOpen] = useState(false);
  const empty = value == null || (typeof value === 'object' && Object.keys(value).length === 0);
  return (
    <div style={{
      borderBottom: '1px solid var(--border)',
      padding: '8px 0',
    }}>
      <div
        className="row"
        style={{ justifyContent: 'space-between', alignItems: 'center', cursor: empty ? 'default' : 'pointer' }}
        onClick={() => !empty && setOpen(!open)}
      >
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: 'var(--muted)', minWidth: 16 }}>
            {empty ? '·' : (open ? '▾' : '▸')}
          </span>
          <span style={{ fontWeight: 600, textTransform: 'capitalize' }}>{stage.replace(/_/g, ' ')}</span>
          {empty
            ? <Pill className="pill off">no data</Pill>
            : <Pill className="pill on">present</Pill>}
        </div>
      </div>
      {open && !empty && (
        <pre style={{
          margin: '8px 0 0',
          padding: 10,
          background: 'var(--panel-2)',
          borderRadius: 6,
          maxHeight: 240,
          overflow: 'auto',
          fontSize: 12,
          color: 'var(--text)',
        }}>
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}

const STAGE_ORDER = [
  'signal', 'snapshot', 'regime', 'features', 'confluence', 'probability',
  'rank', 'abstain', 'min_grade_tightened', 'meta_ai', 'portfolio_risk',
  'risk', 'audit', 'consensus', 'memory', 'execution', 'outcome', 'autopsy',
  'cohort', 'memo',
];

function MemoryPanel({ memory }) {
  if (!memory) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>📚 Memory Recall</h3>
        <div style={{ color: 'var(--muted)' }}>
          No analogues found — either no closed trades yet, or no past trade was similar enough.
        </div>
      </div>
    );
  }
  const { matches = [], summary = {} } = memory;
  if (!matches.length) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>📚 Memory Recall</h3>
        <div style={{ color: 'var(--muted)' }}>
          0 analogues. Either no past closed trades yet, or nothing met the similarity threshold.
        </div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>📚 Memory Recall · {summary.matches} similar past {summary.matches === 1 ? 'trade' : 'trades'}</h3>
        <div className="row" style={{ gap: 8 }}>
          {summary.hit_rate != null && (
            <Pill className={summary.hit_rate >= 0.55 ? 'pill on' : (summary.hit_rate <= 0.4 ? 'pill danger' : 'pill info')}>
              hit-rate {Math.round(summary.hit_rate * 100)}%
            </Pill>
          )}
          <Pill className={summary.total_pnl >= 0 ? 'pill on' : 'pill danger'}>
            {summary.total_pnl >= 0 ? '+' : ''}${summary.total_pnl.toFixed(2)} cumulative
          </Pill>
          <Pill className="pill info">avg sim {summary.avg_similarity?.toFixed(2)}</Pill>
        </div>
      </div>
      <div style={{ display: 'grid', gap: 8 }}>
        {matches.map((m) => (
          <div key={m.decision_id} style={{
            padding: '8px 12px',
            background: 'var(--panel-2)',
            borderRadius: 6,
            border: '1px solid var(--border)',
          }}>
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
              <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                <span style={{ fontWeight: 600 }}>
                  {m.trade_id != null ? `#${m.trade_id}` : `decision #${m.decision_id}`}
                </span>
                <span style={{ color: 'var(--muted)' }}>{m.ticker}</span>
                <Pill className="pill info">{m.regime_label}</Pill>
                {m.grade && <Pill className="pill purple">grade {m.grade}</Pill>}
              </div>
              <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                <Pill className={m.win === true ? 'pill on' : (m.win === false ? 'pill danger' : 'pill off')}>
                  {m.win === true ? 'win' : m.win === false ? 'loss' : 'neutral'}
                </Pill>
                <span style={{ color: m.outcome_pnl >= 0 ? 'var(--accent)' : 'var(--danger)', fontWeight: 600 }}>
                  {m.outcome_pnl >= 0 ? '+' : ''}${(m.outcome_pnl || 0).toFixed(2)}
                </span>
                <span style={{ color: 'var(--muted)', fontSize: 12 }}>
                  sim {Math.round(m.similarity * 100)}%
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function LineagePanel({ lineage }) {
  if (!lineage) return null;
  const stages = lineage.stages || {};
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>🔗 Decision Lineage</h3>
      <div style={{ color: 'var(--muted)', fontSize: 13, marginBottom: 12 }}>
        Every gate the bot's reasoning passed through. Click any stage to expand.
      </div>
      <div>
        {STAGE_ORDER.map((s) => (
          <LineageRow key={s} stage={s} value={stages[s]} />
        ))}
      </div>
    </div>
  );
}

function TradePicker({ trades, selected, onSelect, loading }) {
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="row" style={{ alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ fontWeight: 600 }}>Trade</div>
        <select
          value={selected || ''}
          onChange={(e) => onSelect(Number(e.target.value))}
          style={{
            background: 'var(--panel-2)', color: 'var(--text)',
            border: '1px solid var(--border)', borderRadius: 6,
            padding: '6px 10px', minWidth: 320,
          }}
        >
          <option value="">{loading ? 'Loading…' : 'Pick a trade'}</option>
          {trades.map((t) => (
            <option key={t.id} value={t.id}>
              #{t.id} · {t.ticker} {t.action} · {t.strategy || '—'}
              {t.status && t.status !== 'open' ? ` · ${t.status}` : ''}
              {t.pnl != null ? ` · ${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : ''}
            </option>
          ))}
        </select>
        <span style={{ color: 'var(--muted)', fontSize: 12 }}>
          {trades.length} recent trades
        </span>
      </div>
    </div>
  );
}

export default function MissionControl() {
  const [params, setParams] = useSearchParams();
  const initialId = Number(params.get('id')) || null;
  const [trades, setTrades] = useState([]);
  const [tradesLoading, setTradesLoading] = useState(true);
  const [tradeId, setTradeId] = useState(initialId);
  const [consensus, setConsensus] = useState(null);
  const [memo, setMemo] = useState(null);
  const [lineage, setLineage] = useState(null);
  const [memory, setMemory] = useState(null);
  const [attribution, setAttribution] = useState(null);
  const [loading, setLoading] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState(null);

  // Load recent trades for the picker.
  useEffect(() => {
    setTradesLoading(true);
    fetchJson('/trades/list?limit=50')
      .then((rows) => {
        const list = Array.isArray(rows) ? rows : (rows.trades || []);
        setTrades(list);
        if (!tradeId && list.length) {
          setTradeId(list[0].id);
        }
      })
      .catch((e) => setError(e.message))
      .finally(() => setTradesLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync ?id= in URL with selected trade for shareable links.
  useEffect(() => {
    if (tradeId && String(tradeId) !== params.get('id')) {
      setParams({ id: String(tradeId) }, { replace: true });
    }
  }, [tradeId, params, setParams]);

  const load = useCallback(async (id) => {
    if (!id) return;
    setLoading(true);
    setError(null);
    const results = await Promise.allSettled([
      fetchJson(`/lineage/trade/${id}`),
      fetchJson(`/memo/trade/${id}`),
      fetchJson(`/agents/consensus/${id}`),
      fetchJson(`/memory/recall/trade/${id}?k=5`),
      fetchJson(`/explain/features/${id}?top_k=8`),
    ]);
    setLineage(results[0].status === 'fulfilled' ? results[0].value : null);
    setMemo(results[1].status === 'fulfilled' ? results[1].value.memo : null);
    setConsensus(results[2].status === 'fulfilled' ? results[2].value.consensus : null);
    setMemory(results[3].status === 'fulfilled' ? results[3].value : null);
    setAttribution(results[4].status === 'fulfilled' ? results[4].value : null);
    setLoading(false);
  }, []);

  useEffect(() => { load(tradeId); }, [tradeId, load]);

  const regenerateMemo = useCallback(async () => {
    if (!tradeId) return;
    setRegenerating(true);
    try {
      const result = await fetchJson(`/memo/regenerate/${tradeId}`, { method: 'POST' });
      setMemo(result.memo);
    } catch (e) {
      setError(`Regenerate failed: ${e.message}`);
    } finally {
      setRegenerating(false);
    }
  }, [tradeId]);

  return (
    <div>
      <TradePicker
        trades={trades}
        selected={tradeId}
        onSelect={setTradeId}
        loading={tradesLoading}
      />

      {error && (
        <div className="panel" style={{ borderColor: 'var(--danger)', marginBottom: 12 }}>
          <div style={{ color: 'var(--danger)' }}>{error}</div>
        </div>
      )}

      {!tradeId && !tradesLoading && (
        <div className="panel" style={{ textAlign: 'center', padding: 48 }}>
          <div style={{ fontSize: 40, marginBottom: 8 }}>🎯</div>
          <div style={{ color: 'var(--muted)' }}>
            Pick a trade above to inspect its memo, agent consensus, and full decision lineage.
          </div>
        </div>
      )}

      {tradeId && (
        <div style={{ display: 'grid', gap: 16 }}>
          {/* MITS Phase 16.E — Decision Cockpit drill-in for this trade. */}
          <div className="row" style={{
            justifyContent: 'flex-end', marginBottom: -8,
          }}>
            <a
              className="btn small"
              href={`/decision-cockpit/${tradeId}`}
              title="Open in Decision Cockpit — full per-decision audit">
              Open in Decision Cockpit →
            </a>
          </div>
          {loading && <div className="panel">Loading trade {tradeId}…</div>}
          {!loading && (
            <>
              {/* MITS Phase 1 — corpus evidence for the active trade.
                  Tries the (ticker, strategy) cell first, then falls
                  back to the ticker-only top-3 patterns. */}
              {(() => {
                const active = (trades || []).find((t) => t.id === tradeId);
                if (!active) return null;
                return (
                  <div>
                    <EvidencePanel
                      ticker={active.ticker}
                      pattern={active.strategy || undefined}
                      horizon="1d"
                    />
                    {active.strategy && (
                      <div style={{ marginTop: 6 }}>
                        <EvidencePanel ticker={active.ticker} topN={3}
                                              horizon="1d" />
                      </div>
                    )}
                  </div>
                );
              })()}
              <ChairmanPanel consensus={consensus} />
              <ConsensusPanel consensus={consensus} />
              <AgentScorecards />
              <MemoPanel memo={memo} onRegenerate={regenerateMemo} regenerating={regenerating} />
              <AttributionPanel attribution={attribution} />
              <MemoryPanel memory={memory} />
              <LineagePanel lineage={lineage} />
            </>
          )}
        </div>
      )}
    </div>
  );
}
