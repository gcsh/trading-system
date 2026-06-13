/**
 * LongGammaStrip — Item #14 regime header.
 *
 * Reads Tier A + Tier B GEX fields from /heatseeker/{ticker}:
 *   - dealer_regime + dealer_flow + dealer_flow_intensity
 *   - distance_to_flip
 *   - vol_trigger
 *   - zero_dte_share + zero_dte_net_gex
 *   - max_gamma_strike + max_gamma_value
 *   - pin_risk_strike + pin_risk_distance + pin_risk_dte_weighted
 *   - total_vanna + total_charm
 *
 * Sits above the three heatmap panels. A trader scans this in 2s and
 * only drops into the per-strike heatmaps when something's interesting.
 */
import React, { useEffect, useState } from 'react';
import { money } from '../lib/format.js';

function StripCard({ label, value, sub, color, title }) {
  return (
    <div title={title} style={{
      flex: '1 1 0',
      minWidth: 110,
      padding: '8px 10px',
      borderRadius: 8,
      background: 'var(--panel-2)',
      border: '1px solid var(--border)',
    }}>
      <div style={{
        fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em',
        color: 'var(--muted)', fontWeight: 600,
      }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color, marginTop: 2 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 11, color: 'var(--muted-2)', marginTop: 1 }}>{sub}</div>}
    </div>
  );
}

