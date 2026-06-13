/**
 * Feature-Merge F5 — Full 10-stage Decision Pipeline funnel chart.
 *
 * Lives on the ORIGINAL site at /decision-scorecard (the F1 banner's
 * "Why?" deep-link target). SaaS-style vertical conversion funnel:
 *
 *   • Big number at top — submissions / evaluations + pct
 *   • 10 horizontal stage bars, each width ∝ n_passed / first-stage-N
 *   • Between stages: drop indicator with top 1-3 drop reasons as pills
 *   • Bottom card — "Top surgical change candidate" + plain-English why
 *
 * Color gradient: cyan/blue (top funnel) → orange/red (bottom funnel),
 * matching the operator's pipeline-leak narrative.
 *
 * Single canonical data source: useFunnel from hooks/swr/useFunnel.js
 * (created by Feature-Merge F1). Numbers MUST equal:
 *   • Today page's ThroughputAlertBanner
 *   • FunnelSummaryPanel's 5-row mini-funnel
 *   • DecisionCockpit's learning_insights.funnel_snapshot (F3)
 *
 * NO design tokens from src/design/* or src/v2/* — original-site
 * var(--…) tokens only. Mobile responsive.
 */
import React, { useMemo } from 'react';
import { useFunnel } from '../hooks/swr/useFunnel.js';
import TooltipExplainer from './TooltipExplainer.jsx';

// Each stage in the 10-step canonical report.stages array. The
// `explainer` text is operator-facing plain English — what does this
// stage actually measure / what does it mean if the pipeline leaks
// here.
const STAGE_META = {
  watchlist_evaluated: {
    label: 'Watchlist evaluated',
    explainer: 'Every ticker the engine looked at this cycle. This is the top of the funnel — everything starts here.',
  },
  analysis_candidate: {
    label: 'Analysis candidate',
    explainer: 'Ticker had enough data + activity to be worth a full Brain analysis.',
  },
  brain_non_hold: {
    label: 'Brain non-HOLD',
    explainer: 'The Brain returned a non-HOLD recommendation (buy or sell, not "do nothing").',
  },
  policy_eligible: {
    label: 'Policy eligible',
    explainer: 'Cleared the declarative policy engine (max positions, cooldowns, hours, risk caps).',
  },
  consensus_quorum_met: {
    label: 'Consensus quorum met',
    explainer: 'Enough Council agents weighed in — we had a real opinion, not a single vote.',
  },
  consensus_non_abstain: {
    label: 'Consensus non-abstain',
    explainer: 'The Council majority opinion was actionable (not "abstain / unclear").',
  },
  risk_passed: {
    label: 'Risk passed',
    explainer: 'Risk Manager approved — position size, exposure, and correlation all within limits.',
  },
  simulator_passed: {
    label: 'Simulator passed',
    explainer: 'The Simulator Agent validated the trade against historical analogs — expected edge held up.',
  },
  submitted: {
    label: 'Order submitted',
    explainer: 'Order was sent to the broker. This is the only stage that costs real (or paper) money.',
  },
  closed_with_pnl: {
    label: 'Closed with P&L',
    explainer: 'Trade exited (TP / SL / time stop / expiry). This is where the funnel becomes learning data.',
  },
};

// Color gradient indexed by stage position (0..9). Cyan/blue top →
// orange/red bottom, matching the "pipeline leak" narrative.
const STAGE_COLORS = [
  '#22d3ee',  // 0 watchlist_evaluated   — system / cyan
  '#38bdf8',  // 1 analysis_candidate    — info
  '#60a5fa',  // 2 brain_non_hold        — blue
  '#818cf8',  // 3 policy_eligible       — governance
  '#a78bfa',  // 4 consensus_quorum_met  — purple
  '#c4b5fd',  // 5 consensus_non_abstain — purple-2
  '#fbbf24',  // 6 risk_passed           — warn-2 / yellow
  '#f59e0b',  // 7 simulator_passed      — warn
  '#f97316',  // 8 submitted             — heat
  '#10b981',  // 9 closed_with_pnl       — accent (closes the loop, good)
];

function fmtN(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString();
}

function fmtPct(rate, digits = 2) {
  if (rate == null || Number.isNaN(Number(rate))) return '—';
  return `${(Number(rate) * 100).toFixed(digits)}%`;
}

