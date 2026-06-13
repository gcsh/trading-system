/**
 * TradeAutopsy — Stage-9 + Stage-17 loss-autopsy memo gallery.
 *
 * Reads /autopsy/recent and renders one card per losing trade with:
 *   - PNL, strategy, regime
 *   - Avoidable tag (avoidable | mixed | variance) + confidence
 *   - Flip hypotheses (counterfactual: "would event-hold have blocked this?")
 *   - Plain-English summary
 */
import React, { useEffect, useMemo, useState } from 'react';

const TAG_COLOR = {
  avoidable: '#ff5d5d',
  mixed: '#ffd84d',
  variance: '#5dc6ff',
};

function fmtDollar(v) {
  if (v == null || isNaN(v)) return '—';
  const n = Number(v);
  return `${n < 0 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;
}
function fmtPct(v) {
  if (v == null || isNaN(v)) return '—';
  return `${(Number(v) * 100).toFixed(1)}%`;
}
function fmtMinutes(v) {
  if (v == null) return '—';
  const m = Number(v);
  if (m < 60) return `${m}m`;
  const h = (m / 60).toFixed(1);
  return `${h}h`;
}


function AutopsyCard({ a }) {
  const tagColor = TAG_COLOR[a.avoidable_tag] || 'var(--muted)';
  const score = a.avoidable_score ?? 0;
  const flips = a.flip_hypotheses || [];
  const triggered = flips.filter((f) => f.triggered);

  return (
    <div className="panel" style={{ padding: 16, borderLeft: `4px solid ${tagColor}` }}>
      <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
        <span style={{
          fontSize: 9, padding: '2px 8px', borderRadius: 10,
          background: tagColor + '22', color: tagColor,
          fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          {a.avoidable_tag}
        </span>
        <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text)' }}>
          {a.ticker} <span style={{ color: 'var(--muted)' }}>· {a.strategy}</span>
        </div>
        <div style={{ marginLeft: 'auto', fontSize: 15, fontWeight: 700,
                            color: 'var(--danger)', fontFeatureSettings: '"tnum"' }}>
          {fmtDollar(a.pnl)} <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            ({fmtPct(a.pnl_pct)})
          </span>
        </div>
      </div>

      {a.summary && (
        <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-soft)',
                            lineHeight: 1.45 }}>
          {a.summary}
        </div>
      )}

      <div className="row" style={{ marginTop: 10, gap: 14, fontSize: 11, flexWrap: 'wrap' }}>
        <span><span style={{ color: 'var(--muted)' }}>action: </span><strong>{a.action}</strong></span>
        <span><span style={{ color: 'var(--muted)' }}>grade: </span><strong>{a.grade || '—'}</strong></span>
        <span><span style={{ color: 'var(--muted)' }}>win_p at entry: </span>
          <strong>{fmtPct(a.win_probability)}</strong></span>
        <span><span style={{ color: 'var(--muted)' }}>regime: </span><strong>{a.regime_label || '—'}</strong></span>
        <span><span style={{ color: 'var(--muted)' }}>holding: </span><strong>{fmtMinutes(a.holding_minutes)}</strong></span>
        <span><span style={{ color: 'var(--muted)' }}>exit: </span><strong>{a.exit_reason || '—'}</strong></span>
        <span style={{ marginLeft: 'auto', color: 'var(--muted)' }}>
          avoidable score {(score * 100).toFixed(0)}%
        </span>
      </div>

      {flips.length > 0 && (
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
          <div style={{ fontSize: 10, color: 'var(--muted)',
                            textTransform: 'uppercase', letterSpacing: '0.05em',
                            fontWeight: 600, marginBottom: 6 }}>
            Counterfactuals ({triggered.length}/{flips.length} would have flipped)
          </div>
          <div style={{ display: 'grid', gap: 4 }}>
            {flips.map((f, i) => (
              <div key={i} className="row" style={{ gap: 6, fontSize: 11 }}>
                <span style={{
                  fontSize: 13, color: f.triggered ? tagColor : 'var(--muted)',
                  minWidth: 18,
                }}>
                  {f.triggered ? '●' : '○'}
                </span>
                <span style={{ minWidth: 130, color: f.triggered ? 'var(--text)' : 'var(--muted)',
                                    fontWeight: f.triggered ? 600 : 400 }}>
                  {f.name}
                </span>
                <span style={{ color: 'var(--text-soft)' }}>{f.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {a.execution_quality && Object.keys(a.execution_quality).length > 0 && (
        <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid var(--border)',
                            fontSize: 11, color: 'var(--muted)' }}>
          <span>exec: </span>
          {Object.entries(a.execution_quality).map(([k, v]) =>
            `${k}=${typeof v === 'number' ? v.toFixed(2) : v}`
          ).join(' · ')}
        </div>
      )}
    </div>
  );
}


export default function TradeAutopsy() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    fetch('/autopsy/recent?limit=100')
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, []);

  const filtered = useMemo(() => {
    if (!data?.autopsies) return [];
    if (filter === 'all') return data.autopsies;
    return data.autopsies.filter((a) => a.avoidable_tag === filter);
  }, [data, filter]);

  if (err) return <div className="empty">autopsy error: {err}</div>;
  if (!data) return <div className="empty">Loading autopsies…</div>;
  if (!data.autopsies?.length) {
    return <div className="empty">No closed losses to autopsy yet — keep trading.</div>;
  }

  const counts = data.by_tag || {};

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <FilterChip active={filter === 'all'} onClick={() => setFilter('all')}
                    label={`All (${data.n_losses_analyzed})`} color="var(--text-soft)" />
        <FilterChip active={filter === 'avoidable'} onClick={() => setFilter('avoidable')}
                    label={`Avoidable (${counts.avoidable || 0})`} color={TAG_COLOR.avoidable} />
        <FilterChip active={filter === 'mixed'} onClick={() => setFilter('mixed')}
                    label={`Mixed (${counts.mixed || 0})`} color={TAG_COLOR.mixed} />
        <FilterChip active={filter === 'variance'} onClick={() => setFilter('variance')}
                    label={`Variance (${counts.variance || 0})`} color={TAG_COLOR.variance} />
      </div>

      <div style={{ display: 'grid', gap: 12 }}>
        {filtered.map((a) => <AutopsyCard key={a.trade_id} a={a} />)}
      </div>

      <div style={{ marginTop: 12, fontSize: 10, color: 'var(--muted-2)' }}>
        Tags: <strong>avoidable</strong> = pre-trade gates would have blocked; <strong>variance</strong> = honest
        losing trade; <strong>mixed</strong> = partial flips. Counterfactuals run pre-trade gates against post-hoc data.
      </div>
    </div>
  );
}


function FilterChip({ active, onClick, label, color }) {
  return (
    <button onClick={onClick} className="btn small"
            style={{
              background: active ? `${color}22` : 'transparent',
              border: `1px solid ${active ? color : 'var(--border)'}`,
              color: active ? color : 'var(--text-soft)',
              fontWeight: active ? 700 : 500,
            }}>
      {label}
    </button>
  );
}
