/**
 * MITS Phase 12.J — Detector Scorecard page.
 *
 * Surfaces /detectors/edge — per-detector 5d win rate vs the corpus
 * baseline (68.9 percent by default; operator-tunable). Color-coded
 * by label: strong / marginal / noise / negative. Sortable + family
 * filter. Click a row to drill into the per-ticker breakdown via the
 * existing /detectors/{name}/scorecard endpoint.
 *
 * Auto-suggest disable: any detector below baseline with N>=500 gets
 * a "suggest disable" badge so the operator can decide.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

async function api(path) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

const LABEL_STYLES = {
  strong:   { background: '#10b98122', color: '#10b981', border: '1px solid #10b981' },
  marginal: { background: '#06b6d422', color: '#06b6d4', border: '1px solid #06b6d4' },
  noise:    { background: '#a3a3a322', color: '#a3a3a3', border: '1px solid #a3a3a3' },
  negative: { background: '#ef444422', color: '#ef4444', border: '1px solid #ef4444' },
  no_data:  { background: '#37415122', color: '#9ca3af', border: '1px solid #37415133' },
};

function LabelChip({ label }) {
  const style = LABEL_STYLES[label] || LABEL_STYLES.no_data;
  return (
    <span style={{
      ...style, padding: '3px 9px', borderRadius: 12, fontSize: 11,
      fontWeight: 700, letterSpacing: '0.04em', textTransform: 'uppercase',
    }}>
      {label.replace('_', ' ')}
    </span>
  );
}

function EdgeBar({ edgePp }) {
  if (edgePp == null) return <span style={{ color: '#9ca3af' }}>—</span>;
  const color = edgePp >= 5 ? '#10b981'
              : edgePp >= 0 ? '#06b6d4'
              : edgePp >= -2 ? '#a3a3a3'
              : '#ef4444';
  const width = Math.min(100, Math.abs(edgePp) * 5);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 120 }}>
      <div style={{ flexGrow: 1, background: '#1f2937', borderRadius: 4, height: 8, position: 'relative' }}>
        <div style={{
          background: color, width: `${width}%`, height: '100%',
          borderRadius: 4,
          marginLeft: edgePp < 0 ? `${100 - width}%` : 0,
        }} />
      </div>
      <span style={{ color, fontWeight: 700, minWidth: 56, textAlign: 'right' }}>
        {edgePp > 0 ? '+' : ''}{edgePp.toFixed(2)} pp
      </span>
    </div>
  );
}

export default function DetectorScorecard() {
  const [data, setData] = useState(null);
  const [families, setFamilies] = useState(null);
  const [err, setErr] = useState(null);
  const [familyFilter, setFamilyFilter] = useState('all');
  const [labelFilter, setLabelFilter] = useState('all');
  const [sortBy, setSortBy] = useState('edge');
  const [sortDir, setSortDir] = useState('desc');

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [d, f] = await Promise.all([
          api('/detectors/edge'),
          api('/detectors/edge/families'),
        ]);
        if (alive) {
          setData(d);
          setFamilies(f);
        }
      } catch (e) {
        if (alive) setErr(String(e));
      }
    })();
    return () => { alive = false; };
  }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    let r = data.detectors;
    if (familyFilter !== 'all') r = r.filter((d) => d.family === familyFilter);
    if (labelFilter !== 'all') r = r.filter((d) => d.label === labelFilter);
    const dir = sortDir === 'asc' ? 1 : -1;
    r = [...r].sort((a, b) => {
      const key = sortBy === 'edge' ? 'edge_pp_vs_baseline'
                : sortBy === 'n' ? 'sample_size'
                : sortBy === 'wr' ? 'win_rate_5d'
                : 'name';
      const av = a[key] ?? -Infinity;
      const bv = b[key] ?? -Infinity;
      if (key === 'name') return dir * String(av).localeCompare(String(bv));
      return dir * ((av > bv ? 1 : -1));
    });
    return r;
  }, [data, familyFilter, labelFilter, sortBy, sortDir]);

  const allFamilies = useMemo(() => {
    if (!families) return [];
    return families.families.map((f) => f.family).sort();
  }, [families]);

  if (err) return <div style={{ padding: 24, color: '#ef4444' }}>Error: {err}</div>;
  if (!data) return <div style={{ padding: 24 }}>Loading detector edge…</div>;

  const baseline = data.baseline_5d;

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                          alignItems: 'baseline', marginBottom: 12 }}>
        <h1 style={{ fontSize: 22, margin: 0 }}>
          Detector Edge Scorecard
        </h1>
        <span style={{ color: '#9ca3af', fontSize: 13 }}>
          baseline 5d win rate = {(baseline * 100).toFixed(1)}%
        </span>
      </div>

      {/* family rollup strip */}
      {families && (
        <div style={{ display: 'flex', gap: 8, overflowX: 'auto',
                            marginBottom: 16, paddingBottom: 8 }}>
          {families.families.map((f) => (
            <div key={f.family} style={{
              padding: '8px 12px', borderRadius: 8,
              background: '#111827', minWidth: 160,
              border: '1px solid #1f2937',
            }}>
              <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 4 }}>
                {f.family}
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between',
                                  alignItems: 'baseline' }}>
                <span style={{ fontSize: 18, fontWeight: 700 }}>
                  {f.win_rate_5d != null ? (f.win_rate_5d * 100).toFixed(1) + '%' : '—'}
                </span>
                <LabelChip label={f.label} />
              </div>
              <div style={{ fontSize: 11, color: '#6b7280', marginTop: 4 }}>
                N={f.total_n} • {f.detector_count} detectors
              </div>
            </div>
          ))}
        </div>
      )}

      {/* filters */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
        <select value={familyFilter} onChange={(e) => setFamilyFilter(e.target.value)}>
          <option value="all">all families</option>
          {allFamilies.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <select value={labelFilter} onChange={(e) => setLabelFilter(e.target.value)}>
          <option value="all">all labels</option>
          <option value="strong">strong</option>
          <option value="marginal">marginal</option>
          <option value="noise">noise</option>
          <option value="negative">negative</option>
          <option value="no_data">no_data</option>
        </select>
        <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}>
          <option value="edge">sort: edge</option>
          <option value="n">sort: N</option>
          <option value="wr">sort: win rate</option>
          <option value="name">sort: name</option>
        </select>
        <button onClick={() => setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))}>
          {sortDir === 'asc' ? '↑' : '↓'}
        </button>
      </div>

      {/* table */}
      <div style={{ background: '#0a0a0a', borderRadius: 8, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#111827', color: '#9ca3af' }}>
              <th style={{ textAlign: 'left', padding: 10 }}>Detector</th>
              <th style={{ textAlign: 'left', padding: 10 }}>Family</th>
              <th style={{ textAlign: 'right', padding: 10 }}>N (5d)</th>
              <th style={{ textAlign: 'right', padding: 10 }}>Win Rate</th>
              <th style={{ textAlign: 'left', padding: 10, width: 220 }}>Edge vs Baseline</th>
              <th style={{ textAlign: 'center', padding: 10 }}>Label</th>
              <th style={{ textAlign: 'center', padding: 10 }}>Enabled</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const wrPct = r.win_rate_5d != null ? (r.win_rate_5d * 100).toFixed(1) + '%' : '—';
              const ciLo = r.ci_lower != null ? (r.ci_lower * 100).toFixed(1) : '—';
              const ciHi = r.ci_upper != null ? (r.ci_upper * 100).toFixed(1) : '—';
              const suggestDisable = r.enabled
                && r.sample_size >= 500
                && r.edge_pp_vs_baseline != null
                && r.edge_pp_vs_baseline < 0;
              return (
                <tr key={r.name} style={{ borderTop: '1px solid #1f2937' }}>
                  <td style={{ padding: 10 }}>
                    <Link to={`/detectors/${r.name}`} style={{ color: '#60a5fa',
                                                                              textDecoration: 'none',
                                                                              fontWeight: 600 }}>
                      {r.name}
                    </Link>
                    {suggestDisable && (
                      <span style={{ marginLeft: 8, fontSize: 10,
                                              padding: '2px 6px', borderRadius: 8,
                                              background: '#ef444422', color: '#ef4444',
                                              fontWeight: 700 }}
                                title="N>=500 + below baseline — consider disabling">
                        SUGGEST DISABLE
                      </span>
                    )}
                    <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>
                      {r.description?.slice(0, 100)}
                      {r.description && r.description.length > 100 ? '…' : ''}
                    </div>
                  </td>
                  <td style={{ padding: 10, color: '#9ca3af' }}>{r.family}</td>
                  <td style={{ padding: 10, textAlign: 'right' }}>{r.sample_size}</td>
                  <td style={{ padding: 10, textAlign: 'right' }}>
                    {wrPct}
                    <div style={{ fontSize: 10, color: '#6b7280' }}>
                      [{ciLo}, {ciHi}]
                    </div>
                  </td>
                  <td style={{ padding: 10 }}><EdgeBar edgePp={r.edge_pp_vs_baseline} /></td>
                  <td style={{ padding: 10, textAlign: 'center' }}><LabelChip label={r.label} /></td>
                  <td style={{ padding: 10, textAlign: 'center' }}>
                    {r.enabled ? <span style={{ color: '#10b981' }}>●</span>
                                       : <span style={{ color: '#ef4444' }}>○</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 12, fontSize: 11, color: '#6b7280' }}>
        {rows.length} detectors • thresholds: strong &gt;+{data.thresholds.strong_pp}pp,
        marginal &gt;{data.thresholds.marginal_pp}pp, negative &lt;{data.thresholds.negative_pp}pp
      </div>
    </div>
  );
}