function StageBar({
  stage,
  idx,
  baseline,
  color,
}) {
  const meta = STAGE_META[stage.name] || {
    label: stage.name,
    explainer: 'Pipeline stage.',
  };
  const nPassed = Number(stage.n_passed ?? 0);
  const widthPct = baseline > 0
    ? Math.max(2, Math.min(100, (nPassed / baseline) * 100))
    : 0;
  const rate = stage.pass_rate == null ? null : Number(stage.pass_rate);
  return (
    <div
      data-testid={`funnel-stage-${stage.name}`}
      data-stage-index={idx}
      className="row"
      style={{
        alignItems: 'center',
        gap: 10,
        marginBottom: 2,
        flexWrap: 'nowrap',
      }}
    >
      {/* Stage label + tooltip — fixed-width on desktop, shrinks on mobile */}
      <div style={{
        minWidth: 180,
        maxWidth: 180,
        fontSize: 12.5,
        color: 'var(--text-soft)',
        fontWeight: 500,
        lineHeight: 1.2,
      }}>
        <TooltipExplainer
          term={meta.label}
          explanation={meta.explainer}
        >
          <span>{meta.label}</span>
        </TooltipExplainer>
      </div>

      {/* The actual funnel bar — width shrinks per stage to convey shape */}
      <div style={{
        flex: 1,
        minWidth: 0,
        height: 22,
        background: 'var(--panel-2)',
        borderRadius: 4,
        border: '1px solid var(--border)',
        overflow: 'hidden',
        position: 'relative',
      }}>
        <div
          data-testid={`funnel-stage-${stage.name}-bar`}
          style={{
            width: `${widthPct}%`,
            height: '100%',
            background: `linear-gradient(90deg, ${color} 0%, ${color}cc 100%)`,
            transition: 'width 240ms ease-out',
          }}
        />
      </div>

      {/* n_passed count */}
      <div
        data-testid={`funnel-stage-${stage.name}-count`}
        style={{
          minWidth: 80,
          textAlign: 'right',
          color: 'var(--text)',
          fontWeight: 600,
          fontFeatureSettings: '"tnum"',
          fontSize: 12.5,
        }}
      >
        {fmtN(nPassed)}
      </div>

      {/* pass_rate vs prior stage */}
      <div
        data-testid={`funnel-stage-${stage.name}-rate`}
        style={{
          minWidth: 64,
          textAlign: 'right',
          color: 'var(--muted)',
          fontFeatureSettings: '"tnum"',
          fontSize: 11.5,
        }}
        title="Pass rate vs the prior stage"
      >
        {fmtPct(rate)}
      </div>
    </div>
  );
}

function DropIndicator({ stage, nextStage }) {
  // Stage k -> stage k+1 drops:
  //   stage.n_passed -> nextStage.n_passed delta is "dropped between k and k+1"
  //   but the report already exposes top_3_drop_reasons on the LATER stage
  //   (reasons the next stage's input was killed). We render that here.
  const reasons = nextStage?.top_3_drop_reasons || [];
  const nDropped = Math.max(0,
    Number(stage?.n_passed ?? 0) - Number(nextStage?.n_passed ?? 0));
  if (nDropped <= 0 && reasons.length === 0) {
    return (
      <div style={{ height: 6 }} />
    );
  }
  return (
    <div
      data-testid={`funnel-drop-${nextStage.name}`}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        margin: '2px 0 4px 190px',
        flexWrap: 'wrap',
      }}
    >
      <span
        style={{
          fontSize: 10.5,
          color: 'var(--danger-2)',
          fontWeight: 600,
          fontFeatureSettings: '"tnum"',
        }}
        title="Decisions dropped between these two stages"
      >
        ↘ {fmtN(nDropped)} dropped
      </span>
      {reasons.slice(0, 3).map((r, i) => {
        // Live shape uses `rule` + `n`; older shapes had `reason`/`name` + `count`.
        const reasonName = (typeof r === 'string')
          ? r
          : (r?.rule || r?.reason || r?.name || 'unknown');
        const reasonCount = r?.n != null
          ? r.n
          : (r?.count != null ? r.count : null);
        return (
          <span
            key={`${reasonName}-${i}`}
            data-testid={`funnel-drop-reason-${reasonName}`}
            style={{
              fontSize: 10.5,
              padding: '2px 6px',
              borderRadius: 10,
              background: 'var(--danger-soft)',
              border: '1px solid var(--danger-border)',
              color: 'var(--danger-2)',
            }}
            title={`Top drop reason: ${reasonName}`}
          >
            {reasonName}
            {reasonCount != null ? ` · ${fmtN(reasonCount)}` : ''}
          </span>
        );
      })}
    </div>
  );
}

