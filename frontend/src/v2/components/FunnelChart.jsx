/* MITS Phase 19 Stream 1 — SaaS-style decision funnel.
 *
 * Renders the 10-stage decision funnel from /learning/funnel as
 * stacked horizontal bars with conversion percentages between each
 * stage. The width of each bar is proportional to ``n_decisions``
 * (the population that ENTERED that stage). The drop-bar below shows
 * how many were lost between stages with a tooltip showing the top-3
 * blocking rules.
 *
 *   props:
 *     stages:       [{name, n_decisions, n_passed, n_dropped, pass_rate,
 *                     top_3_drop_reasons, note}, ...]
 *     onStageClick: optional (stage) => void
 *
 * Visual:
 *   - Bar fill colour blends green→cyan→yellow→red as pass_rate falls.
 *   - The two smoking-gun stages (policy_eligible + consensus_non_abstain)
 *     get a subtle red outline so the operator sees them first.
 */
import React from 'react';

const TOKENS = {
  green:  '#00ff88',
  cyan:   '#00d4ff',
  yellow: '#ffd700',
  red:    '#ff3355',
  dim:    '#94a3b8',
};

function fmtN(n) {
  if (n == null) return '—';
  const x = Number(n);
  if (!isFinite(x)) return '—';
  return x.toLocaleString();
}

function fmtPct(p) {
  if (p == null) return '—';
  const x = Number(p);
  if (!isFinite(x)) return '—';
  // pass_rate from backend is a 0..1 fraction.
  return `${(x * 100).toFixed(2)}%`;
}

function colorForRate(rate) {
  if (rate == null) return TOKENS.dim;
  if (rate >= 0.75) return TOKENS.green;
  if (rate >= 0.30) return TOKENS.cyan;
  if (rate >= 0.05) return TOKENS.yellow;
  return TOKENS.red;
}

const STAGE_DESCRIPTIONS = {
  watchlist_evaluated:    'Tickers the engine ranged over this cycle',
  analysis_candidate:     'Passed the Analysis Composer (regime + strategy)',
  brain_non_hold:         'Agent council emitted a non-HOLD recommendation',
  policy_eligible:        'Cleared the declarative Policy Engine (no blockers)',
  consensus_quorum_met:   'Consensus reached quorum (≥4 agents)',
  consensus_non_abstain:  'Consensus stance ≠ abstain',
  risk_passed:            'Risk Manager approved sizing + capacity',
  simulator_passed:       'Simulator verdict was tradeable (incl. Monte Carlo)',
  submitted:              'Order sent to the paper broker',
  filled:                 'Broker confirmed a fill',
  closed_with_pnl:        'Closed with realised P&L',
};

export default function FunnelChart({ stages = [], onStageClick }) {
  if (!Array.isArray(stages) || !stages.length) {
    return (
      <div className="v2-funnel v2-funnel--empty">
        no funnel data — engine has not completed a cycle window yet.
        <style>{`
          .v2-funnel--empty {
            padding: 20px;
            background: var(--bg-tertiary);
            border: 1px dashed var(--border-default);
            border-radius: var(--radius-md);
            color: var(--text-tertiary);
            text-align: center;
            font-size: 12px;
          }
        `}</style>
      </div>
    );
  }

  // Width baseline = first stage (watchlist_evaluated).
  const baseline = Math.max(1, Number(stages[0]?.n_decisions || 1));

  return (
    <div className="v2-funnel">
      {stages.map((st, i) => {
        const enteredN = Number(st.n_decisions || 0);
        const passedN  = Number(st.n_passed || 0);
        const droppedN = Number(st.n_dropped || 0);
        const rate     = st.pass_rate;
        const pct      = baseline ? (enteredN / baseline) * 100 : 0;
        const color    = colorForRate(rate);
        const isSmokingGun = st.name === 'policy_eligible' || st.name === 'consensus_non_abstain';
        const next = stages[i + 1];
        const dropPctOfBaseline = baseline ? (droppedN / baseline) * 100 : 0;

        return (
          <div key={st.name} className="v2-funnel__row">
            <button
              type="button"
              className={`v2-funnel__bar${isSmokingGun ? ' v2-funnel__bar--gun' : ''}`}
              onClick={onStageClick ? () => onStageClick(st) : undefined}
              style={{
                width:       `${Math.max(8, pct)}%`,
                background:  `linear-gradient(90deg, ${color}33, ${color}11)`,
                borderColor: color,
              }}
              title={STAGE_DESCRIPTIONS[st.name] || st.name}
            >
              <span className="v2-funnel__bar-name">
                {st.name.replaceAll('_', ' ')}
              </span>
              <span className="v2-funnel__bar-n mono">
                {fmtN(enteredN)} → {fmtN(passedN)}
                <span className="v2-funnel__bar-rate" style={{ color }}>
                  ({fmtPct(rate)})
                </span>
              </span>
            </button>
            {/* drop indicator between this row and the next */}
            {droppedN > 0 && next && (
              <div className="v2-funnel__drop" title={
                (st.top_3_drop_reasons || []).map((r) =>
                  `${r.rule || r.name}: ${r.n}`).join('\n') || 'lost between stages'
              }>
                <div className="v2-funnel__drop-bar"
                     style={{
                       width: `${Math.min(95, Math.max(2, dropPctOfBaseline))}%`,
                       background: '#ff335520',
                       borderColor: '#ff335580',
                     }} />
                <span className="v2-funnel__drop-label mono">
                  −{fmtN(droppedN)} ({fmtPct(droppedN / Math.max(1, enteredN))})
                </span>
              </div>
            )}
          </div>
        );
      })}
      <style>{`
        .v2-funnel {
          display: flex; flex-direction: column;
          gap: 4px;
          padding: 6px 0;
        }
        .v2-funnel__row { display: flex; flex-direction: column; gap: 2px; }
        .v2-funnel__bar {
          display: flex; align-items: center; justify-content: space-between;
          gap: 12px;
          padding: 8px 14px;
          border: 1px solid;
          border-radius: var(--radius-md);
          background: transparent;
          color: var(--text-primary);
          font-size: 12px;
          cursor: pointer;
          transition: filter var(--transition-fast);
          min-width: 220px;
        }
        .v2-funnel__bar:hover { filter: brightness(1.15); }
        .v2-funnel__bar--gun {
          box-shadow: 0 0 0 1px ${TOKENS.red} inset;
        }
        .v2-funnel__bar-name {
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          font-size: 11px;
        }
        .v2-funnel__bar-n {
          font-family: 'JetBrains Mono', monospace;
          font-size: 11px;
          color: var(--text-secondary);
        }
        .v2-funnel__bar-rate { margin-left: 4px; font-weight: 700; }
        .v2-funnel__drop {
          display: flex; align-items: center; gap: 8px;
          padding-left: 12px;
        }
        .v2-funnel__drop-bar {
          height: 4px;
          border: 1px solid;
          border-radius: 2px;
        }
        .v2-funnel__drop-label {
          color: ${TOKENS.red};
          font-size: 10px;
        }
      `}</style>
    </div>
  );
}
