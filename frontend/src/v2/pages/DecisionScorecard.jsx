/* MITS Phase 19 Cluster C — Decision Scorecard v2 (/v2/decision/scorecard).
 *
 * Surfaces /decision/scorecard?window=N for the operator to inspect
 * decision quality over time. The endpoint returns aggregates only
 * (mean / median / stddev) for composite_distribution, not the raw
 * series — so we render a 4-stat KPI strip + 4 sub-score panels +
 * calibration + expectancy charts. If the bins are all-empty (no
 * closed trades yet in any quality band) we show EmptyState so the
 * operator knows "this will populate once trades close" without
 * thinking the page is broken.
 *
 * Plain-English tooltips on Brier / ECE / composite / etc. live on
 * every chart header so the operator never needs a stats refresher.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import CalibrationChart from '../components/CalibrationChart.jsx';

const WINDOWS = [50, 100, 250, 500, 1000];

const TOOLTIPS = {
  composite:    'Composite Quality = weighted blend of analysis, council agreement, risk, and execution sub-scores (0-100). 60+ is "edge" territory.',
  analysis:     'Analysis Quality measures how thorough the per-ticker analysis was: data freshness, axis coverage, regime confidence.',
  council:      'Council Agreement measures how aligned the 8 agents were on the final stance (1.0 = unanimous; lower = split).',
  risk:         'Risk Quality measures whether the risk manager had clean inputs: drawdown headroom, correlation cap, sizing inputs.',
  execution:    'Execution Quality measures the realised fill: spread crossed, latency, slippage vs mid.',
  calibration:  'Calibration: do high-composite-quality decisions actually win more often? On the dashed line = perfectly calibrated.',
  expectancy:   'Expectancy by composite-quality bin. Higher bars in the right-hand bins = the score predicts realised P&L.',
};

async function api(path) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

function fmtPct1(v) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toFixed(1);
}

/* ── KPI strip ──────────────────────────────────────────────────────── */
function KPIStrip({ scorecard }) {
  const dist = scorecard?.composite_distribution || {};
  const bins = scorecard?.calibration_bins || [];
  const nTotal = (scorecard?.n_rows ?? 0);

  // % above 60: bin labels "60-70" / "70-80" / "80-90" / "90-100"
  const edgeN = bins.filter(b => {
    const lo = Number((b.bin || '0-0').split('-')[0]);
    return lo >= 60;
  }).reduce((acc, b) => acc + (b.n || 0), 0);

  const edgePct = nTotal > 0 ? (edgeN / nTotal) * 100 : 0;

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fit, minmax(170px,1fr))',
      gap: 12,
    }}>
      <Card>
        <Stat label="Composite Mean"
              value={fmtPct1(dist.mean)} mono
              hint={TOOLTIPS.composite} />
      </Card>
      <Card>
        <Stat label="Composite Median"
              value={fmtPct1(dist.median)} mono
              hint={TOOLTIPS.composite} />
      </Card>
      <Card>
        <Stat label="N Rows" value={nTotal} mono
              hint="Number of decisions in the selected window." />
      </Card>
      <Card glow={edgePct > 30 ? 'green' : 'none'}>
        <Stat label="% Above 60 (Edge)"
              value={`${edgePct.toFixed(1)}%`} mono
              hint="Share of decisions whose composite quality landed in the 60+ bands. Bot's edge band." />
      </Card>
      <Card>
        <Stat label="Stddev"
              value={fmtPct1(dist.stddev)} mono
              hint="Composite-quality dispersion. Lower = more consistent decision quality." />
      </Card>
    </div>
  );
}

