/**
 * IVRegime — P2.3 universe regime map.
 *
 * Reads /iv-regime/universe/all and renders a grid of ticker tiles, each
 * showing the current 6-way regime + confidence + supporting features.
 * Click a tile to see the slope / autocorr / std panel.
 */
import React, { useEffect, useMemo, useState } from 'react';

const REGIME_META = {
  mean_reverting: { label: 'Mean Reverting', color: '#5dc6ff', desc: 'IV oscillates around a mean — short-vol setups work; sized appropriately.' },
  trending_up: { label: 'Trending Up', color: '#ffd84d', desc: 'IV is rising — long-vol bias; selling premium here gets punished.' },
  trending_down: { label: 'Trending Down', color: '#9be65a', desc: 'IV is falling — long premium hurts; short-vol harvests theta + IV-drop.' },
  expanding: { label: 'Expanding', color: '#ff5d5d', desc: 'Volatility-of-volatility rising — gamma squeeze risk for premium sellers.' },
  stable_low: { label: 'Stable Low', color: '#a98bff', desc: 'Persistent, low-variance band — quiet tape; size up modestly.' },
  unknown: { label: 'Unknown', color: 'var(--muted)', desc: 'Not enough history yet — fall back to other signals.' },
};

function fmtPct(v) {
  if (v == null || isNaN(v)) return '—';
  return `${(v * 100).toFixed(1)}%`;
}
function fmtNum(v, digits = 4) {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}

function RegimeTile({ ticker, report, selected, onClick }) {
  const meta = REGIME_META[report?.regime] || REGIME_META.unknown;
  const conf = Number(report?.confidence || 0);
  return (
    <button
      onClick={onClick}
      className="panel"
      style={{
        padding: '10px 12px', textAlign: 'left',
        background: selected ? 'var(--panel-2)' : 'var(--panel)',
        border: selected ? `1px solid ${meta.color}` : '1px solid var(--border)',
        borderLeft: `3px solid ${meta.color}`,
        cursor: 'pointer',
        minWidth: 150,
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)' }}>{ticker}</div>
      <div style={{ fontSize: 11, color: meta.color, fontWeight: 600, marginTop: 2 }}>
        {meta.label}
      </div>
      <div style={{
        marginTop: 4, height: 4, background: 'var(--panel-2)', borderRadius: 2,
        overflow: 'hidden',
      }}>
        <div style={{
          width: `${Math.max(2, conf * 100)}%`, height: 4,
          background: meta.color, opacity: 0.8,
        }} />
      </div>
      <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 3 }}>
        conf {fmtPct(conf)} · n={report?.sample_count ?? 0}
      </div>
    </button>
  );
}

function DetailPanel({ ticker, report }) {
  if (!ticker || !report) {
    return (
      <div className="empty" style={{ padding: 20, fontSize: 12 }}>
        Click a ticker tile to see classifier details.
      </div>
    );
  }
  const meta = REGIME_META[report.regime] || REGIME_META.unknown;
  return (
    <div className="panel" style={{ padding: 16 }}>
      <div className="row" style={{ alignItems: 'baseline', gap: 12 }}>
        <div style={{ fontSize: 22, fontWeight: 700 }}>{ticker}</div>
        <div style={{
          fontSize: 13, padding: '2px 10px', borderRadius: 14,
          background: meta.color + '22', color: meta.color, fontWeight: 600,
        }}>
          {meta.label}
        </div>
        <div style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)' }}>
          {report.sample_count} obs · confidence {fmtPct(report.confidence)}
        </div>
      </div>
      <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-soft)' }}>
        {meta.desc}
      </div>
      {report.note && (
        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--muted)', fontStyle: 'italic' }}>
          {report.note}
        </div>
      )}
      <div style={{
        marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
        gap: 8, fontSize: 11,
      }}>
        <Stat label="Current IV" value={fmtPct(report.current_iv)} />
        <Stat label="Mean IV (window)" value={fmtPct(report.mean_iv)} />
        <Stat label="Std IV" value={fmtNum(report.std_iv, 4)} />
        <Stat label="Slope (per day)" value={fmtNum(report.slope, 6)} />
        <Stat label="Autocorr (lag-1)" value={fmtNum(report.autocorr_lag1, 3)} />
        <Stat label="Recent vs trailing σ" value={
          report.recent_std != null && report.trailing_std
            ? `${fmtNum(report.recent_std, 4)} / ${fmtNum(report.trailing_std, 4)}`
            : '—'
        } />
      </div>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div style={{
      padding: '8px 10px', background: 'var(--panel-2)',
      borderRadius: 6, border: '1px solid var(--border)',
    }}>
      <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase',
                       letterSpacing: '0.05em' }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 600, marginTop: 2,
                       fontFeatureSettings: '"tnum"' }}>{value}</div>
    </div>
  );
}


export default function IVRegime() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/iv-regime/universe/all')
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((d) => {
        if (cancelled) return;
        setData(d);
        // Default to first ticker with non-unknown regime, else first.
        const tickers = d?.universe || [];
        const firstReal = tickers.find((t) => d.regimes?.[t]?.regime && d.regimes[t].regime !== 'unknown');
        setSelected(firstReal || tickers[0] || null);
      })
      .catch((e) => { if (!cancelled) setErr(String(e)); });
    return () => { cancelled = true; };
  }, []);

  // Summarize regime counts for the header strip.
  const summary = useMemo(() => {
    if (!data?.regimes) return null;
    const counts = {};
    for (const r of Object.values(data.regimes)) {
      counts[r.regime] = (counts[r.regime] || 0) + 1;
    }
    return counts;
  }, [data]);

  if (err) return <div className="empty">IV regime endpoint error: {err}</div>;
  if (!data) return <div className="empty">Loading IV regime universe…</div>;

  const tickers = data.universe || [];
  if (!tickers.length) return <div className="empty">No tickers in scan universe.</div>;

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        {summary && Object.entries(summary).map(([regime, count]) => {
          const meta = REGIME_META[regime] || REGIME_META.unknown;
          return (
            <div key={regime} style={{
              padding: '6px 12px',
              background: meta.color + '22',
              border: `1px solid ${meta.color}55`,
              borderRadius: 16, fontSize: 12,
            }}>
              <span style={{ color: meta.color, fontWeight: 600 }}>{meta.label}</span>
              <span style={{ marginLeft: 6, color: 'var(--muted)' }}>{count}</span>
            </div>
          );
        })}
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))',
        gap: 8, marginBottom: 18,
      }}>
        {tickers.map((t) => (
          <RegimeTile
            key={t} ticker={t} report={data.regimes?.[t]}
            selected={selected === t}
            onClick={() => setSelected(t)}
          />
        ))}
      </div>

      <DetailPanel ticker={selected} report={selected && data.regimes?.[selected]} />

      <div style={{ marginTop: 12, fontSize: 10, color: 'var(--muted-2)' }}>
        Classifier: lag-1 autocorrelation + OLS slope over rolling IV history. Cached 1h.
      </div>
    </div>
  );
}