function SurgicalChangeCard({ candidate }) {
  if (!candidate) return null;
  // The endpoint exposes either a string (recommendation_candidate /
  // top_surgical_change_candidate) or an object with `name`/`rationale`.
  let name = null;
  let rationale = null;
  let confidence = null;
  if (typeof candidate === 'string') {
    name = candidate;
  } else if (typeof candidate === 'object') {
    // Live shape uses `candidate` for the name + `rationale` for the
    // explanation. Fall back to `name`/`recommendation` for older shapes.
    name = candidate.candidate
        || candidate.name
        || candidate.recommendation
        || null;
    rationale = candidate.rationale
        || candidate.explanation
        || candidate.investigation
        || candidate.why
        || null;
    confidence = candidate.confidence ?? candidate.score ?? null;
  }
  if (!name) return null;

  return (
    <div
      data-testid="funnel-surgical-card"
      className="panel"
      style={{
        marginTop: 16,
        padding: 14,
        background: 'var(--accent-soft)',
        border: '1px solid var(--accent-border)',
        borderRadius: 'var(--radius-sm, 8px)',
      }}
    >
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        marginBottom: 6,
      }}>
        <span style={{
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: '0.06em',
          color: 'var(--accent-2)',
          fontWeight: 700,
        }}>
          Top surgical change candidate
        </span>
        <TooltipExplainer
          term="Surgical change candidate"
          explanation="The single advisory change the learning system thinks would most improve throughput. Advisory only — nothing changes automatically."
        />
      </div>
      <div style={{
        color: 'var(--text)',
        fontWeight: 600,
        fontSize: 14,
        marginBottom: rationale ? 6 : 0,
      }}>
        {name}
        {confidence != null && (
          <span style={{
            marginLeft: 8,
            fontSize: 11,
            color: 'var(--muted)',
            fontWeight: 500,
          }}>
            (confidence {(Number(confidence) * 100).toFixed(0)}%)
          </span>
        )}
      </div>
      {rationale && (
        <div style={{
          color: 'var(--text-soft)',
          fontSize: 12.5,
          lineHeight: 1.5,
        }}>
          {rationale}
        </div>
      )}
    </div>
  );
}