function fmt(n, digits = 1) {
  if (n == null || isNaN(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(digits)}K`;
  return n.toFixed(digits);
}

function RegimeRibbon({ ticker }) {
  const [points, setPoints] = useState([]);
  useEffect(() => {
    let cancelled = false;
    fetch(`/heatseeker/${encodeURIComponent(ticker)}/history?limit=60`)
      .then((r) => r.ok ? r.json() : { items: [] })
      .then((d) => { if (!cancelled) setPoints(d.items || d.history || []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [ticker]);
  if (!points.length) return <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>warming up…</span>;
  // Each point: {timestamp, dealer_regime, net_gex_total}. Map to 0/1/-1.
  const ticks = points.map((p) => {
    if (p.dealer_regime === 'long_gamma') return 1;
    if (p.dealer_regime === 'short_gamma') return -1;
    return 0;
  }).slice(-60);
  const w = 100;
  const h = 14;
  const step = ticks.length > 1 ? w / (ticks.length - 1) : 0;
  const segs = ticks.map((t, i) => ({
    x: i * step,
    y: t === 1 ? 2 : t === -1 ? h - 4 : h / 2,
    color: t === 1 ? 'var(--accent)' : t === -1 ? 'var(--danger)' : 'var(--muted)',
  }));
  const path = segs.reduce((acc, p, i) => acc + `${i === 0 ? 'M' : 'L'}${p.x},${p.y} `, '');
  return (
    <svg width={w} height={h} style={{ verticalAlign: 'middle' }}>
      <path d={path} fill="none" stroke="var(--text-2)" strokeWidth="1" opacity="0.5" />
      {segs.map((p, i) => <circle key={i} cx={p.x} cy={p.y} r={1.5} fill={p.color} />)}
    </svg>
  );
}

export default function LongGammaStrip({ data, ticker }) {
  if (!data) return null;
  const regime = data.dealer_regime;
  const flow = data.dealer_flow || 'neutral';
  const intensity = data.dealer_flow_intensity || 0;
  const distance = data.distance_to_flip;
  const volTrigger = data.vol_trigger;
  const zeroShare = data.zero_dte_share;
  const zeroNet = data.zero_dte_net_gex;
  const maxGammaStrike = data.max_gamma_strike;
  const maxGammaValue = data.max_gamma_value;
  const pinStrike = data.pin_risk_strike;
  const pinDistance = data.pin_risk_distance;
  const pinDte = data.pin_risk_dte_weighted;
  const vanna = data.total_vanna;
  const charm = data.total_charm;

  const regimeColor = regime === 'long_gamma' ? 'var(--accent)'
                     : regime === 'short_gamma' ? 'var(--danger)'
                     : 'var(--muted)';
  const regimeLabel = regime === 'long_gamma' ? 'LONG GAMMA'
                     : regime === 'short_gamma' ? 'SHORT GAMMA'
                     : '—';
  const flowLabel = flow === 'stabilizing' ? 'dealers buy dips · sell rips'
                   : flow === 'amplifying' ? 'dealers amplify moves'
                   : 'neutral';

  return (
    <div className="panel" style={{ marginBottom: 12, padding: 12 }}>
      <div className="row" style={{
        gap: 8, marginBottom: 8, fontSize: 10,
        textTransform: 'uppercase', letterSpacing: '0.08em',
        color: 'var(--muted)', fontWeight: 600,
      }}>
        <span>Long Gamma Regime · {ticker}</span>
        <span style={{ marginLeft: 'auto' }}>
          30d <RegimeRibbon ticker={ticker} />
        </span>
      </div>
      <div className="row" style={{ gap: 8, alignItems: 'stretch', flexWrap: 'wrap' }}>
        <StripCard
          label="Regime"
          value={regimeLabel}
          color={regimeColor}
          sub={flowLabel}
          title="Long gamma → dealers hedge into moves to dampen them (mean-revert).
Short gamma → dealers hedge with the move to amplify (trend / vol expansion)."
        />
        <StripCard
          label="Distance to Flip"
          value={distance != null ? `${distance > 0 ? '+' : ''}${distance.toFixed(2)}` : '—'}
          color={distance == null ? 'var(--muted)' : Math.abs(distance) < 1 ? 'var(--warn)' : 'var(--text)'}
          sub={volTrigger != null ? `flip @ ${money(volTrigger)}` : null}
          title="Points from spot to the gamma flip strike. Small absolute number = regime change one move away."
        />
        <StripCard
          label="0DTE Share"
          value={zeroShare != null ? `${(zeroShare * 100).toFixed(1)}%` : '—'}
          color={zeroShare != null && zeroShare > 0.30 ? 'var(--warn)' : 'var(--text)'}
          sub={zeroNet != null ? `net ${fmt(zeroNet)}` : 'no 0DTE'}
          title="Fraction of total |GEX| concentrated in today's expiry. High share = fragile intraday."
        />
        <StripCard
          label="Peak Gamma"
          value={maxGammaStrike != null ? money(maxGammaStrike) : '—'}
          color="var(--accent)"
          sub={maxGammaValue ? `|${fmt(maxGammaValue)}|` : null}
          title="Strike with the highest absolute net GEX. Often a magnet (long gamma) or breakout level (short gamma)."
        />
        <StripCard
          label="Pin Risk"
          value={pinStrike != null ? money(pinStrike) : '—'}
          color={pinDte != null && pinDte > 0.7 ? 'var(--warn)' : 'var(--text)'}
          sub={pinDistance != null ? `${pinDistance > 0 ? '+' : ''}${pinDistance.toFixed(2)} · dte-wt ${pinDte != null ? pinDte.toFixed(2) : '—'}` : null}
          title="Highest-OI near-money strike, distance from spot, weighted by 1/DTE. Higher value = stronger pin pressure into close."
        />
        <StripCard
          label="Vanna (net)"
          value={vanna != null ? fmt(vanna) : '—'}
          color="var(--info)"
          sub="∂Γ/∂σ — IV-driven hedge flow"
          title="Sum of (calls − puts) × vanna × OI. Positive → IV up means dealers buy underlying; negative → opposite."
        />
        <StripCard
          label="Charm (net)"
          value={charm != null ? fmt(charm) : '—'}
          color="var(--info)"
          sub="∂Δ/∂t — decay hedge flow"
          title="Sum of (calls − puts) × charm × OI. Drives end-of-day pinning as deltas decay."
        />
      </div>
    </div>
  );
}
