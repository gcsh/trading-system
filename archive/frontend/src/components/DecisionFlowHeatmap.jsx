/**
 * DecisionFlowHeatmap — live visualization of the whole decision
 * pipeline. Answers "is the entire decision tree working as expected?"
 * by showing every stage from data fetch through execution with
 * volume + pass/block counts + per-ticker exit points.
 *
 * Data source: /bot/status.recent_signals. Each signal carries a
 * `status` field that pinpoints where in the pipeline the ticker
 * exited. We map status → stage to derive the funnel without any new
 * backend work.
 *
 * Two views:
 *   1. Funnel — horizontal stages with volume bars (high level)
 *   2. Matrix — per-ticker × per-stage heatmap (drill-down)
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

// Pipeline stages in execution order. Each ticker passes through
// these in sequence and exits at the first one that blocks it.
const STAGES = [
  { key: 'data',      label: 'Data fetch',   short: 'Data',      color: 'var(--data)' },
  { key: 'analytics', label: 'Analytics',    short: 'Analytics', color: 'var(--data)' },
  { key: 'strategy',  label: 'Strategy',     short: 'Strategy',  color: 'var(--info)' },
  { key: 'grade',     label: 'Grade gate',   short: 'Grade',     color: 'var(--info)' },
  { key: 'drift',     label: 'Drift halt',   short: 'Drift',     color: 'var(--warn)' },
  { key: 'event',     label: 'Event risk',   short: 'Event',     color: 'var(--warn)' },
  { key: 'meta',      label: 'Meta-AI',      short: 'Meta',      color: 'var(--purple)' },
  { key: 'council',   label: 'Council',      short: 'Council',   color: 'var(--governance)' },
  { key: 'chairman',  label: 'Chairman',     short: 'Chairman',  color: 'var(--governance)' },
  { key: 'risk',      label: 'Risk eval',    short: 'Risk',      color: 'var(--heat)' },
  { key: 'execute',   label: 'Submitted',    short: 'Execute',   color: 'var(--accent)' },
];

const STAGE_INDEX = Object.fromEntries(STAGES.map((s, i) => [s.key, i]));

// Map signal.status → which stage the ticker EXITED at.
const STATUS_TO_EXIT = {
  hold:                'strategy',     // analyzed, no strategy fired
  signal_only:         'execute',      // passed everything; auto-exec OFF
  submitted:           'execute',      // full pipeline pass — trade fired
  consensus_abstain:   'council',
  chairman_abstain:    'chairman',
  chairman_monitor:    'chairman',
  meta_rejected:       'meta',
  rejected:            'risk',
  low_grade:           'grade',
  drift_halt:          'drift',
  too_small:           'risk',
  already_held:        'risk',
  abstain:             'strategy',     // legacy
};

// Outcome class — was this a PASS at this stage (continued downstream),
// BLOCK (terminal failure), or HOLD (already in book, skipped to avoid
// pyramiding)?  HOLD is its own state because the operator's question
// "did we execute" should be NO for already_held — the position isn't
// new. Earlier this rendered as a green executed cell which falsely
// implied a fresh trade fired.
const STATUS_TO_OUTCOME = {
  submitted:           'pass',         // executed — fresh trade fired
  signal_only:         'pass',         // would have executed; auto-exec off
  hold:                'block',        // no signal
  consensus_abstain:   'block',
  chairman_abstain:    'block',
  chairman_monitor:    'block',
  meta_rejected:       'block',
  rejected:            'block',
  low_grade:           'block',
  drift_halt:          'block',
  too_small:           'block',
  already_held:        'held',         // own state — not a fresh execute
  abstain:             'block',
};

function reasonForStatus(status, fallback) {
  const map = {
    hold:              'analyzed; no strategy fired',
    signal_only:       'would trade — auto-execute OFF',
    submitted:         'order submitted to broker',
    consensus_abstain: 'council voted to abstain',
    chairman_abstain:  'Chairman voted ABSTAIN',
    chairman_monitor:  'Chairman voted MONITOR',
    meta_rejected:     'Meta-AI vetoed',
    rejected:          'risk evaluator rejected',
    low_grade:         'analytics grade below threshold',
    drift_halt:        'strategy auto-halted by drift',
    too_small:         'order below minimum notional',
    already_held:      'position already open — no pyramid',
  };
  return map[status] || fallback || status;
}

function rollup(recent) {
  // Count: by exit stage, by outcome at that stage.
  // Per ticker: which stage they last exited at. recent_signals is
  // append-ordered (oldest → newest); iterate in reverse so the FIRST
  // hit we keep per ticker is the latest event, not a stale one stranded
  // from before the last engine restart.
  const stageCounts = {};
  STAGES.forEach((s) => { stageCounts[s.key] = { pass: 0, block: 0, held: 0, total: 0 }; });
  const perTicker = {};
  const list = recent || [];
  for (let i = list.length - 1; i >= 0; i--) {
    const s = list[i];
    // Synthetic engine events (calendar gate, system warnings) ride on
    // ticker "—" and aren't per-ticker decisions — exclude them from the
    // matrix and the funnel counts so they don't bleed across cycles.
    if (!s.ticker || s.ticker === '—') continue;
    const exit = STATUS_TO_EXIT[s.status] || 'strategy';
    const outcome = STATUS_TO_OUTCOME[s.status] || 'block';
    stageCounts.data.pass += 1; stageCounts.data.total += 1;
    stageCounts.analytics.pass += 1; stageCounts.analytics.total += 1;
    const exitIdx = STAGE_INDEX[exit];
    for (let j = 2; j < exitIdx; j++) {
      stageCounts[STAGES[j].key].pass += 1;
      stageCounts[STAGES[j].key].total += 1;
    }
    if (outcome === 'pass') stageCounts[exit].pass += 1;
    else if (outcome === 'held') stageCounts[exit].held += 1;
    else stageCounts[exit].block += 1;
    stageCounts[exit].total += 1;

    if (!perTicker[s.ticker]) {
      perTicker[s.ticker] = { ...s, exit, outcome };
    }
  }
  return { stageCounts, perTicker };
}

function StageNode({ stage, count, outcome, isExit, maxTotal }) {
  const intensity = maxTotal ? Math.min(1, count.total / maxTotal) : 0;
  const pass = count.pass;
  const block = count.block;
  const total = count.total;
  return (
    <div style={{
      flex: 1, display: 'flex', flexDirection: 'column',
      gap: 6, minWidth: 80,
    }}>
      <div style={{
        fontSize: 9.5, textTransform: 'uppercase', letterSpacing: '0.08em',
        color: 'var(--muted)', fontWeight: 600, textAlign: 'center',
      }}>{stage.short}</div>
      <div style={{
        position: 'relative',
        background: 'var(--panel-2)',
        border: '1px solid var(--border)',
        borderRadius: 8,
        padding: '14px 6px',
        textAlign: 'center',
        minHeight: 84,
        overflow: 'hidden',
      }}>
        {/* heat fill */}
        <div style={{
          position: 'absolute', inset: 0,
          background: stage.color,
          opacity: 0.10 + intensity * 0.30,
        }} />
        {/* glow if active */}
        {total > 0 && (
          <div style={{
            position: 'absolute', inset: 0,
            boxShadow: `inset 0 0 0 1px ${stage.color}`,
            borderRadius: 8,
            opacity: 0.7,
          }} />
        )}
        <div style={{ position: 'relative' }}>
          <div style={{
            fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em',
            color: total > 0 ? 'var(--text)' : 'var(--muted-2)',
            fontFeatureSettings: '"tnum"',
            lineHeight: 1,
          }}>{total}</div>
          <div style={{ fontSize: 10, marginTop: 6, lineHeight: 1.3 }}>
            {pass > 0 && (
              <span className="accent-markets" style={{ marginRight: 6 }}>
                ✓ {pass}
              </span>
            )}
            {count.held > 0 && (
              <span className="accent-system" style={{ marginRight: 6 }}
                    title="already-held — position from a prior cycle">
                ⌂ {count.held}
              </span>
            )}
            {block > 0 && (
              <span className="accent-bear">
                ✗ {block}
              </span>
            )}
            {total === 0 && <span className="accent-muted">—</span>}
          </div>
        </div>
      </div>
    </div>
  );
}