export default function FullDecisionFunnelChart() {
  const { row, report, submissionRate, isLoading, error } = useFunnel();

  const stages = useMemo(() => {
    const arr = report?.stages;
    return Array.isArray(arr) ? arr : [];
  }, [report]);

  const baseline = useMemo(() => {
    for (const s of stages) {
      const n = Number(s?.n_decisions ?? s?.n_passed ?? 0);
      if (n > 0) return n;
    }
    return Number(row?.n_evaluations ?? 0);
  }, [stages, row]);

  // Derive submissions / evaluations from stages when the daily row
  // hasn't been persisted yet (on-demand fallback path: report exists
  // but row is null). The watchlist_evaluated stage = evaluations,
  // submitted stage = submissions.
  const stageStats = useMemo(() => {
    const byName = {};
    for (const s of stages) {
      if (s?.name) byName[s.name] = s;
    }
    const evals = Number(byName.watchlist_evaluated?.n_decisions
                         ?? byName.watchlist_evaluated?.n_passed
                         ?? 0);
    const subs  = Number(byName.submitted?.n_passed ?? 0);
    const rate  = evals > 0 ? subs / evals : null;
    return { evals, subs, rate };
  }, [stages]);

  const headerLine = useMemo(() => {
    // Prefer the persisted daily row; fall back to stage-derived totals
    // so the chart still tells the operator the same story on the
    // on-demand fallback path.
    const subs  = row?.n_submitted   ?? stageStats.subs;
    const evals = row?.n_evaluations ?? stageStats.evals;
    if (!evals && !subs) return null;
    const rate = submissionRate ?? stageStats.rate;
    const pct = rate == null
      ? '—'
      : `${(rate * 100).toFixed(3)}%`;
    return `${fmtN(subs)} submissions / ${fmtN(evals)} evaluations = ${pct}`;
  }, [row, submissionRate, stageStats]);

  const surgical = report?.top_surgical_change_candidate
    || report?.recommendation_candidate
    || null;

  // Treat "report.stages populated" as a renderable state even if `row`
  // is null — the on-demand fallback path is the common dev/staging case.
  const renderable = !!row || stages.length > 0;

  // Empty / error / loading shells preserve the section's height so the
  // page doesn't jump.
  if (error) {
    return (
      <div
        data-testid="full-funnel-chart"
        className="panel"
        style={{ padding: 14, marginBottom: 16 }}
      >
        <h2 style={{ margin: '0 0 8px 0', fontSize: 16 }}>
          Decision Pipeline — Last 14 Days
        </h2>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>
          Funnel unavailable — {String(error.message || error)}.
        </div>
      </div>
    );
  }
  if (isLoading && !renderable) {
    return (
      <div
        data-testid="full-funnel-chart"
        className="panel"
        style={{ padding: 14, marginBottom: 16 }}
      >
        <h2 style={{ margin: '0 0 8px 0', fontSize: 16 }}>
          Decision Pipeline — Last 14 Days
        </h2>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>
          Loading funnel…
        </div>
      </div>
    );
  }
  if (!renderable) {
    return (
      <div
        data-testid="full-funnel-chart"
        className="panel"
        style={{ padding: 14, marginBottom: 16 }}
      >
        <h2 style={{ margin: '0 0 8px 0', fontSize: 16 }}>
          Decision Pipeline — Last 14 Days
        </h2>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>
          Funnel snapshot not computed yet today (next run 21:55 ET).
        </div>
      </div>
    );
  }
  if (stages.length === 0) {
    return (
      <div
        data-testid="full-funnel-chart"
        className="panel"
        style={{ padding: 14, marginBottom: 16 }}
      >
        <h2 style={{ margin: '0 0 8px 0', fontSize: 16 }}>
          Decision Pipeline — Last 14 Days
        </h2>
        <div style={{
          fontSize: 13,
          color: 'var(--text)',
          marginBottom: 4,
        }}>
          {headerLine}
        </div>
        <div style={{ color: 'var(--muted)', fontSize: 13 }}>
          Stage breakdown not yet persisted — daily report
          regenerates at 21:55 ET.
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="full-funnel-chart"
      className="panel"
      style={{
        padding: 14,
        marginBottom: 16,
      }}
    >
      <div className="row" style={{
        justifyContent: 'space-between',
        alignItems: 'baseline',
        gap: 8,
        flexWrap: 'wrap',
        marginBottom: 10,
      }}>
        <h2 style={{ margin: 0, fontSize: 16, display: 'flex',
                     alignItems: 'center', gap: 6 }}>
          Decision Pipeline — Last 14 Days
          <TooltipExplainer
            term="Decision Pipeline"
            explanation="A 10-stage funnel from every ticker the engine looked at down to closed trades. Each stage shows how many decisions survived. Wide drops between stages tell you where the pipeline leaks."
          />
        </h2>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          window={(row?.window_days ?? report?.window_days ?? 14)} days
        </div>
      </div>

      {/* Big headline number — submissions / evaluations + pct */}
      <div
        data-testid="full-funnel-headline"
        style={{
          fontSize: 18,
          fontWeight: 700,
          color: 'var(--text)',
          marginBottom: 14,
          fontFeatureSettings: '"tnum"',
        }}
      >
        {headerLine}
      </div>

      {/* 10 stages, each followed by a drop indicator */}
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {stages.map((s, i) => {
          const color = STAGE_COLORS[i] || 'var(--info)';
          const next = stages[i + 1];
          return (
            <React.Fragment key={s.name || i}>
              <StageBar
                stage={s}
                idx={i}
                baseline={baseline}
                color={color}
              />
              {next && (
                <DropIndicator stage={s} nextStage={next} />
              )}
            </React.Fragment>
          );
        })}
      </div>

      <SurgicalChangeCard candidate={surgical} />

      {/* Cross-page consistency hint — these are the same numbers the
          Today banner and Cockpit show. */}
      <div style={{
        marginTop: 12,
        fontSize: 11,
        color: 'var(--muted-2, var(--muted))',
        lineHeight: 1.4,
      }}>
        Source: <code style={{
          fontSize: 11,
          color: 'var(--muted)',
          background: 'var(--panel-2)',
          padding: '1px 5px',
          borderRadius: 3,
        }}>/learning/funnel</code> via
        the canonical <code style={{
          fontSize: 11,
          color: 'var(--muted)',
          background: 'var(--panel-2)',
          padding: '1px 5px',
          borderRadius: 3,
        }}>useFunnel</code> hook —
        same numbers as Today&apos;s pipeline banner and the Decision
        Cockpit&apos;s learning panel.
      </div>
    </div>
  );
}
