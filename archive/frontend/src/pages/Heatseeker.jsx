import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import TickerSearch from '../components/TickerSearch.jsx';
import LongGammaStrip from '../components/LongGammaStrip.jsx';
import GexHeatmapHero from '../components/GexHeatmapHero.jsx';
import GexDrillIns from '../components/GexDrillIns.jsx';
import { money } from '../lib/format.js';

/**
 * Phase 19 — Heatseeker restructure.
 *
 * The page body now reads top-to-bottom as:
 *   1. Sticky controls bar (TickerSearch + quick chips + DTE + refresh)
 *   2. KPI strip (4 cards: Spot, Net GEX, Walls+Flip, Expected Move)
 *   3. Hero heatmap (full-width GexHeatmapHero — the page's primary visual)
 *   4. Tabbed drill-ins (Per Strike / Cumulative / By Expiry / Flow)
 *   5. Regime footer (LongGammaStrip, thin)
 *
 * All data fetching is untouched — the page still talks to `/heatseeker/{t}`
 * for the legacy summary + per-strike table, and the heatmap reuses the
 * canonical `useHeatseekerMulti` SWR hook for the multi-expiration matrix.
 */

const QUICK = ['SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'AAPL'];

// Compact GEX magnitude (e.g. 2.8B, 615.8M, -14.3B).
function gx(v) {
  const n = Number(v) || 0;
  const a = Math.abs(n);
  const s = n < 0 ? '-' : '';
  if (a >= 1e9) return `${s}${(a / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}K`;
  return `${s}${a.toFixed(0)}`;
}

const oi = (v) => (Number(v) || 0).toLocaleString();

// KPI card — bigger than the previous summary cards because they're now
// the only headline numbers above the hero heatmap. Mirrors the existing
// `panel` chrome so the design tokens carry through.
function KpiCard({ label, primary, primaryColor, lines = [] }) {
  return (
    <div
      className="panel"
      style={{
        padding: '12px 14px', flex: 1, minWidth: 200, marginBottom: 0,
        minHeight: 120, display: 'flex', flexDirection: 'column', gap: 4,
      }}
    >
      <div style={{
        fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase',
        letterSpacing: '0.06em', fontWeight: 600,
      }}>
        {label}
      </div>
      <div style={{
        fontSize: 22, fontWeight: 700, color: primaryColor || 'var(--text)',
        fontFeatureSettings: '"tnum"', lineHeight: 1.1,
      }}>
        {primary}
      </div>
      <div style={{ display: 'grid', gap: 2, marginTop: 'auto' }}>
        {lines.map((l, i) => (
          <div
            key={i}
            style={{
              fontSize: 11, color: l.color || 'var(--muted)',
              fontFeatureSettings: '"tnum"', display: 'flex',
              justifyContent: 'space-between', gap: 8,
            }}
          >
            <span style={{ color: 'var(--muted)' }}>{l.label}</span>
            <span style={{ color: l.color || 'var(--text-soft)', fontWeight: 600 }}>
              {l.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// One-sided horizontal-bar + numeric table — the active tab decides which bar
// (green call gamma / red put gamma) is drawn, so the two sides never overlap.
// Carried over from the previous Heatseeker version verbatim — this is the
// "Per Strike" drill-in that the new <GexDrillIns/> tab renders.
function GexTable({ rows, tab, setTab, spotStrike, callWall, putWall, flip }) {
  const barColor = tab === 'call' ? 'var(--accent)' : 'var(--danger)';
  const maxAbs = Math.max(1, ...rows.map((r) => Math.abs(tab === 'call' ? r.call_gex : r.put_gex)));
  const Th = ({ children, left }) => (
    <th style={{ padding: '6px 10px', textAlign: left ? 'left' : 'right', fontWeight: 600,
      fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em',
      position: 'sticky', top: 0, background: 'var(--panel)', borderBottom: '1px solid var(--border)' }}>{children}</th>
  );
  const Td = ({ children, color }) => (
    <td style={{ padding: '5px 10px', textAlign: 'right', color: color || 'var(--text-soft)', whiteSpace: 'nowrap' }}>{children}</td>
  );
  return (
    <div>
      <div className="row" style={{ gap: 6, marginBottom: 8 }}>
        <button className={`btn small ${tab === 'call' ? 'primary' : ''}`} onClick={() => setTab('call')}>Call GEX</button>
        <button className={`btn small ${tab === 'put' ? 'primary' : ''}`} onClick={() => setTab('put')}>Put GEX</button>
      </div>
      <div style={{ maxHeight: 320, overflow: 'auto', borderRadius: 8 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5, fontFeatureSettings: '"tnum"' }}>
          <thead>
            <tr>
              <Th left>Strike</Th>
              <Th left>Expiry</Th>
              <Th left>{tab === 'call' ? 'Call γ' : 'Put γ'}</Th>
              <Th>Net GEX</Th>
              <Th>Call GEX</Th>
              <Th>Put GEX</Th>
              <Th>Call OI</Th>
              <Th>Put OI</Th>
              <Th>Total OI</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const val = tab === 'call' ? r.call_gex : r.put_gex;
              const w = Math.min(100, (Math.abs(val) / maxAbs) * 100);
              const tags = [];
              if (r.strike === spotStrike) tags.push(['Spot', 'var(--info)']);
              if (callWall != null && r.strike === callWall) tags.push(['Call Wall', 'var(--accent)']);
              if (putWall != null && r.strike === putWall) tags.push(['Put Wall', 'var(--danger)']);
              if (flip != null && r.strike === flip) tags.push(['Flip', 'var(--warn)']);
              const hi = tags.length > 0;
              return (
                <tr key={`${r.strike}-${r.expiry}`} style={{ background: hi ? 'var(--panel-2)' : 'transparent', borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '5px 10px', whiteSpace: 'nowrap' }}>
                    <span style={{ fontWeight: hi ? 700 : 600 }}>{r.strike}</span>
                    {tags.map(([t, c]) => (
                      <span key={t} style={{ marginLeft: 5, fontSize: 9.5, fontWeight: 700, color: c }}>{t}</span>
                    ))}
                  </td>
                  <td style={{ padding: '5px 10px', whiteSpace: 'nowrap', color: 'var(--text-soft)' }}>
                    {r.expiry || '—'}
                    {r.dte != null && (
                      <span style={{ marginLeft: 6, fontSize: 10, color: r.has_zero_dte ? 'var(--danger)' : 'var(--muted)' }}>
                        {r.dte}d
                      </span>
                    )}
                  </td>
                  <td style={{ padding: '5px 10px', width: '30%' }}>
                    <div style={{ height: 12, width: `${w}%`, minWidth: val ? 2 : 0, background: barColor, borderRadius: 2, opacity: hi ? 1 : 0.7 }} />
                  </td>
                  <Td color={r.net_gex >= 0 ? 'var(--accent)' : 'var(--danger)'}>{gx(r.net_gex)}</Td>
                  <Td color="var(--accent)">{gx(r.call_gex)}</Td>
                  <Td color="var(--danger)">{gx(r.put_gex)}</Td>
                  <Td>{oi(r.call_oi)}</Td>
                  <Td>{oi(r.put_oi)}</Td>
                  <Td>{oi(r.total_oi)}</Td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Heatseeker() {
  // The ?symbol= query param is the source of truth for the active ticker, so
  // the view is deep-linkable, shareable and reload-safe.
  const [params, setParams] = useSearchParams();
  const ticker = (params.get('symbol') || 'SPY').toUpperCase();
  const setTicker = useCallback((t) => {
    const next = new URLSearchParams(params);
    next.set('symbol', (t || 'SPY').toUpperCase());
    setParams(next);
  }, [params, setParams]);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState('call');   // call | put — drives Per-Strike sub-toggle

  // MITS Phase 9.3 — DTE bucket dropdown (default 'all' preserves
  // legacy front-45-day aggregation).
  const expirationFromUrl = params.get('expiration') || 'all';
  const [expiration, setExpirationState] = useState(expirationFromUrl);
  const setExpiration = useCallback((value) => {
    setExpirationState(value);
    const next = new URLSearchParams(params);
    if (value === 'all') next.delete('expiration');
    else next.set('expiration', value);
    setParams(next);
  }, [params, setParams]);
  const EXPIRATION_BUCKETS = ['0d', '1d', '5d', '7d', '14d', '30d', '60d', 'all'];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = new URLSearchParams();
      if (expiration && expiration !== 'all') q.set('expiration', expiration);
      const url = `/heatseeker/${encodeURIComponent(ticker)}` +
                  (q.toString() ? `?${q.toString()}` : '');
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setData(d);
      setErr(d.ok ? null : (d.note || 'no data'));
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, [ticker, expiration]);

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);

  const spot = data?.spot_price || 0;
  const spotChangePct = data?.spot_change_pct ?? data?.change_pct ?? null;
  const rows = useMemo(() => {
    if (!data?.gex_by_strike?.length) return [];
    return [...data.gex_by_strike]
      .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot))
      .slice(0, 30)
      .sort((a, b) => b.strike - a.strike);   // high strike on top, like the reference
  }, [data, spot]);

  const spotStrike = useMemo(() => {
    if (!rows.length) return null;
    return rows.reduce((best, r) => (Math.abs(r.strike - spot) < Math.abs(best - spot) ? r.strike : best), rows[0].strike);
  }, [rows, spot]);

  const regime = data?.dealer_regime;
  const regimeOn = regime === 'long_gamma';
  const em = data?.expected_move;
  const emPct = data?.expected_move_pct;
  // No native 5d expected move in the API today — approximate via √5
  // (standard IV-scaling assumption) when 1d is available. Sub-line is
  // tagged as approximate so the operator doesn't read it as a precise
  // backend number.
  const em5d = em != null ? em * Math.sqrt(5) : null;
  const em5dPct = emPct != null ? emPct * Math.sqrt(5) : null;

  // Wall + flip strings — reused in the KPI card and the GexTable tags.
  const callWall = data?.call_wall ?? null;
  const putWall  = data?.put_wall ?? null;
  const flip     = data?.gamma_flip ?? null;

  // Per-strike node handed to <GexDrillIns> so the existing table JSX
  // (with the call/put sub-toggle) lives inside the Per Strike tab
  // without us re-implementing it inside the new component.
  const perStrikeNode = rows.length > 0 ? (
    <GexTable
      rows={rows}
      tab={tab}
      setTab={setTab}
      spotStrike={spotStrike}
      callWall={callWall}
      putWall={putWall}
      flip={flip}
    />
  ) : (
    <div className="empty" style={{ padding: 20 }}>
      {loading ? 'Loading GEX…' : 'No per-strike GEX data for this symbol.'}
    </div>
  );

  return (
    <div>
      <div className="panel-head" style={{ marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>Heatseeker — Gamma Exposure (GEX)</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            Where dealer hedging pins or accelerates price. Green = call gamma (resistance), red = put gamma (support). Auto-refreshes every 60s.
          </div>
        </div>
      </div>

      {/* ROW 1 — sticky controls bar. Picker, quick chips, DTE, refresh. */}
      <div
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 5,
          background: 'var(--bg)',
          padding: '8px 0',
          marginBottom: 12,
          borderBottom: '1px solid var(--border)',
        }}
      >
        <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ width: 240 }}>
            <TickerSearch onAdd={(s) => setTicker(s.toUpperCase())} placeholder={`${ticker} — search`} />
          </div>
          {QUICK.map((s) => (
            <button
              key={s}
              className={`btn small ${ticker === s ? 'primary' : ''}`}
              onClick={() => setTicker(s)}
            >
              {s}
            </button>
          ))}
          <label style={{
            display: 'flex', alignItems: 'center', gap: 6,
            fontSize: 11, color: 'var(--muted)', marginLeft: 4,
          }}>
            DTE
            <select
              value={expiration}
              onChange={(e) => setExpiration(e.target.value)}
              title="Filter chain by days-to-expiry"
            >
              {EXPIRATION_BUCKETS.map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </label>
          <button
            className="btn small"
            onClick={load}
            disabled={loading}
            title="Force-refresh /heatseeker"
          >
            {loading ? 'refreshing…' : 'Refresh'}
          </button>
          {err && (
            <span style={{ fontSize: 11, color: 'var(--warn)', marginLeft: 4 }}>
              {err}
            </span>
          )}
        </div>
      </div>

      {/* ROW 2 — KPI strip (4 cards). */}
      <div className="row" style={{ gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
        <KpiCard
          label="Spot"
          primary={money(spot)}
          primaryColor="var(--info)"
          lines={[
            spotChangePct != null
              ? {
                  label: 'Δ today',
                  value: `${spotChangePct >= 0 ? '+' : ''}${Number(spotChangePct).toFixed(2)}%`,
                  color: spotChangePct >= 0 ? 'var(--accent)' : 'var(--danger)',
                }
              : { label: 'Δ today', value: data?.stale ? 'stale' : '—', color: 'var(--muted)' },
            {
              label: 'Regime',
              value: regime === 'long_gamma' ? 'LONG GAMMA'
                   : regime === 'short_gamma' ? 'SHORT GAMMA'
                   : '—',
              color: regimeOn ? 'var(--accent)' : (regime === 'short_gamma' ? 'var(--danger)' : 'var(--muted)'),
            },
          ]}
        />
        <KpiCard
          label="Net Gamma (GEX)"
          primary={gx(data?.net_gex_total)}
          primaryColor={(data?.net_gex_total ?? 0) >= 0 ? 'var(--accent)' : 'var(--danger)'}
          lines={[
            { label: 'Call γ', value: gx(data?.call_gex_total), color: 'var(--accent)' },
            { label: 'Put γ',  value: gx(data?.put_gex_total),  color: 'var(--danger)' },
          ]}
        />
        <KpiCard
          label="Walls + Flip"
          primary={callWall != null ? money(callWall) : '—'}
          primaryColor="var(--accent)"
          lines={[
            { label: 'Call wall', value: callWall != null ? money(callWall) : '—', color: 'var(--accent)' },
            { label: 'Put wall',  value: putWall  != null ? money(putWall)  : '—', color: 'var(--danger)' },
            { label: 'Gamma flip', value: flip != null ? money(flip) : '—', color: 'var(--warn)' },
          ]}
        />
        <KpiCard
          label="Expected Move"
          primary={em != null ? `± ${money(em)}` : '—'}
          primaryColor="var(--info)"
          lines={[
            {
              label: '1d',
              value: emPct != null ? `±${Number(emPct).toFixed(2)}%` : 'no IV',
              color: 'var(--text-soft)',
            },
            {
              label: '5d (~√5)',
              value: em5dPct != null ? `±${Number(em5dPct).toFixed(2)}%` : '—',
              color: 'var(--text-soft)',
            },
            ...(data?.opex_day
              ? [{ label: 'OPEX day', value: 'YES', color: 'var(--warn)' }]
              : []),
          ]}
        />
      </div>

      {/* ROW 3 — Hero heatmap. */}
      <GexHeatmapHero ticker={ticker} dte={expiration} height={520} />

      {/* ROW 4 — Tabbed drill-ins. */}
      <GexDrillIns
        ticker={ticker}
        dte={expiration}
        rows={rows}
        spotStrike={spotStrike}
        callWall={callWall}
        putWall={putWall}
        flip={flip}
        perStrikeNode={perStrikeNode}
      />

      {/* ROW 5 — Regime footer (thin). */}
      <div style={{ marginTop: 14 }}>
        <LongGammaStrip data={data} ticker={ticker} />
      </div>
    </div>
  );
}
