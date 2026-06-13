/* MITS Phase 19 Cluster C — Learning Funnel v2 (/v2/learning/funnel).
 *
 * Dedicated full-screen SaaS conversion funnel backed by /learning/funnel.
 * Surfaces every stage of the decision pipeline so the operator can see
 * at a glance "where did 6000 evals turn into 7 submitted trades?".
 *
 *   Top-of-page banner   — throughput-collapse warning if submission_rate < 0.5%
 *   FullFunnelChart       — 10 stages, vertical
 *   Confidence histogram  — 3 series overlaid (all / non-hold / submitted)
 *   Counterfactual chart  — show new headline blocker counts
 *   Cooldown audit panel  — affected tickers + lost opportunities
 *   Top surgical change   — engine's own recommendation (advisory)
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import FullFunnelChart from '../components/FullFunnelChart.jsx';

async function api(path) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

function fmtN(v) {
  if (v == null) return '—';
  return Number(v).toLocaleString();
}

/* ── Confidence histogram (3 series overlay) ──────────────────────── */
function ConfidenceHistogram({ histograms }) {
  if (!histograms || !histograms.bin_edges) {
    return <EmptyState icon="∅" message="No confidence histogram yet." />;
  }
  const { bin_edges, all_evals, non_hold, submitted } = histograms;
  const nBins = bin_edges.length - 1;
  const max = Math.max(
    ...(all_evals || []),
    ...(non_hold || []),
    ...(submitted || []),
    1,
  );

  // Render 3 stacked rows so the operator can compare counts at each bin.
  function Series({ label, color, values }) {
    return (
      <div style={{ marginBottom: 6 }}>
        <div style={{
          display: 'flex', alignItems: 'baseline', gap: 6,
          fontSize: 11, color: 'var(--text-tertiary)',
        }}>
          <span style={{
            display: 'inline-block', width: 10, height: 10,
            background: color, borderRadius: 2,
          }}/>
          <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
          <span className="mono" style={{ marginLeft: 'auto' }}>
            Σ {(values || []).reduce((a, b) => a + (b || 0), 0).toLocaleString()}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: 50, marginTop: 4 }}>
          {Array.from({ length: nBins }).map((_, i) => {
            const v = (values || [])[i] || 0;
            const h = (v / max) * 100;
            return (
              <div key={i}
                   style={{ flex: 1, position: 'relative' }}
                   title={`[${bin_edges[i].toFixed(1)}–${bin_edges[i + 1].toFixed(1)}]: ${v}`}>
                <div style={{
                  position: 'absolute', bottom: 0, left: 0, right: 0,
                  height: `${Math.max(2, h)}%`,
                  background: color, opacity: 0.85,
                  borderRadius: '2px 2px 0 0',
                  minHeight: 2,
                }}/>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div>
      <Series label="All evaluations" color="var(--text-muted)" values={all_evals} />
      <Series label="Non-HOLD votes"   color="var(--accent-cyan)" values={non_hold} />
      <Series label="Submitted trades" color="var(--accent-green)" values={submitted} />

      {/* X-axis labels */}
      <div style={{ display: 'flex', gap: 2, marginTop: 6 }}>
        {Array.from({ length: nBins }).map((_, i) => (
          <div key={i} className="mono" style={{
            flex: 1, textAlign: 'center',
            fontSize: 9, color: 'var(--text-muted)',
          }}>
            {bin_edges[i].toFixed(1)}
          </div>
        ))}
      </div>
      <div style={{
        marginTop: 6, fontSize: 11, color: 'var(--text-tertiary)',
      }}>
        X-axis = Brain confidence (0–1). Operator note: when all-evals mass piles up
        below 0.1, the Brain is voting HOLD by default. That's the canonical
        "confidence collapse" signature.
      </div>
    </div>
  );
}

/* ── Counterfactual histogram ─────────────────────────────────────── */
function CounterfactualChart({ cf }) {
  if (!cf?.new_headline_blocker_counts) {
    return <EmptyState icon="∅" message="No counterfactual snapshot yet." />;
  }
  const entries = Object.entries(cf.new_headline_blocker_counts)
    .sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 8 }}>
        If we remove <code>{cf.rule_overridden}</code> as a blocker, here is what
        would block instead (sample n = {fmtN(cf.n_decisions_analyzed)}).
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {entries.map(([rule, n], i) => (
          <div key={rule} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="mono" style={{ width: 200, fontSize: 12, color: 'var(--text-secondary)' }}>
              {rule}
            </span>
            <div style={{
              flex: 1, height: 14, background: 'var(--bg-secondary)',
              borderRadius: 3, overflow: 'hidden',
            }}>
              <div style={{
                width: `${(n / max) * 100}%`,
                height: '100%',
                background: i === 0 ? 'var(--accent-red)' : 'var(--accent-yellow)',
              }}/>
            </div>
            <span className="mono" style={{
              minWidth: 70, textAlign: 'right',
              fontSize: 12, color: 'var(--text-primary)',
            }}>
              {fmtN(n)}
            </span>
          </div>
        ))}
      </div>
      {cf.note && (
        <div style={{ marginTop: 8, fontSize: 11, fontStyle: 'italic', color: 'var(--text-muted)' }}>
          {cf.note}
        </div>
      )}
    </div>
  );
}

