/* MITS Phase 19 Stream 2 — GEX Dashboard v2 (Heatseeker).
 *
 * Bloomberg/Heatseeker-style page mounted at /v2/gex/:ticker?. Replicates
 * the operator's reference image using REAL data from:
 *   - /heatseeker/{ticker}   → spot price, walls, gamma flip, dealer
 *                              regime, gex_by_strike[], call/put/net
 *                              totals, ATM IV, expected move, vanna,
 *                              charm, pin risk, 0DTE breakouts.
 *   - /quote/{ticker}        → live spot tick (1s in-market via useLivePrice).
 *   - /watchlist/items       → tickers in the dropdown.
 *
 * Honesty rules:
 *   - No fabricated data. Every panel sources from the response.
 *   - Missing fields render <EmptyState/> with a precise reason.
 *   - The expiration × strike heatmap honors whatever the backend
 *     actually returned for `expiry` — most of the time that's a
 *     single near-term expiry; we surface that fact instead of inventing rows.
 *   - The "GEX Trend (Total)" sparkline only shows when we accumulate
 *     ≥ 3 polled snapshots. Otherwise EmptyState.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Card, Stat, Pill, Sparkline, MiniHeatmap, Section, Table, EmptyState,
  AlertBanner,
} from '../../design/Components.jsx';
import useGex from '../hooks/useGex.js';
import useLivePrice from '../hooks/useLivePrice.js';
import GexByStrikeChart from '../components/GexByStrikeChart.jsx';
import GexProfileChart from '../components/GexProfileChart.jsx';
import GexExpiryBars from '../components/GexExpiryBars.jsx';

/* ── formatters ────────────────────────────────────────────────────── */
function fmtMoney(v, places = 2) {
  if (v == null || !isFinite(v)) return '—';
  return `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: places, maximumFractionDigits: places,
  })}`;
}
function fmtBig(n) {
  if (n == null || !isFinite(n)) return '—';
  const x = Math.abs(Number(n));
  const sign = n < 0 ? '-' : '';
  if (x >= 1e9) return `${sign}${(x / 1e9).toFixed(2)}B`;
  if (x >= 1e6) return `${sign}${(x / 1e6).toFixed(1)}M`;
  if (x >= 1e3) return `${sign}${(x / 1e3).toFixed(1)}K`;
  return `${sign}${x.toFixed(0)}`;
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}%`;
}

function nyMarketState(d = new Date()) {
  const day = d.getUTCDay();
  if (day === 0 || day === 6) return 'closed';
  const etHour = (d.getUTCHours() - 4 + 24) % 24;
  const etMin = d.getUTCMinutes();
  const minutes = etHour * 60 + etMin;
  if (minutes >= 9 * 60 + 30 && minutes < 16 * 60) return 'open';
  if (minutes >= 4 * 60 && minutes < 9 * 60 + 30) return 'premarket';
  if (minutes >= 16 * 60 && minutes < 20 * 60) return 'afterhours';
  return 'closed';
}

function nyDateString(d = new Date()) {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
    }).format(d);
  } catch (_) {
    return d.toDateString();
  }
}

/* ── derived calculators ───────────────────────────────────────────── */
function pctOTM(strikes, spot, side /* 'call' | 'put' */) {
  if (!Array.isArray(strikes) || !spot) return null;
  let totalAbs = 0, otmAbs = 0;
  for (const r of strikes) {
    const g = side === 'call' ? Number(r.call_gex) : Number(r.put_gex);
    const v = Math.abs(g);
    if (!isFinite(v) || v === 0) continue;
    totalAbs += v;
    const otm = side === 'call' ? (r.strike > spot) : (r.strike < spot);
    if (otm) otmAbs += v;
  }
  return totalAbs > 0 ? (otmAbs / totalAbs) * 100 : null;
}

/* ── page ──────────────────────────────────────────────────────────── */
export default function GexDashboard() {
  const { ticker: rawTicker } = useParams();
  const navigate = useNavigate();
  const ticker = (rawTicker || 'SPY').toUpperCase();

  const { data: gex, error: gexErr, loading } = useGex(ticker, { refreshMs: 30_000 });
  const { tick } = useLivePrice(ticker);

  const [watchlist, setWatchlist] = useState([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch('/watchlist/items');
        if (!r.ok) return;
        const j = await r.json();
        if (cancelled) return;
        const list = Array.isArray(j) ? j : (j?.items || []);
        const tickers = list.map((i) => (i.ticker || '').toUpperCase()).filter(Boolean);
        setWatchlist(tickers);
      } catch (_) {}
    })();
    return () => { cancelled = true; };
  }, []);

  /* ── GEX trend history — captured from each polled snapshot ──── */
  const [history, setHistory] = useState([]);
  const lastTimestampRef = useRef(null);
  useEffect(() => {
    if (!gex?.timestamp || !isFinite(gex?.net_gex_total)) return;
    if (lastTimestampRef.current === gex.timestamp) return;
    lastTimestampRef.current = gex.timestamp;
    setHistory((h) => {
      const next = [...h, {
        t: gex.timestamp,
        net: Number(gex.net_gex_total),
        call: Number(gex.call_gex_total) || 0,
        put:  Number(gex.put_gex_total)  || 0,
      }];
      return next.slice(-60); // keep last 60 ticks (~30min @ 30s)
    });
  }, [gex?.timestamp, gex?.net_gex_total, gex?.call_gex_total, gex?.put_gex_total]);

  /* ── derived signals ──────────────────────────────────────────── */
  const livePrice = tick?.price ?? gex?.spot_price ?? null;
  const prevSpot = useMemo(() => {
    // Prefer prev_close from /quote if exposed, otherwise derive a Δ
    // from history (delta % vs first snapshot in this session).
    if (history.length > 1 && livePrice != null) {
      const first = history[0];
      // We don't have a real "previous close" — historical sparkline only.
      return null;
    }
    return null;
  }, [history, livePrice]);

  const totalGex = gex?.net_gex_total;
  const totalGexBias = totalGex == null ? null : (totalGex >= 0 ? 'Bullish' : 'Bearish');
  const callGex = gex?.call_gex_total;
  const putGex = gex?.put_gex_total;
  const netGex = totalGex;

  const otmCallPct = useMemo(() => pctOTM(gex?.gex_by_strike, livePrice, 'call'),
    [gex?.gex_by_strike, livePrice]);
  const otmPutPct  = useMemo(() => pctOTM(gex?.gex_by_strike, livePrice, 'put'),
    [gex?.gex_by_strike, livePrice]);

  const trendDelta = useMemo(() => {
    if (history.length < 2) return null;
    const prev = history[0].net;
    const curr = history[history.length - 1].net;
    if (!isFinite(prev) || !isFinite(curr)) return null;
    return curr - prev;
  }, [history]);

  const sparklineData = useMemo(() => history.map((h) => h.net), [history]);

  /* ── Key levels table ─────────────────────────────────────────── */
  const keyLevels = useMemo(() => {
    if (!gex) return [];
    const rows = [];
    if (gex.max_gamma_strike != null) {
      rows.push({
        type: 'γ MAX',
        price: fmtMoney(gex.max_gamma_strike),
        impact: fmtBig(gex.max_gamma_value),
        tone: 'info',
      });
    }
    if (gex.call_wall != null) {
      rows.push({
        type: 'CALL WALL',
        price: fmtMoney(gex.call_wall),
        impact: 'Resistance',
        tone: 'success',
      });
    }
    if (livePrice != null) {
      rows.push({
        type: 'SPOT',
        price: fmtMoney(livePrice),
        impact: tick?.source || gex?.source || '—',
        tone: 'warning',
      });
    }
    if (gex.gamma_flip != null) {
      rows.push({
        type: 'γ FLIP',
        price: fmtMoney(gex.gamma_flip),
        impact: gex.dealer_regime || '—',
        tone: 'neutral',
      });
    }
    if (gex.put_wall != null) {
      rows.push({
        type: 'PUT WALL',
        price: fmtMoney(gex.put_wall),
        impact: 'Support',
        tone: 'error',
      });
    }
    if (gex.pin_risk_strike != null) {
      rows.push({
        type: 'PIN RISK',
        price: fmtMoney(gex.pin_risk_strike),
        impact: `${(Number(gex.pin_risk_dte_weighted) || 0).toFixed(2)} dte-wt`,
        tone: 'neutral',
      });
    }
    return rows;
  }, [gex, livePrice, tick]);

  /* ── Heatmap (expirations × strikes). Backend usually returns a single
       expiry per call so the matrix collapses to one row + an EmptyState. */
  const heatmap = useMemo(() => {
    if (!Array.isArray(gex?.gex_by_strike)) return null;
    const exps = Array.from(new Set(gex.gex_by_strike.map((r) => r.expiry).filter(Boolean)));
    if (!exps.length) return null;
    exps.sort();
    // Pick the 6 strikes closest to spot (or evenly spread when no spot).
    let strikes = Array.from(new Set(gex.gex_by_strike.map((r) => r.strike))).sort((a, b) => a - b);
    if (livePrice != null) {
      strikes = strikes.sort((a, b) => Math.abs(a - livePrice) - Math.abs(b - livePrice)).slice(0, 6).sort((a, b) => a - b);
    } else {
      strikes = strikes.slice(0, 6);
    }
    const matrix = exps.map((exp) =>
      strikes.map((s) => {
        const row = gex.gex_by_strike.find((r) => r.strike === s && r.expiry === exp);
        return row ? Number(row.net_gex) || 0 : 0;
      }),
    );
    const rowLabels = exps.map((e) => {
      const sample = gex.gex_by_strike.find((r) => r.expiry === e);
      const dte = sample?.dte;
      if (dte === 0) return '0DTE';
      if (dte != null && isFinite(dte)) return `${dte}d`;
      return e;
    });
    const colLabels = strikes.map((s) => `${s.toFixed(0)}`);
    return { matrix, rowLabels, colLabels, expCount: exps.length };
  }, [gex?.gex_by_strike, livePrice]);

  /* ── strike rows count for graceful states ───────────────────── */
  const strikeCount = Array.isArray(gex?.gex_by_strike) ? gex.gex_by_strike.length : 0;
  const enoughStrikes = strikeCount >= 10;

  const marketState = nyMarketState();
  const stateTone = marketState === 'open' ? 'success'
                  : marketState === 'closed' ? 'error'
                  : 'warning';

  /* ── render ──────────────────────────────────────────────────── */
  return (
    <div className="v2-gex">
      {/* HEADER STRIP */}
      <div className="v2-gex-header">
        <div className="v2-gex-header__left">
          <div className="v2-gex-header__title">GEX DASHBOARD</div>
          <div className="v2-gex-header__subtitle">Gamma Exposure Analysis</div>
          <div className="v2-gex-header__tickerwrap">
            <label className="v2-gex-header__pickerlabel">Ticker</label>
            <select className="v2-gex-header__picker"
                    value={ticker}
                    onChange={(e) => navigate(`/v2/gex/${e.target.value.toUpperCase()}`)}>
              {(watchlist.length ? watchlist : [ticker]).includes(ticker)
                ? null : <option value={ticker}>{ticker}</option>}
              {(watchlist.length ? watchlist : [ticker]).map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="v2-gex-header__center">
          <div className="v2-gex-header__price">
            <div className="v2-gex-header__pxlabel">SPOT</div>
            <div className="v2-gex-header__px mono">
              {fmtMoney(livePrice)}
            </div>
            <div className={`v2-gex-header__src mono ${tick?.source?.includes('alpaca') ? 'src-good' : tick?.source?.includes('stale') ? 'src-stale' : ''}`}>
              {tick?.source || gex?.source || '—'}
              {tick?.age_seconds != null && ` · ${tick.age_seconds.toFixed(0)}s`}
            </div>
          </div>
        </div>

        <div className="v2-gex-header__right">
          <div className="v2-gex-header__kpi">
            <div className="v2-gex-header__kpi-label">TOTAL GEX</div>
            <div className={`v2-gex-header__kpi-val mono ${totalGex >= 0 ? 'pos' : 'neg'}`}>
              {fmtBig(totalGex)}
            </div>
            <Pill tone={totalGex >= 0 ? 'success' : 'error'}>
              {totalGexBias || '—'}
            </Pill>
          </div>
          <div className="v2-gex-header__kpi">
            <div className="v2-gex-header__kpi-label">NET GEX</div>
            <div className={`v2-gex-header__kpi-val mono ${netGex >= 0 ? 'pos' : 'neg'}`}>
              {fmtBig(netGex)}
            </div>
            <Pill tone={netGex >= 0 ? 'success' : 'error'}>
              {netGex >= 0 ? 'Long γ' : 'Short γ'}
            </Pill>
          </div>
          <div className="v2-gex-header__kpi">
            <div className="v2-gex-header__kpi-label">MARKET</div>
            <Pill tone={stateTone}>{marketState.toUpperCase()}</Pill>
            <div className="v2-gex-header__date mono">{nyDateString()}</div>
          </div>
        </div>
      </div>

      {gexErr && (
        <AlertBanner severity="warning">
          /heatseeker/{ticker} failed: {gexErr}. Showing whatever loaded earlier.
        </AlertBanner>
      )}
      {gex?.stale && (
        <AlertBanner severity="info">
          Snapshot flagged STALE by backend ({gex.note || 'no freshness note'}).
        </AlertBanner>
      )}

      {/* THREE-COLUMN BODY */}
      <div className="v2-gex-grid">
        {/* LEFT COLUMN */}
        <div className="v2-gex-col v2-gex-col--left">
          <Section title="GEX Summary" subtitle="snapshot">
            <Card>
              <div className="v2-gex-summary">
                <Stat label="Total GEX" value={fmtBig(totalGex)}
                      deltaPositive={totalGex >= 0}
                      delta={totalGexBias}
                      mono />
                <Stat label="Call GEX" value={fmtBig(callGex)}
                      deltaPositive={true} mono />
                <Stat label="Put GEX" value={fmtBig(putGex)}
                      deltaPositive={false} mono />
                <Stat label="Net GEX" value={fmtBig(netGex)}
                      deltaPositive={netGex >= 0}
                      hint={gex?.dealer_regime ? `dealer: ${gex.dealer_regime}` : null}
                      mono />
                <div className="v2-gex-summary__otm">
                  <div className="v2-gex-summary__otm-label">% OTM GEX</div>
                  <div className="v2-gex-summary__otm-row">
                    <span>Calls</span>
                    <span className="mono">{otmCallPct != null ? `${otmCallPct.toFixed(0)}%` : '—'}</span>
                  </div>
                  <div className="v2-gex-summary__otm-row">
                    <span>Puts</span>
                    <span className="mono">{otmPutPct != null ? `${otmPutPct.toFixed(0)}%` : '—'}</span>
                  </div>
                </div>
                <div className="v2-gex-summary__skew">
                  <div className="v2-gex-summary__otm-label">GEX Skew</div>
                  <Pill tone={
                    totalGex == null ? 'neutral'
                    : totalGex > 0 ? 'success' : 'error'
                  }>
                    {totalGex == null ? '—'
                     : totalGex > 0 ? 'Bullish (pinning ↑)'
                     : 'Bearish (volatile ↓)'}
                  </Pill>
                </div>
              </div>
            </Card>
          </Section>

          <Section title="GEX Trend (Total)" subtitle="this session — polled @ 30s">
            <Card>
              {sparklineData.length >= 3 ? (
                <div className="v2-gex-trend">
                  <Sparkline data={sparklineData}
                             color={trendDelta >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}
                             width={260} height={56} />
                  <div className="v2-gex-trend__meta">
                    <div className="mono">
                      {fmtBig(sparklineData[sparklineData.length - 1])}
                      <span className={trendDelta >= 0 ? 'pos' : 'neg'}
                            style={{ marginLeft: 6 }}>
                        {trendDelta != null
                          ? `(${trendDelta >= 0 ? '+' : ''}${fmtBig(trendDelta)} since open)`
                          : ''}
                      </span>
                    </div>
                    <div className="dim">
                      {sparklineData.length} snapshot{sparklineData.length === 1 ? '' : 's'} ·
                      peak {fmtBig(Math.max(...sparklineData))}
                    </div>
                  </div>
                </div>
              ) : (
                <EmptyState
                  icon="∿"
                  message={`Collecting snapshots… need 3+ polled refreshes (currently ${sparklineData.length}).`} />
              )}
            </Card>
          </Section>

          <Section title="Key Levels" subtitle="from /heatseeker">
            <Card>
              {keyLevels.length ? (
                <Table cols={[
                  { key: 'type',   label: 'Type',    mono: true },
                  { key: 'price',  label: 'Price',   mono: true, align: 'right' },
                  { key: 'impact', label: 'GEX impact', align: 'right' },
                ]}
                       rows={keyLevels.map((r, i) => ({
                         __key: r.type + i,
                         type: <Pill tone={r.tone}>{r.type}</Pill>,
                         price: r.price,
                         impact: <span className="mono dim">{r.impact}</span>,
                       }))}
                       striped />
              ) : (
                <EmptyState icon="∅" message="Heatseeker did not return walls/flip." />
              )}
            </Card>
          </Section>
        </div>

        {/* CENTER COLUMN — the centerpiece chart */}
        <div className="v2-gex-col v2-gex-col--center">
          <Section title="GEX by Strike"
                   subtitle={enoughStrikes
                     ? `${strikeCount} strikes · ±8% window around spot`
                     : `${strikeCount} strikes — limited data`}>
            <Card>
              {enoughStrikes ? (
                <GexByStrikeChart
                  strikes={gex.gex_by_strike}
                  spotPrice={livePrice}
                  callWall={gex?.call_wall}
                  putWall={gex?.put_wall}
                  gammaFlip={gex?.gamma_flip}
                  maxGammaStrike={gex?.max_gamma_strike}
                  height={560} />
              ) : (
                <EmptyState icon="∅"
                            message={`Limited strike data — ${strikeCount} strike${strikeCount === 1 ? '' : 's'} returned.`} />
              )}
            </Card>
          </Section>

          <Section title="GEX Profile (Aggregated)"
                   subtitle="net GEX by strike with bullish / bearish zones">
            <Card>
              {enoughStrikes ? (
                <GexProfileChart
                  strikes={gex.gex_by_strike}
                  spotPrice={livePrice}
                  callWall={gex?.call_wall}
                  putWall={gex?.put_wall}
                  gammaFlip={gex?.gamma_flip}
                  height={240} />
              ) : (
                <EmptyState icon="∅" message="Need ≥10 strikes for profile chart." />
              )}
            </Card>
          </Section>

          {/* Bonus signals strip — uses real backend scalars. */}
          <Section title="Dealer + Volatility Signals">
            <Card>
              <div className="v2-gex-kpigrid">
                <Stat label="ATM IV"
                      value={gex?.atm_iv != null ? `${(gex.atm_iv * 100).toFixed(1)}%` : '—'}
                      mono />
                <Stat label="Expected Move"
                      value={gex?.expected_move != null ? fmtMoney(gex.expected_move) : '—'}
                      delta={gex?.expected_move_pct != null ? `${(gex.expected_move_pct * 100).toFixed(1)}%` : null}
                      mono />
                <Stat label="Vol Trigger"
                      value={fmtMoney(gex?.vol_trigger)} mono />
                <Stat label="Dist to Flip"
                      value={gex?.distance_to_flip != null
                        ? `${(gex.distance_to_flip * 100).toFixed(2)}%`
                        : '—'}
                      mono />
                <Stat label="Vanna Σ"
                      value={fmtBig(gex?.total_vanna)} mono />
                <Stat label="Charm Σ"
                      value={fmtBig(gex?.total_charm)} mono />
                <Stat label="Dealer Flow"
                      value={gex?.dealer_flow || '—'}
                      delta={gex?.dealer_flow_intensity != null
                        ? `intensity ${(gex.dealer_flow_intensity).toFixed(2)}`
                        : null}
                      mono />
                <Stat label="0DTE Net γ"
                      value={gex?.zero_dte_net_gex != null
                        ? fmtBig(gex.zero_dte_net_gex)
                        : 'n/a'}
                      hint={gex?.zero_dte_share != null
                        ? `${(gex.zero_dte_share * 100).toFixed(1)}% of total`
                        : 'snapshot lacks 0DTE expiry'}
                      mono />
              </div>
            </Card>
          </Section>
        </div>

        {/* RIGHT COLUMN */}
        <div className="v2-gex-col v2-gex-col--right">
          <Section title="GEX Heatmap"
                   subtitle={heatmap
                     ? `${heatmap.expCount} expiration${heatmap.expCount === 1 ? '' : 's'} × ${heatmap.colLabels.length} strikes`
                     : 'awaiting data'}>
            <Card>
              {heatmap && heatmap.expCount >= 2 ? (
                <MiniHeatmap data={heatmap.matrix}
                             rowLabels={heatmap.rowLabels}
                             colLabels={heatmap.colLabels} />
              ) : (
                <EmptyState
                  icon="∅"
                  message={heatmap
                    ? `Multi-expiration GEX data not yet available — backend returned only ${heatmap.expCount} expiry (${heatmap.rowLabels[0] || 'n/a'}).`
                    : 'Heatseeker did not return per-strike rows yet.'} />
              )}
            </Card>
          </Section>

          <Section title="GEX Exposure by Expiry"
                   subtitle="bucketed by DTE">
            <Card>
              <GexExpiryBars
                strikes={Array.isArray(gex?.gex_by_strike) ? gex.gex_by_strike : []}
                height={220} />
            </Card>
          </Section>

          <Section title="How to Read">
            <Card>
              <ul className="v2-gex-legend">
                <li>
                  <span className="v2-gex-legend__sw" style={{ background: 'var(--accent-green)' }} />
                  <strong>Call GEX bars</strong> — dealer hedging supply above. Acts as resistance / pin.
                </li>
                <li>
                  <span className="v2-gex-legend__sw" style={{ background: 'var(--accent-red)' }} />
                  <strong>Put GEX bars</strong> — dealer hedging demand below. Acts as support / accelerant on breaks.
                </li>
                <li>
                  <span className="v2-gex-legend__sw" style={{ background: 'rgba(241,245,249,0.85)', height: 3 }} />
                  <strong>Net GEX line</strong> — call − put per strike. Positive = pin-friendly, negative = trendy.
                </li>
                <li>
                  <span className="v2-gex-legend__sw v2-gex-legend__sw--dashed" style={{ borderColor: 'var(--accent-yellow)' }} />
                  <strong>Spot (dotted)</strong> — current price tick from <code>/quote</code>.
                </li>
                <li>
                  <span className="v2-gex-legend__sw" style={{ background: 'var(--accent-purple)' }} />
                  <strong>γ Flip</strong> — where dealer regime inverts from short γ to long γ.
                </li>
              </ul>
            </Card>
          </Section>
        </div>
      </div>

      <div className="v2-gex-footer mono dim">
        Updated {gex?.timestamp || '—'} · expiration {gex?.expiration || '—'} ·
        source {gex?.source || '—'} · {loading ? 'refreshing…' : 'idle'}
      </div>

      <style>{`
        .v2-gex { display: flex; flex-direction: column; gap: var(--space-4); }

        .v2-gex-header {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto auto;
          gap: 24px;
          align-items: center;
          padding: 12px 16px;
          background: var(--bg-secondary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-md);
        }
        .v2-gex-header__title {
          font-size: 22px;
          font-weight: 800;
          letter-spacing: 0.08em;
          color: var(--accent-green);
        }
        .v2-gex-header__subtitle {
          font-size: 11px;
          color: var(--text-tertiary);
          letter-spacing: 0.05em;
          margin-top: 2px;
          margin-bottom: 8px;
        }
        .v2-gex-header__tickerwrap {
          display: flex; align-items: center; gap: 8px;
        }
        .v2-gex-header__pickerlabel {
          font-size: 11px;
          color: var(--text-tertiary);
          letter-spacing: 0.06em;
          text-transform: uppercase;
        }
        .v2-gex-header__picker {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-primary);
          padding: 4px 8px;
          border-radius: var(--radius-sm);
          font-family: var(--font-mono);
          font-size: 12px;
          min-width: 100px;
        }
        .v2-gex-header__center {
          display: flex; align-items: center; justify-content: center;
        }
        .v2-gex-header__price {
          text-align: center;
        }
        .v2-gex-header__pxlabel {
          font-size: 10px;
          color: var(--text-tertiary);
          letter-spacing: 0.1em;
        }
        .v2-gex-header__px {
          font-size: 32px;
          font-weight: 800;
          letter-spacing: 0.02em;
          color: var(--text-primary);
        }
        .v2-gex-header__src {
          font-size: 11px;
          color: var(--text-tertiary);
        }
        .v2-gex-header__src.src-good { color: var(--accent-green); }
        .v2-gex-header__src.src-stale { color: var(--accent-yellow); }

        .v2-gex-header__right {
          display: flex; gap: 18px;
        }
        .v2-gex-header__kpi {
          display: flex; flex-direction: column; align-items: flex-end;
          gap: 4px; min-width: 120px;
        }
        .v2-gex-header__kpi-label {
          font-size: 10px;
          letter-spacing: 0.08em;
          color: var(--text-tertiary);
        }
        .v2-gex-header__kpi-val {
          font-size: 22px; font-weight: 800;
        }
        .v2-gex-header__kpi-val.pos { color: var(--accent-green); }
        .v2-gex-header__kpi-val.neg { color: var(--accent-red); }
        .v2-gex-header__date {
          font-size: 10px; color: var(--text-tertiary);
        }

        .v2-gex-grid {
          display: grid;
          grid-template-columns: minmax(0, 280px) minmax(0, 1fr) minmax(0, 320px);
          gap: var(--space-4);
        }
        .v2-gex-col {
          display: flex; flex-direction: column; gap: var(--space-2);
          min-width: 0;
        }
        @media (max-width: 1400px) {
          .v2-gex-grid {
            grid-template-columns: minmax(0, 260px) minmax(0, 1fr);
          }
          .v2-gex-col--right { grid-column: 1 / -1; }
        }
        @media (max-width: 900px) {
          .v2-gex-grid { grid-template-columns: 1fr; }
          .v2-gex-header {
            grid-template-columns: 1fr;
            text-align: center;
          }
          .v2-gex-header__right { justify-content: center; flex-wrap: wrap; }
          .v2-gex-header__kpi { align-items: center; }
        }

        .v2-gex-summary {
          display: flex; flex-direction: column; gap: 14px;
        }
        .v2-gex-summary__otm,
        .v2-gex-summary__skew {
          padding-top: 8px;
          border-top: 1px dashed var(--border-subtle);
        }
        .v2-gex-summary__otm-label {
          font-size: 10px;
          letter-spacing: 0.1em;
          color: var(--text-tertiary);
          margin-bottom: 6px;
          text-transform: uppercase;
          font-weight: 600;
        }
        .v2-gex-summary__otm-row {
          display: flex; justify-content: space-between;
          font-size: 12px; color: var(--text-secondary);
          padding: 2px 0;
        }

        .v2-gex-trend {
          display: flex; align-items: center; gap: 12px;
          flex-wrap: wrap;
        }
        .v2-gex-trend__meta { flex: 1; font-size: 12px; }
        .v2-gex-trend__meta .pos { color: var(--accent-green); }
        .v2-gex-trend__meta .neg { color: var(--accent-red); }

        .v2-gex-kpigrid {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 14px 18px;
        }
        @media (max-width: 1100px) {
          .v2-gex-kpigrid { grid-template-columns: repeat(2, 1fr); }
        }

        .v2-gex-legend {
          list-style: none; padding: 0; margin: 0;
          display: flex; flex-direction: column; gap: 8px;
          font-size: 12px;
          color: var(--text-secondary);
          line-height: 1.4;
        }
        .v2-gex-legend strong { color: var(--text-primary); }
        .v2-gex-legend__sw {
          display: inline-block;
          width: 14px; height: 10px;
          margin-right: 8px;
          vertical-align: middle;
          border-radius: 1px;
        }
        .v2-gex-legend__sw--dashed {
          height: 0;
          border-top: 2px dashed;
          width: 14px;
        }

        .v2-gex-footer {
          font-size: 11px;
          padding: 6px 8px;
          color: var(--text-tertiary);
        }
        .dim { color: var(--text-tertiary); }
        .pos { color: var(--accent-green); }
        .neg { color: var(--accent-red); }
      `}</style>
    </div>
  );
}
