/**
 * ExpiryDecompositionPanel — Item #14 third heatmap panel.
 *
 * REWRITE 2026-06-02: previously pivoted to show strikes-on-rows with
 * tiny expiry-colored bars. Operator feedback: "the expiration DATES
 * are what matter; the per-strike axis is already in the left panel."
 *
 * New layout: ROWS = expirations (date + DTE prominent), each row's
 * bar = total net GEX magnitude for that expiry. Color buckets by DTE
 * (0DTE / weekly / monthly OPEX). Click a row to see per-strike breakdown
 * inline.
 *
 * Data: /heatseeker/{ticker}/by-expiry
 *   → { expiries: [{expiry, dte, strikes: [...], totals: {net_gex, call_gex, put_gex}}, ...] }
 */
import React, { useEffect, useMemo, useState } from 'react';

const EXPIRY_PALETTE = [
  '#ff5d5d', // 0–1 day (0DTE)
  '#ff944d', // 2–4 (this week)
  '#ffd84d', // 5–8 (next week)
  '#9be65a', // 9–14 (monthly OPEX approach)
  '#5dc6ff', // 15–25 (monthly)
  '#a98bff', // 26+ (further out)
];

function bucketColor(dte) {
  if (dte <= 1) return EXPIRY_PALETTE[0];
  if (dte <= 4) return EXPIRY_PALETTE[1];
  if (dte <= 8) return EXPIRY_PALETTE[2];
  if (dte <= 14) return EXPIRY_PALETTE[3];
  if (dte <= 25) return EXPIRY_PALETTE[4];
  return EXPIRY_PALETTE[5];
}

function bucketLabel(dte) {
  if (dte <= 0) return '0DTE';
  if (dte === 1) return '1d';
  if (dte <= 4) return `${dte}d (this wk)`;
  if (dte <= 8) return `${dte}d (next wk)`;
  if (dte <= 14) return `${dte}d (OPEX wk)`;
  if (dte <= 25) return `${dte}d (monthly)`;
  return `${dte}d`;
}