/* ── Composite distribution histogram (using calibration_bins as proxy) ── */
function CompositeHistogram({ bins, totalN }) {
  if (!bins || bins.length === 0 || totalN === 0) {
    return <EmptyState icon="∅" message="No decision rows in this window yet." />;
  }
  const max = Math.max(...bins.map(b => b.n || 0), 1);
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 180, padding: '8px 4px' }}>
      {bins.map((b, i) => {
        const h = ((b.n || 0) / max) * 100;
        const lo = Number((b.bin || '0-0').split('-')[0]);
        const color = lo >= 60 ? 'var(--accent-green)'
                     : lo >= 40 ? 'var(--accent-cyan)'
                     : 'var(--accent-red)';
        return (
          <div key={i} style={{
            flex: 1, display: 'flex', flexDirection: 'column',
            alignItems: 'center', gap: 4,
          }} title={`${b.bin}: n=${b.n}`}>
            <div className="mono" style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>
              {b.n || 0}
            </div>
            <div style={{
              width: '100%',
              height: `${Math.max(2, h)}%`,
              background: color,
              opacity: 0.85,
              borderRadius: '3px 3px 0 0',
              minHeight: 2,
            }} />
            <div className="mono" style={{
              fontSize: 9, color: 'var(--text-muted)',
              writingMode: 'horizontal-tb',
            }}>{b.bin}</div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Expectancy by bin bar chart ──────────────────────────────────── */
function ExpectancyBars({ bins }) {
  const valid = (bins || []).filter(b => b.mean_pnl_pct != null);
  if (valid.length === 0) {
    return <EmptyState icon="∅"
      message="No closed-trade P&L per quality bin yet." />;
  }
  const max = Math.max(...valid.map(b => Math.abs(b.mean_pnl_pct)), 1);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '8px 4px' }}>
      {bins.map((b, i) => {
        const pnl = b.mean_pnl_pct;
        const h = pnl == null ? 0 : (Math.abs(pnl) / max) * 70;
        const positive = (pnl || 0) >= 0;
        return (
          <div key={i} style={{
            flex: 1, display: 'flex', flexDirection: 'column',
            alignItems: 'center', minHeight: 160, justifyContent: 'center',
          }} title={`${b.bin}: n=${b.n}, mean=${pnl != null ? pnl.toFixed(2) + '%' : '—'}`}>
            <div className="mono" style={{ fontSize: 10, color: 'var(--text-tertiary)', height: 14 }}>
              {pnl != null ? `${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%` : '—'}
            </div>
            <div style={{
              width: '100%',
              flex: 1, display: 'flex', flexDirection: 'column',
              justifyContent: 'center', alignItems: 'stretch',
            }}>
              <div style={{ flex: 1, display: 'flex', alignItems: 'flex-end' }}>
                {positive && (
                  <div style={{
                    width: '100%', height: `${h}%`,
                    background: 'var(--accent-green)',
                    borderRadius: '3px 3px 0 0',
                  }}/>
                )}
              </div>
              <div style={{ height: 1, background: 'var(--border-default)' }} />
              <div style={{ flex: 1 }}>
                {!positive && pnl != null && (
                  <div style={{
                    width: '100%', height: `${h}%`,
                    background: 'var(--accent-red)',
                    borderRadius: '0 0 3px 3px',
                  }}/>
                )}
              </div>
            </div>
            <div className="mono" style={{ fontSize: 9, color: 'var(--text-muted)' }}>
              {b.bin}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Sub-score mini panel ─────────────────────────────────────────── */
function SubScorePanel({ name, label, sub, tip }) {
  if (!sub || sub.mean == null) {
    return (
      <Card>
        <div className="v2-stat__label" title={tip}>{label}</div>
        <EmptyState icon="∅" message="No samples." />
      </Card>
    );
  }
  return (
    <Card>
      <div className="v2-stat__label" title={tip}>{label}</div>
      <div className="mono" style={{
        fontSize: 28, fontWeight: 700, color: 'var(--text-primary)',
        marginTop: 4,
      }}>
        {sub.mean.toFixed(1)}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>
        median {sub.median != null ? sub.median.toFixed(1) : '—'}
      </div>
      <div style={{ marginTop: 8 }}>
        <div style={{
          height: 6, background: 'var(--bg-secondary)', borderRadius: 3,
          overflow: 'hidden',
        }}>
          <div style={{
            height: '100%',
            width: `${Math.min(100, Math.max(0, sub.mean))}%`,
            background: sub.mean >= 60 ? 'var(--accent-green)'
                        : sub.mean >= 40 ? 'var(--accent-cyan)'
                        : 'var(--accent-red)',
          }}/>
        </div>
      </div>
    </Card>
  );
}

/* ── Page ────────────────────────────────────────────────────────── */
export default function DecisionScorecardV2() {
  const [window, setWindow] = useState(50);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const j = await api(`/decision/scorecard?window=${window}`);
      setData(j); setErr(null);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [window]);

  useEffect(() => { load(); }, [load]);

  return (
    <div style={{ padding: 'var(--space-6)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 20, gap: 16 }}>
        <h1 style={{
          fontSize: 'var(--font-size-xl)', fontWeight: 800,
          color: 'var(--text-primary)', margin: 0, letterSpacing: '0.02em',
          textTransform: 'uppercase',
        }}>Decision Scorecard</h1>
        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>
          Composite quality + sub-scores + calibration over the last {window} decisions.
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          {WINDOWS.map(w => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              data-testid={`window-${w}`}
              style={{
                background: window === w ? 'var(--accent-cyan-dim)' : 'var(--bg-elevated)',
                color: window === w ? 'var(--bg-primary)' : 'var(--text-secondary)',
                border: '1px solid ' + (window === w ? 'var(--accent-cyan)' : 'var(--border-default)'),
                borderRadius: 999,
                padding: '5px 14px',
                fontSize: 11.5,
                fontWeight: 700,
                cursor: 'pointer',
              }}
            >{w === 1000 ? 'All' : w}</button>
          ))}
        </div>
      </div>

      {err && (
        <div className="v2-alert v2-alert--critical" style={{ marginBottom: 16 }}>
          Failed to load /decision/scorecard?window={window}: {err}
        </div>
      )}

      {/* KPI strip */}
      <Section title="Composite at a glance">
        <KPIStrip scorecard={data || {}} />
      </Section>

      {/* Composite distribution */}
      <Section
        title="Composite Quality Distribution"
        subtitle="How many decisions landed in each quality band">
        <Card>
          <div style={{
            display: 'flex', alignItems: 'baseline', marginBottom: 6,
          }}>
            <span style={{
              fontSize: 11, color: 'var(--text-tertiary)',
            }} title={TOOLTIPS.composite}>
              Distribution over composite-quality bins (10-band histogram)
            </span>
            {loading && (
              <Pill tone="info" size="sm" style={{ marginLeft: 'auto' }}>
                Loading…
              </Pill>
            )}
          </div>
          <CompositeHistogram
            bins={data?.calibration_bins}
            totalN={data?.n_rows}
          />
        </Card>
      </Section>

      {/* Sub-scores */}
      <Section title="Sub-Score Means"
               subtitle="Each panel = one of the 4 quality dimensions">
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px,1fr))',
          gap: 12,
        }}>
          <SubScorePanel name="analysis_quality" label="Analysis Quality"
                         sub={data?.by_sub_score?.analysis_quality}
                         tip={TOOLTIPS.analysis} />
          <SubScorePanel name="council_agreement" label="Council Agreement"
                         sub={data?.by_sub_score?.council_agreement}
                         tip={TOOLTIPS.council} />
          <SubScorePanel name="risk_quality" label="Risk Quality"
                         sub={data?.by_sub_score?.risk_quality}
                         tip={TOOLTIPS.risk} />
          <SubScorePanel name="execution_quality" label="Execution Quality"
                         sub={data?.by_sub_score?.execution_quality}
                         tip={TOOLTIPS.execution} />
        </div>
      </Section>

      {/* Calibration */}
      <Section title="Calibration"
               subtitle="Did high-composite decisions actually win more often?">
        <Card>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 6 }}
               title={TOOLTIPS.calibration}>
            X-axis: composite-quality bin midpoint. Y-axis: realised win rate %.
            Dashed line = perfect calibration.
          </div>
          <CalibrationChart bins={data?.calibration_bins} mode="win_rate" />
        </Card>
      </Section>

      {/* Expectancy */}
      <Section title="Expectancy By Bin"
               subtitle="Mean realised P&L percentage per composite-quality bucket">
        <Card>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 6 }}
               title={TOOLTIPS.expectancy}>
            Green = bin made money on average; red = bin lost money. Skewed-right curve = signal.
          </div>
          <ExpectancyBars bins={data?.expectancy_by_bin} />
        </Card>
      </Section>
    </div>
  );
}