function FlowConnector({ active }) {
  return (
    <div style={{
      width: 14, alignSelf: 'center',
      marginTop: 30, // align with stage node mid
      display: 'flex', justifyContent: 'center', alignItems: 'center',
    }}>
      <svg width="14" height="10" viewBox="0 0 14 10">
        <path
          d="M0,5 L11,5 M8,2 L11,5 L8,8"
          fill="none"
          stroke={active ? 'var(--accent-2)' : 'var(--border-strong)'}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

function PerTickerMatrix({ perTicker }) {
  const tickers = Object.keys(perTicker).sort();
  if (!tickers.length) {
    return (
      <div className="empty">
        <div className="title">No evaluations yet</div>
        <div className="hint">Waiting for the first cycle to complete.</div>
      </div>
    );
  }
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', tableLayout: 'fixed' }}>
        <thead>
          <tr>
            <th style={{ width: 80 }}>Ticker</th>
            {STAGES.map((s) => (
              <th key={s.key} style={{
                textAlign: 'center',
                fontSize: 9.5,
                padding: '8px 4px',
              }}>{s.short}</th>
            ))}
            <th style={{ width: 160 }}>Last reason</th>
          </tr>
        </thead>
        <tbody>
          {tickers.map((t) => {
            const sig = perTicker[t];
            const exitIdx = STAGE_INDEX[sig.exit] ?? 0;
            const pass = sig.outcome === 'pass';
            return (
              <tr key={t}>
                <td style={{ fontWeight: 600 }}>{t}</td>
                {STAGES.map((s, i) => {
                  let cell = null;
                  if (i < exitIdx) {
                    cell = <span style={{
                      display: 'inline-block', width: 16, height: 16,
                      borderRadius: 3,
                      background: 'var(--accent)', opacity: 0.6,
                    }} title="passed" />;
                  } else if (i === exitIdx) {
                    const isHeld = sig.outcome === 'held';
                    const terminalColor = isHeld
                      ? 'var(--system)'                  // cyan = already-held
                      : pass ? 'var(--accent)' : 'var(--danger)';
                    const terminalSoft = isHeld
                      ? 'var(--system-soft)'
                      : pass ? 'var(--accent-soft)' : 'var(--danger-soft)';
                    const label = isHeld ? 'HELD' : (pass ? 'EXECUTED' : 'BLOCKED');
                    cell = (
                      <span style={{
                        display: 'inline-block', width: 16, height: 16,
                        borderRadius: 3,
                        background: terminalColor,
                        boxShadow: `0 0 0 2px ${terminalSoft}`,
                      }} title={`${label} at ${s.label}: ${reasonForStatus(sig.status, sig.reason)}`} />
                    );
                  } else {
                    cell = <span style={{
                      display: 'inline-block', width: 16, height: 16,
                      borderRadius: 3,
                      background: 'var(--panel-2)',
                      border: '1px solid var(--border)',
                    }} title="not reached" />;
                  }
                  return (
                    <td key={s.key} style={{ textAlign: 'center', padding: '6px 4px' }}>
                      {cell}
                    </td>
                  );
                })}
                <td style={{
                  fontSize: 11, color: 'var(--muted)',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden', textOverflow: 'ellipsis',
                }} title={sig.reason}>
                  {sig.reason || reasonForStatus(sig.status)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function DecisionFlowHeatmap() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const s = await api('/bot/status');
      setStatus(s);
      setError(null);
    } catch (e) { setError(e.message); }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  const recent = status?.recent_signals || [];
  const { stageCounts, perTicker } = useMemo(() => rollup(recent), [recent]);
  const maxTotal = Math.max(...Object.values(stageCounts).map((c) => c.total), 1);

  // Total submissions
  const submitted = stageCounts.execute?.pass || 0;
  const totalEvaluations = recent.length;

  return (
    <div className="panel panel--governance">
      <div className="panel-head">
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>Decision flow · live</div>
          <h2 style={{ margin: '4px 0 0' }}>Pipeline heatmap</h2>
        </div>
        <div className="row" style={{ gap: 8, fontSize: 12, color: 'var(--muted)' }}>
          <span>{totalEvaluations} evaluations in window</span>
          {submitted > 0 && (
            <span className="pill on">{submitted} executed</span>
          )}
        </div>
      </div>

      {error && (
        <div className="accent-bear" style={{ fontSize: 12, marginBottom: 8 }}>
          {error}
        </div>
      )}

      <div style={{ marginBottom: 8, fontSize: 12, color: 'var(--muted)' }}>
        Every ticker passes through these stages in order; if a stage blocks, the
        ticker exits there with a reason. Hover a cell below to see why.
      </div>

      {/* Funnel */}
      <div style={{
        display: 'flex',
        alignItems: 'stretch',
        gap: 0,
        background: 'var(--bg)',
        padding: 14,
        borderRadius: 12,
        border: '1px solid var(--border)',
        marginBottom: 18,
        overflowX: 'auto',
      }}>
        {STAGES.map((stage, i) => {
          const count = stageCounts[stage.key];
          const next = i < STAGES.length - 1 ? STAGES[i + 1] : null;
          const flowActive = (count?.pass || 0) > 0
                                  && next && (stageCounts[next.key]?.total || 0) > 0;
          return (
            <React.Fragment key={stage.key}>
              <StageNode
                stage={stage}
                count={count}
                isExit={false}
                maxTotal={maxTotal}
              />
              {next && <FlowConnector active={flowActive} />}
            </React.Fragment>
          );
        })}
      </div>

      {/* Per-ticker matrix */}
      <div className="section-title">Per ticker · where each one is right now</div>
      <div style={{ maxHeight: 420, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 8 }}>
        <PerTickerMatrix perTicker={perTicker} />
      </div>

      {/* Legend */}
      <div style={{
        marginTop: 10, fontSize: 11, color: 'var(--muted)',
        display: 'flex', gap: 14, flexWrap: 'wrap',
      }}>
        <span className="row" style={{ gap: 6 }}>
          <span style={{ width: 12, height: 12, borderRadius: 3, background: 'var(--accent)', opacity: 0.6 }} />
          passed
        </span>
        <span className="row" style={{ gap: 6 }}>
          <span style={{ width: 12, height: 12, borderRadius: 3, background: 'var(--accent)' }} />
          executed (fresh trade)
        </span>
        <span className="row" style={{ gap: 6 }}>
          <span style={{ width: 12, height: 12, borderRadius: 3, background: 'var(--system)' }} />
          already held (skipped — position from earlier)
        </span>
        <span className="row" style={{ gap: 6 }}>
          <span style={{ width: 12, height: 12, borderRadius: 3, background: 'var(--danger)' }} />
          blocked here
        </span>
        <span className="row" style={{ gap: 6 }}>
          <span style={{ width: 12, height: 12, borderRadius: 3, background: 'var(--panel-2)', border: '1px solid var(--border)' }} />
          not reached
        </span>
      </div>
    </div>
  );
}