function fmtCompact(n) {
  if (n == null || isNaN(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return n.toFixed(0);
}

// Friday-of-week / OPEX-Friday detection so we can label monthlies.
function formatExpiry(expiry, dte) {
  if (!expiry) return '—';
  // expiry is "YYYY-MM-DD"; parse without timezone shenanigans.
  const [y, m, d] = expiry.split('-').map(Number);
  if (!y || !m || !d) return expiry;
  const date = new Date(Date.UTC(y, m - 1, d));
  const month = date.toLocaleString('en-US', { month: 'short', timeZone: 'UTC' });
  const dow = date.toLocaleString('en-US', { weekday: 'short', timeZone: 'UTC' });
  // 3rd Friday of month = monthly OPEX
  const isMonthly = dow === 'Fri' && d >= 15 && d <= 21;
  return `${month} ${d} ${dow}${isMonthly ? ' · OPEX' : ''}`;
}


export default function ExpiryDecompositionPanel({ ticker, spotStrike }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setData(null); setErr(null); setExpanded(null);
    fetch(`/heatseeker/${encodeURIComponent(ticker)}/by-expiry`)
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(String(e)); });
    return () => { cancelled = true; };
  }, [ticker]);

  const { rows, maxAbs } = useMemo(() => {
    if (!data?.expiries?.length) return { rows: [], maxAbs: 1 };
    // One row per expiry. Total net GEX = signed sum across strikes.
    const out = data.expiries.map((ex) => {
      const netSum = (ex.strikes || []).reduce(
        (a, s) => a + (Number(s.net_gex) || 0), 0,
      );
      const callSum = (ex.strikes || []).reduce(
        (a, s) => a + (Number(s.call_gex) || 0), 0,
      );
      const putSum = (ex.strikes || []).reduce(
        (a, s) => a + (Number(s.put_gex) || 0), 0,
      );
      const totals = ex.totals || {};
      // Top 3 strikes by |net_gex| for the expanded view.
      const topStrikes = [...(ex.strikes || [])]
        .map((s) => ({ ...s, abs: Math.abs(Number(s.net_gex) || 0) }))
        .sort((a, b) => b.abs - a.abs)
        .slice(0, 5);
      return {
        expiry: ex.expiry, dte: ex.dte,
        net_gex: Number(totals.net_gex ?? netSum) || 0,
        call_gex: Number(totals.call_gex ?? callSum) || 0,
        put_gex: Number(totals.put_gex ?? putSum) || 0,
        topStrikes,
      };
    });
    const maxAbs = Math.max(1, ...out.map((r) => Math.abs(r.net_gex)));
    return { rows: out, maxAbs };
  }, [data]);

  if (err) return <div className="empty" style={{ fontSize: 11 }}>per-expiry: {err}</div>;
  if (!data) return <div className="empty" style={{ fontSize: 11 }}>loading per-expiry…</div>;
  if (!rows.length) return <div className="empty" style={{ fontSize: 11 }}>no multi-expiry chain available</div>;

  return (
    <div>
      <div style={{
        fontSize: 11, color: 'var(--muted)', marginBottom: 6,
        textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600,
      }}>
        GEX by Expiry · click a row for top strikes
      </div>
      <div style={{ display: 'grid', gap: 4 }}>
        {rows.map((r) => {
          const widthPct = (Math.abs(r.net_gex) / maxAbs) * 100;
          const isPositive = r.net_gex >= 0;
          const isOpen = expanded === r.expiry;
          return (
            <div key={r.expiry} style={{
              padding: '6px 8px',
              background: 'var(--panel-2)',
              borderRadius: 4,
              borderLeft: `3px solid ${bucketColor(r.dte)}`,
              cursor: 'pointer',
            }}
              onClick={() => setExpanded(isOpen ? null : r.expiry)}
            >
              <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                <span style={{
                  fontSize: 11.5, fontWeight: 600, color: 'var(--text)',
                  minWidth: 130,
                }}>
                  {formatExpiry(r.expiry, r.dte)}
                </span>
                <span style={{
                  fontSize: 10, color: 'var(--muted)',
                  background: 'var(--panel)',
                  padding: '1px 6px', borderRadius: 8,
                }}>
                  {bucketLabel(r.dte)}
                </span>
                <span style={{
                  marginLeft: 'auto',
                  fontSize: 11.5, fontWeight: 700,
                  color: isPositive ? 'var(--accent)' : 'var(--danger)',
                }}>
                  {isPositive ? '+' : ''}{fmtCompact(r.net_gex)}
                </span>
              </div>
              <div style={{
                marginTop: 4,
                display: 'flex', height: 6, borderRadius: 3, overflow: 'hidden',
                background: 'var(--panel)',
              }}>
                <div style={{
                  width: `${widthPct}%`,
                  background: isPositive ? 'var(--accent)' : 'var(--danger)',
                  opacity: 0.85,
                }} />
              </div>
              {isOpen && r.topStrikes.length > 0 && (
                <div style={{
                  marginTop: 8, paddingTop: 6, borderTop: '1px solid var(--border)',
                  fontSize: 10.5, display: 'grid', gap: 3,
                }}>
                  <div style={{ color: 'var(--muted)', marginBottom: 2 }}>
                    Top strikes by |net GEX|
                  </div>
                  {r.topStrikes.map((s, i) => {
                    const isSpot = spotStrike != null && Math.abs(s.strike - spotStrike) < 1e-6;
                    const pos = Number(s.net_gex) >= 0;
                    return (
                      <div key={i} className="row" style={{ gap: 6 }}>
                        <span style={{
                          minWidth: 50,
                          color: isSpot ? 'var(--info)' : 'var(--text-2)',
                          fontWeight: isSpot ? 700 : 400,
                        }}>
                          {Number(s.strike).toFixed(1)}
                        </span>
                        <span style={{
                          marginLeft: 'auto',
                          color: pos ? 'var(--accent)' : 'var(--danger)',
                          fontWeight: 600,
                        }}>
                          {pos ? '+' : ''}{fmtCompact(s.net_gex)}
                        </span>
                      </div>
                    );
                  })}
                  <div className="row" style={{ gap: 6, marginTop: 4, color: 'var(--muted)' }}>
                    <span style={{ minWidth: 50 }}>calls Σ</span>
                    <span style={{ marginLeft: 'auto', color: 'var(--accent)' }}>
                      {fmtCompact(r.call_gex)}
                    </span>
                  </div>
                  <div className="row" style={{ gap: 6, color: 'var(--muted)' }}>
                    <span style={{ minWidth: 50 }}>puts Σ</span>
                    <span style={{ marginLeft: 'auto', color: 'var(--danger)' }}>
                      {fmtCompact(r.put_gex)}
                    </span>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--muted-2)' }}>
        Bar length = |net GEX| for the expiry · color = DTE bucket · click to expand.
      </div>
    </div>
  );
}