/* ── Page ─────────────────────────────────────────────────────────── */
export default function LearningFunnel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [selectedStage, setSelectedStage] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const j = await api('/learning/funnel');
        if (alive) { setData(j); setErr(null); }
      } catch (e) {
        if (alive) setErr(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, []);

  const report = data?.report;
  const row = data?.row;
  const stages = report?.stages || [];
  const submitStage = stages.find(s => s.name === 'submitted');
  const submitRate = submitStage?.pass_rate ?? 0;
  const isCollapsed = submitRate < 0.005;

  const surgical = report?.top_surgical_change_candidate;
  const cf = report?.counterfactual;
  const cooldown = report?.cooldown_audit;
  const histograms = report?.confidence_histograms;

  // KPI derivatives
  const watchlistSize = report?.watchlist_size ?? row?.watchlist_size;
  const nEval = stages[0]?.n_decisions ?? row?.n_evaluations;
  const nSubmitted = submitStage?.n_passed ?? row?.n_submitted;
  const composite = report?.composite_quality_mean ?? row?.composite_quality_mean;

  return (
    <div style={{ padding: 'var(--space-6)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 16, gap: 16 }}>
        <h1 style={{
          fontSize: 'var(--font-size-xl)', fontWeight: 800,
          color: 'var(--text-primary)', margin: 0,
          letterSpacing: '0.02em', textTransform: 'uppercase',
        }}>Learning Funnel</h1>
        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>
          Watchlist → Eval → Brain → Policy → Risk → Submitted → Closed.
          {report?.window_days ? ` Last ${report.window_days} days.` : ''}
        </div>
      </div>

      {err && (
        <div className="v2-alert v2-alert--critical" style={{ marginBottom: 16 }}>{err}</div>
      )}

      {/* Throughput collapse banner */}
      {data && isCollapsed && (
        <div className="v2-alert v2-alert--critical" style={{ marginBottom: 16 }}>
          <strong>Throughput collapse:</strong>
          &nbsp;submission rate = {(submitRate * 100).toFixed(3)}% (&lt; 0.5%).
          Brain is voting HOLD on the vast majority of evals. The smoking-gun
          signature is confidence mass piling up below 0.1 — see Confidence
          Distribution below.
        </div>
      )}

      {/* KPI strip */}
      <Section title="Funnel KPIs">
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(170px,1fr))',
          gap: 12,
        }}>
          <Card>
            <Stat label="Watchlist size" value={fmtN(watchlistSize)} mono
                  hint="Tickers in scope this snapshot." />
          </Card>
          <Card>
            <Stat label="Evaluations" value={fmtN(nEval)} mono
                  hint="Total decisions evaluated at the top of the funnel." />
          </Card>
          <Card glow={isCollapsed ? 'red' : 'none'}>
            <Stat label="Submitted" value={fmtN(nSubmitted)} mono
                  delta={`${(submitRate * 100).toFixed(3)}% pass`}
                  deltaPositive={submitRate >= 0.005}
                  hint="Decisions that became actual orders." />
          </Card>
          <Card>
            <Stat label="Composite mean"
                  value={composite != null ? Number(composite).toFixed(1) : '—'}
                  mono
                  hint="Average composite quality of submitted decisions." />
          </Card>
          <Card>
            <Stat label="Cooldown hits"
                  value={fmtN(cooldown?.n_cooldown_hits)}
                  mono
                  hint="Number of decisions blocked by the per-ticker cooldown." />
          </Card>
        </div>
      </Section>

      {/* Main funnel + side rail */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0,3fr) minmax(280px,1.2fr)',
        gap: 16,
      }}>
        <div>
          <Section title="Conversion Funnel"
                   subtitle="Click a stage to inspect drop reasons">
            {stages.length === 0
              ? <EmptyState icon="∅" message="No funnel report yet." />
              : <FullFunnelChart stages={stages} onSelect={setSelectedStage} />}
          </Section>
        </div>
        <div>
          {/* Stage detail */}
          <Section title="Stage detail">
            <Card>
              {!selectedStage && (
                <EmptyState icon="◉" message="Click a stage in the funnel to see its drop reasons." />
              )}
              {selectedStage && (
                <>
                  <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6 }}>
                    {selectedStage.name}
                  </div>
                  <div className="mono" style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                    in {fmtN(selectedStage.n_decisions)} · pass {fmtN(selectedStage.n_passed)} · drop {fmtN(selectedStage.n_dropped)}
                  </div>
                  <div style={{ marginTop: 10 }}>
                    <div className="v2-stat__label">Top drop reasons</div>
                    {(selectedStage.top_3_drop_reasons || []).length === 0
                      ? <div style={{ color: 'var(--text-muted)', fontSize: 11, marginTop: 4 }}>None.</div>
                      : (selectedStage.top_3_drop_reasons || []).map((r, i) => (
                        <div key={r.rule + i} style={{
                          display: 'flex', justifyContent: 'space-between',
                          fontSize: 11.5, marginTop: 4,
                        }}>
                          <span className="mono" style={{ color: 'var(--text-secondary)' }}>{r.rule}</span>
                          <span className="mono">{fmtN(r.n)}</span>
                        </div>
                      ))}
                  </div>
                  {selectedStage.note && (
                    <div style={{
                      marginTop: 10, fontSize: 11, color: 'var(--text-muted)',
                      fontStyle: 'italic',
                    }}>{selectedStage.note}</div>
                  )}
                </>
              )}
            </Card>
          </Section>

          {/* Surgical change */}
          <Section title="Top surgical change">
            <Card glow={surgical?.severity === 'high' ? 'red' : surgical ? 'purple' : 'none'}>
              {!surgical && <EmptyState icon="∅" message="No surgical candidate yet." />}
              {surgical && (
                <>
                  <Pill tone={
                    surgical.severity === 'high' ? 'error'
                    : surgical.severity === 'medium' ? 'warning'
                    : 'info'
                  } size="md">{(surgical.severity || 'info').toUpperCase()}</Pill>
                  <div className="mono" style={{
                    fontSize: 13, color: 'var(--accent-purple)', fontWeight: 700,
                    marginTop: 6,
                  }}>{surgical.candidate}</div>
                  <div style={{ fontSize: 11.5, color: 'var(--text-secondary)', marginTop: 4 }}>
                    {surgical.rationale}
                  </div>
                  <div style={{
                    marginTop: 8, fontSize: 11, color: 'var(--text-tertiary)',
                  }}>
                    Auto-apply: <strong>{surgical.auto_apply ? 'YES' : 'NO (advisory only)'}</strong>
                  </div>
                  {surgical.investigation && (
                    <div style={{
                      marginTop: 8, fontSize: 11, color: 'var(--text-secondary)',
                      padding: 8, background: 'var(--bg-secondary)', borderRadius: 4,
                    }}>
                      <strong>Investigate:</strong> {surgical.investigation}
                    </div>
                  )}
                </>
              )}
            </Card>
          </Section>
        </div>
      </div>

      {/* Confidence histogram */}
      <Section title="Confidence Distribution"
               subtitle="All evals vs non-HOLD vs submitted — overlap reveals the gap">
        <Card>
          <ConfidenceHistogram histograms={histograms} />
        </Card>
      </Section>

      {/* Counterfactual */}
      <Section title="Counterfactual: removing one blocker"
               subtitle="What would block next if we waived the named rule?">
        <Card>
          <CounterfactualChart cf={cf} />
        </Card>
      </Section>

      {/* Cooldown audit */}
      <Section title="Cooldown audit"
               subtitle="Per-ticker rate-limiter activity">
        <Card>
          {!cooldown && <EmptyState icon="∅" message="No cooldown stats." />}
          {cooldown && (
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(180px,1fr))',
              gap: 12,
            }}>
              <Stat label="Cooldown hits" value={fmtN(cooldown.n_cooldown_hits)} mono />
              <Stat label="Lost opportunities" value={fmtN(cooldown.n_lost_opportunities)} mono
                    hint="Best-effort count — see endpoint note." />
              <Stat label="Affected tickers"
                    value={cooldown.affected_tickers?.length || 0} mono />
              <Stat label="Avg cooldown (s)"
                    value={cooldown.avg_cooldown_seconds != null ? cooldown.avg_cooldown_seconds.toFixed(0) : '—'} mono />
              <div style={{
                gridColumn: '1/-1', fontSize: 11, color: 'var(--text-muted)',
                fontStyle: 'italic',
              }}>{cooldown.note}</div>
            </div>
          )}
        </Card>
      </Section>

      {/* Metadata */}
      <Section title="Source meta">
        <Card>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px,1fr))', gap: 8 }}>
            <div>Source: <span className="mono">{data?.source || '—'}</span></div>
            <div>Persisted: <span className="mono">{String(data?.persisted ?? '—')}</span></div>
            <div>Computed at: <span className="mono">{report?.computed_at || '—'}</span></div>
            <div>Window days: <span className="mono">{report?.window_days || '—'}</span></div>
            {(report?.notes || []).map((n, i) => (
              <div key={i} style={{ gridColumn: '1/-1' }}>Note: <span className="mono">{n}</span></div>
            ))}
          </div>
        </Card>
      </Section>
    </div>
  );
}
