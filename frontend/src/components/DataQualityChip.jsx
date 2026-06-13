/**
 * DataQualityChip — surfaces the options-data provider mix + sanity-flag
 * aggregates from the options.py in-process counters (P1.5). Sibling to
 * WarningsChip; mounted next to it in the Authority Spine.
 *
 * Backend: GET /system/data-quality, POST /system/data-quality/reset
 *
 * Visual rules:
 *  - "thetadata X" pill is green when ThetaData is the dominant provider
 *    AND no rejections recorded.
 *  - Warns when sanity rejection share > 10% of ThetaData attempts.
 *  - Danger when yfinance/cboe fallback share > 20% of total hits (means
 *    something is wrong with the primary feed).
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function formatUptime(seconds) {
  if (!seconds || seconds < 1) return 'just started';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const FLAG_LABELS = {
  stale: 'Stale quote',
  wide_spread: 'Wide spread',
  warn_spread: 'Wide-ish spread',
  no_quote: 'No quote',
  no_timestamp: 'No timestamp',
  parity_violation: 'Put-call parity violation',
  smile_outlier: 'IV smile outlier',
  intraday_iv_jump: 'Intra-tick IV jump',
  intraday_iv_warmup: 'Intra-tick window warming up',
};

const FLAG_DESCRIPTIONS = {
  stale: 'Quote timestamp older than the freshness threshold (5min during RTH, 18h after-hours). Caused the AAPL CALL incident 2026-06-01.',
  wide_spread: 'Bid-ask spread > 20% of mid — collapsed or illiquid book. Hard reject.',
  warn_spread: 'Bid-ask spread 10–20% of mid — uses the quote but confidence drops to medium.',
  no_quote: 'Both sides of the book have zero. No tradeable market.',
  no_timestamp: 'Quote has no timestamp at all. Treated as soft warning.',
  parity_violation: 'C - P deviates from S - K·e^(-rT) - q·K beyond tolerance. One leg is mispriced — hard reject.',
  smile_outlier: 'One strike\'s IV is > 3x median across nearby strikes. Single bad quote rather than a coherent smile shape.',
  intraday_iv_jump: 'ATM IV jumped more than 3.5 standard deviations vs the trailing intraday window. Either bad quote or undetected staleness.',
  intraday_iv_warmup: 'Fewer than 5 samples in the trailing window yet. Soft warning, not a reject.',
};

export default function DataQualityChip() {
  const [payload, setPayload] = useState(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api('/system/data-quality');
      setPayload(r);
    } catch { /* silent — endpoint may be missing in older builds */ }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);  // 30s — not urgent
    return () => clearInterval(id);
  }, [load]);

  const reset = async () => {
    try { await api('/system/data-quality/reset', { method: 'POST' }); await load(); }
    catch { /* silent */ }
  };

  const summary = useMemo(() => {
    if (!payload) return null;
    const providers = payload.providers || {};
    const flags = payload.sanity_flags || {};

    const total = Object.values(providers).reduce((a, b) => a + b, 0);
    const thetadata = providers.thetadata || 0;
    const rejected = providers.thetadata_rejected || 0;
    const yfHits = providers.yfinance || 0;
    const cboeHits = providers.cboe || 0;
    const noneHits = providers.none || 0;
    const fallbackShare = total > 0 ? (yfHits + cboeHits) / total : 0;
    const rejectShare = (thetadata + rejected) > 0
      ? rejected / (thetadata + rejected) : 0;
    const totalFlags = Object.values(flags).reduce((a, b) => a + b, 0);

    let severity = 'ok';
    if (noneHits > 0 || fallbackShare > 0.20) severity = 'danger';
    else if (rejectShare > 0.10 || totalFlags > 0) severity = 'warn';

    return {
      total, thetadata, rejected, yfHits, cboeHits, noneHits,
      fallbackShare, rejectShare, totalFlags, severity, providers, flags,
    };
  }, [payload]);

  if (!summary) return null;

  const cls = summary.severity === 'danger' ? 'pill danger'
            : summary.severity === 'warn' ? 'pill warn'
            : 'pill on';
  const icon = summary.severity === 'danger' ? '✗ '
             : summary.severity === 'warn' ? '⚠ '
             : '✓ ';
  const label = summary.total === 0
    ? 'data quality · no traffic'
    : `data ${summary.thetadata}/${summary.total} clean`;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className={cls}
        style={{
          border: 'none', cursor: 'pointer',
          fontSize: 11, padding: '3px 9px', fontWeight: 600,
        }}
        title={`ThetaData ${summary.thetadata} · rejected ${summary.rejected} · fallback ${summary.yfHits + summary.cboeHits} · none ${summary.noneHits}`}
      >
        {icon}{label}
      </button>

      {open && (
        <div
          onClick={() => setOpen(false)}
          style={{
            position: 'fixed', inset: 0,
            background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(4px)',
            display: 'grid', placeItems: 'center', zIndex: 100, padding: 24,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="panel"
            style={{ maxWidth: 720, width: '100%', maxHeight: '85vh', overflow: 'auto' }}
          >
            <div className="panel-head">
              <div>
                <div style={{
                  fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
                  color: 'var(--muted)', fontWeight: 600,
                }}>Options data quality · since process start ({formatUptime(payload.uptime_seconds)})</div>
                <h2 style={{ margin: '4px 0 0' }}>
                  {summary.total === 0
                    ? 'No traffic yet'
                    : `${summary.total} snapshot${summary.total === 1 ? '' : 's'}`}
                </h2>
              </div>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn small" onClick={reset} disabled={summary.total === 0}>
                  Reset counters
                </button>
                <button className="btn small" onClick={() => setOpen(false)}>Close</button>
              </div>
            </div>

            {/* Provider breakdown */}
            <div style={{ marginBottom: 16 }}>
              <div style={{
                fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
                color: 'var(--muted)', fontWeight: 600, marginBottom: 8,
              }}>Providers</div>
              <div style={{ display: 'grid', gap: 6 }}>
                {[
                  ['thetadata', 'ThetaData (primary)', 'on'],
                  ['thetadata_rejected', 'ThetaData → sanity rejected', 'warn'],
                  ['yfinance', 'yfinance (fallback)', 'off'],
                  ['cboe', 'Cboe (fallback)', 'off'],
                  ['none', 'No provider succeeded', 'danger'],
                ].map(([key, label, pillCls]) => {
                  const count = summary.providers[key] || 0;
                  if (count === 0 && key !== 'thetadata') return null;
                  const share = summary.total > 0
                    ? Math.round(count * 100 / summary.total) : 0;
                  return (
                    <div key={key} className="row" style={{
                      gap: 8, padding: '8px 10px',
                      background: 'var(--panel-2)',
                      border: `1px solid var(--border)`,
                      borderRadius: 6,
                    }}>
                      <span className={`pill ${pillCls}`} style={{ fontSize: 10, minWidth: 70, textAlign: 'center' }}>{count}</span>
                      <span style={{ fontSize: 12, flex: 1 }}>{label}</span>
                      <span style={{ fontSize: 11, color: 'var(--muted)', minWidth: 36, textAlign: 'right' }}>{share}%</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Sanity-flag breakdown */}
            <div style={{ marginBottom: 8 }}>
              <div style={{
                fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
                color: 'var(--muted)', fontWeight: 600, marginBottom: 8,
              }}>Sanity flags ({summary.totalFlags} total)</div>
              {summary.totalFlags === 0 ? (
                <div className="empty">
                  <div className="title">All clean ✓</div>
                  <div className="hint">No quotes failed staleness, spread, or has-quote checks this session.</div>
                </div>
              ) : (
                <div style={{ display: 'grid', gap: 6 }}>
                  {Object.entries(summary.flags)
                    .sort(([, a], [, b]) => b - a)
                    .map(([flag, count]) => (
                      <div key={flag} style={{
                        padding: '8px 10px',
                        background: 'var(--panel-2)',
                        border: `1px solid var(--border)`,
                        borderRadius: 6,
                        borderLeft: `3px solid ${flag === 'no_quote' || flag === 'wide_spread' ? 'var(--danger)' : 'var(--warn)'}`,
                      }}>
                        <div className="row" style={{ gap: 8, marginBottom: 2 }}>
                          <span className="pill warn" style={{ fontSize: 10, minWidth: 50, textAlign: 'center' }}>{count}</span>
                          <span style={{ fontSize: 12, fontWeight: 600 }}>
                            {FLAG_LABELS[flag] || flag}
                          </span>
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 58 }}>
                          {FLAG_DESCRIPTIONS[flag] || ''}
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </div>

            <div style={{
              marginTop: 12, fontSize: 11, color: 'var(--muted-2)',
              borderTop: '1px solid var(--border)', paddingTop: 8,
            }}>
              Counters reset on bot restart. Operator can also click "Reset counters" to baseline a fresh investigation.
            </div>
          </div>
        </div>
      )}
    </>
  );
}
