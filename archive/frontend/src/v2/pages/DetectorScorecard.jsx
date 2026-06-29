/* MITS Phase 19 Cluster C — Detector Scorecard v2 (/v2/detectors).
 *
 * Per-detector performance grid backed by /detectors/edge. The legacy
 * page was a long table; v2 splits it into:
 *
 *   KPI strip   — total active, avg posterior_wr, total observations (14d)
 *   Filter chips — by detector.family
 *   Matrix       — DetectorMatrix component (rows=detectors × cols=directions)
 *   Right rail   — top-10 by edge for the active filter
 *   Drill panel  — click a matrix row to see Wilson CI + direction split
 *
 * "Suggest disable?" — surfaced when a detector with n >= 500 sits
 * below baseline (the existing /detectors/edge `label` already returns
 * "negative" in that case so we just pivot off it).
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState,
} from '../../design/Components.jsx';
import DetectorMatrix from '../components/DetectorMatrix.jsx';

async function api(path) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

const FAMILY_DEFAULTS = [
  'all', 'smc', 'wyckoff', 'volume_profile', 'volume_profile_v2',
  'catalyst', 'macro_regime', 'quantitative', 'pine_custom',
  'candlesticks', 'options_intel', 'flow_intel', 'market_structure',
  'price_action', 'liquidity', 'vwap',
];

function fmtPct1(v) {
  if (v == null || !isFinite(v)) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}

function DrillPanel({ detector, edges }) {
  if (!detector) {
    return (
      <Card>
        <EmptyState icon="◉" message="Click a row in the matrix to see its details." />
      </Card>
    );
  }
  // edges = the per-direction entries for this detector
  const dirCells = (edges || []).filter(d => d.name === detector.name);
  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <h3 style={{
          margin: 0, fontSize: 14, color: 'var(--text-primary)',
          fontWeight: 700,
        }}>{detector.name}</h3>
        <Pill tone="info">{detector.family}</Pill>
      </div>
      {dirCells.length === 0 && (
        <EmptyState icon="∅" message="No edge rows for this detector." />
      )}
      {dirCells.map(c => {
        const negative = c.label === 'negative';
        const suggestDisable = negative && c.sample_size >= 500;
        return (
          <div key={c.direction || 'null'} style={{
            marginBottom: 10, paddingBottom: 10,
            borderBottom: '1px solid var(--border-subtle)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="mono" style={{
                fontSize: 12, color: 'var(--accent-cyan)', fontWeight: 700,
              }}>{c.direction || 'null'}</span>
              <Pill tone={
                c.label === 'strong' ? 'success'
                : c.label === 'marginal' ? 'info'
                : c.label === 'negative' ? 'error'
                : 'neutral'
              }>{c.label}</Pill>
              {suggestDisable && (
                <Pill tone="warning" size="md">SUGGEST DISABLE</Pill>
              )}
            </div>
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(4,minmax(0,1fr))',
              gap: 8, marginTop: 8,
            }}>
              <div>
                <div className="v2-stat__label">Win rate (5d)</div>
                <div className="mono" style={{ fontSize: 13 }}>{fmtPct1(c.win_rate_5d)}</div>
              </div>
              <div>
                <div className="v2-stat__label">Edge vs baseline</div>
                <div className="mono" style={{
                  fontSize: 13,
                  color: c.edge_pp_vs_baseline > 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                }}>
                  {c.edge_pp_vs_baseline != null
                    ? `${c.edge_pp_vs_baseline > 0 ? '+' : ''}${c.edge_pp_vs_baseline.toFixed(2)}pp`
                    : '—'}
                </div>
              </div>
              <div>
                <div className="v2-stat__label">Wilson CI</div>
                <div className="mono" style={{ fontSize: 12 }}>
                  {c.ci_lower != null && c.ci_upper != null
                    ? `${(c.ci_lower * 100).toFixed(0)}–${(c.ci_upper * 100).toFixed(0)}%`
                    : '—'}
                </div>
              </div>
              <div>
                <div className="v2-stat__label">Sample</div>
                <div className="mono" style={{ fontSize: 13 }}>{c.sample_size}</div>
              </div>
            </div>
          </div>
        );
      })}
    </Card>
  );
}

export default function DetectorScorecardV2() {
  const [edge, setEdge] = useState(null);
  const [err, setErr] = useState(null);
  const [family, setFamily] = useState('all');
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const e = await api('/detectors/edge');
        if (alive) { setEdge(e); setErr(null); }
      } catch (er) {
        if (alive) setErr(String(er.message || er));
      }
    })();
    return () => { alive = false; };
  }, []);

  const detectors = edge?.detectors || [];

  // Discover unique families dynamically — fall back to the static list
  const families = useMemo(() => {
    const set = new Set(detectors.map(d => d.family).filter(Boolean));
    const found = Array.from(set).sort();
    return found.length ? ['all', ...found] : FAMILY_DEFAULTS;
  }, [detectors]);

  // KPI metrics
  const filtered = useMemo(() => {
    if (family === 'all') return detectors;
    return detectors.filter(d => d.family === family);
  }, [detectors, family]);

  const totalActive = filtered.filter(d => d.enabled).length;
  const samplesSum = filtered.reduce((acc, d) => acc + (d.sample_size || 0), 0);
  const wrSum = filtered.reduce((acc, d) => acc + (d.win_rate_5d != null ? d.win_rate_5d : 0), 0);
  const wrCount = filtered.filter(d => d.win_rate_5d != null).length;
  const avgWr = wrCount > 0 ? wrSum / wrCount : 0;

  // Top-10 by edge
  const top10 = useMemo(() => {
    return filtered.slice()
      .filter(d => d.edge_pp_vs_baseline != null)
      .sort((a, b) => (b.edge_pp_vs_baseline || 0) - (a.edge_pp_vs_baseline || 0))
      .slice(0, 10);
  }, [filtered]);

  return (
    <div style={{ padding: 'var(--space-6)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 16, gap: 16 }}>
        <h1 style={{
          fontSize: 'var(--font-size-xl)', fontWeight: 800,
          color: 'var(--text-primary)', margin: 0,
          letterSpacing: '0.02em', textTransform: 'uppercase',
        }}>Detector Scorecard</h1>
        <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>
          5-day win rate vs corpus baseline. Wilson 95% CI. Click a row to drill in.
        </div>
      </div>

      {err && (
        <div className="v2-alert v2-alert--critical" style={{ marginBottom: 16 }}>{err}</div>
      )}

      {/* KPI strip */}
      <Section title="At a glance">
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(170px,1fr))',
          gap: 12,
        }}>
          <Card>
            <Stat label="Detectors (enabled)"
                  value={totalActive} mono
                  hint="Detectors flagged enabled in the registry that match the current family filter." />
          </Card>
          <Card>
            <Stat label="Avg win rate (5d)"
                  value={fmtPct1(avgWr)} mono
                  hint="Mean of per-detector 5-day win rates within the filter." />
          </Card>
          <Card>
            <Stat label="Total observations"
                  value={samplesSum.toLocaleString()} mono
                  hint="Sum of detector sample sizes over the 5-day measurement window." />
          </Card>
          <Card>
            <Stat label="Families"
                  value={families.filter(f => f !== 'all').length} mono
                  hint="Distinct detector families in scope." />
          </Card>
        </div>
      </Section>

      {/* Filter chips */}
      <Section title="Filter by family">
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {families.map(f => {
            const active = family === f;
            return (
              <button
                key={f}
                onClick={() => setFamily(f)}
                data-testid={`family-${f}`}
                style={{
                  background: active ? 'var(--accent-cyan-dim)' : 'var(--bg-elevated)',
                  color: active ? 'var(--bg-primary)' : 'var(--text-secondary)',
                  border: '1px solid ' + (active ? 'var(--accent-cyan)' : 'var(--border-default)'),
                  borderRadius: 999,
                  padding: '4px 12px',
                  fontSize: 11,
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  letterSpacing: '0.04em',
                  cursor: 'pointer',
                }}>
                {f}
              </button>
            );
          })}
        </div>
      </Section>

      {/* Matrix + right rail */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0,3fr) minmax(280px,1.4fr)',
        gap: 16,
      }}>
        <div>
          <Section title="Detector Edge Matrix"
                   subtitle="Cell color = win-rate edge vs baseline; opacity = sample size">
            <DetectorMatrix
              detectors={filtered}
              familyFilter="all"
              baselines={edge?.baselines_5d}
              onSelect={setSelected}
            />
          </Section>
        </div>
        <div>
          <Section title="Top 10 by edge"
                   subtitle="Within the active filter">
            <Card>
              {top10.length === 0 && (
                <EmptyState icon="∅" message="No detectors with edge data in filter." />
              )}
              {top10.map((d, i) => (
                <div key={d.name + d.direction + i}
                     onClick={() => setSelected({ name: d.name, family: d.family })}
                     style={{
                       display: 'flex', alignItems: 'center', gap: 8,
                       padding: '6px 0',
                       borderBottom: '1px solid var(--border-subtle)',
                       cursor: 'pointer',
                     }}>
                  <span className="mono" style={{
                    width: 18, color: 'var(--text-muted)', fontSize: 11,
                  }}>{i + 1}.</span>
                  <span className="mono" style={{
                    flex: 1, fontSize: 12, color: 'var(--text-primary)',
                  }}>{d.name}</span>
                  <span className="mono" style={{
                    color: 'var(--accent-green)', fontSize: 12, fontWeight: 700,
                  }}>+{d.edge_pp_vs_baseline.toFixed(1)}pp</span>
                </div>
              ))}
            </Card>
          </Section>

          {/* Drill panel */}
          <Section title="Detail">
            <DrillPanel detector={selected} edges={detectors} />
          </Section>
        </div>
      </div>
    </div>
  );
}
