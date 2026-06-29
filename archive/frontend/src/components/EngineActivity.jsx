/**
 * EngineActivity — answers "is anything actually happening?"
 *
 * Reads /bot/status (cycle telemetry + recent signals) so the operator
 * can see the engine's pulse even when no trades fired. After a fresh
 * reset the Chairman reports are empty, but the engine still
 * evaluates every ticker every cycle — this surface makes that
 * visible.
 *
 * Status decoration: BUY/SELL highlighted; HOLD shown with reason
 * (e.g., "RSI 79 outside band") so the operator sees WHY no trade
 * fired.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const ACTION_PILL = {
  BUY_STOCK: 'pill on',
  BUY_CALL: 'pill on',
  BUY_PUT: 'pill danger',
  SELL_STOCK: 'pill danger',
  SELL: 'pill danger',
  HOLD: 'pill off',
};

const STATUS_PILL = {
  submitted: 'pill on',
  signal_only: 'pill purple',
  consensus_abstain: 'pill warn',
  chairman_abstain: 'pill warn',
  chairman_monitor: 'pill info',
  meta_rejected: 'pill warn',
  too_small: 'pill warn',
  rejected: 'pill danger',
  drift_halt: 'pill danger',
  low_grade: 'pill warn',
  already_held: 'pill info',
  hold: 'pill off',
};

function timeAgo(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!t) return '—';
  const sec = Math.floor((Date.now() - t) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export default function EngineActivity() {
  const [status, setStatus] = useState(null);
  const [verifying, setVerifying] = useState(false);
  const [verification, setVerification] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const s = await api('/bot/status');
      setStatus(s);
      setError(null);
    } catch (e) { setError(e.message); }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  const verify = async () => {
    setVerifying(true);
    setVerification(null);
    try {
      const r = await api('/diagnostics/cycle');
      setVerification(r);
    } catch (e) { setError(e.message); }
    setVerifying(false);
  };

  const recent = status?.recent_signals || [];
  const grouped = useMemo(() => {
    // Reverse-sorted: most recent first
    return [...recent].reverse().slice(0, 12);
  }, [recent]);

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>Engine pulse</div>
          <h3 style={{ margin: '4px 0 0' }}>
            {status?.running ? 'Engine running' : 'Engine stopped'}
          </h3>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {status && (
            <>
              <span className={status.running ? 'pill on' : 'pill off'}>
                <span className="dot pulse" />
                {status.running ? 'live' : 'stopped'}
              </span>
              <span className="pill info">{status.cycles ?? 0} cycles</span>
              <span className="pill" title={status.last_cycle_at}>
                last cycle {timeAgo(status.last_cycle_at)}
              </span>
            </>
          )}
          <button className="btn small" onClick={verify} disabled={verifying}>
            {verifying ? 'Verifying…' : 'Verify all systems'}
          </button>
        </div>
      </div>

      {error && (
        <div className="accent-bear" style={{ fontSize: 12, marginBottom: 8 }}>
          {error}
        </div>
      )}

      {!status?.running && (
        <div className="empty">
          <div className="title">Engine is stopped</div>
          <div className="hint">Press <strong>Start</strong> in the top right to begin cycles.</div>
        </div>
      )}

      {status?.running && (
        <>
          <div className="section-title">Last 12 evaluations</div>
          {!grouped.length ? (
            <div className="empty">
              <div className="title">Waiting for first cycle…</div>
              <div className="hint">Engine started; first evaluations land within the cycle interval.</div>
            </div>
          ) : (
            // Constrained-height scroll: stays compact regardless of
            // how many tickers are in the scan universe.
            <div style={{
              display: 'grid', gap: 6,
              maxHeight: 480, overflowY: 'auto',
              paddingRight: 4,
            }}>
              {grouped.map((sig, i) => {
                const actionClass = ACTION_PILL[sig.action] || 'pill off';
                const statusClass = STATUS_PILL[sig.status] || 'pill off';
                const showStatus = sig.status && sig.status !== sig.action?.toLowerCase();
                return (
                  <div key={i} style={{
                    display: 'grid',
                    gridTemplateColumns: 'minmax(60px, 70px) minmax(56px, 70px) auto 1fr auto',
                    gap: 10,
                    padding: '8px 12px',
                    background: 'var(--panel-2)',
                    border: '1px solid var(--border)',
                    borderRadius: 8,
                    alignItems: 'center',
                  }}>
                    <span style={{ color: 'var(--muted)', fontSize: 11 }}>
                      {timeAgo(sig.timestamp)}
                    </span>
                    <strong style={{ fontSize: 12.5 }}>{sig.ticker}</strong>
                    <div className="row" style={{ gap: 6 }}>
                      <span className={actionClass}>{sig.action}</span>
                      {showStatus && <span className={statusClass}>{sig.status}</span>}
                    </div>
                    <span style={{
                      color: 'var(--muted)', fontSize: 12,
                      overflow: 'hidden', textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap', minWidth: 0,
                    }} title={sig.reason}>
                      {sig.reason}
                    </span>
                    <span style={{
                      fontSize: 11, color: 'var(--muted)',
                      fontFeatureSettings: '"tnum"',
                      minWidth: 32, textAlign: 'right',
                    }}>
                      {sig.confidence > 0 ? `${Math.round(sig.confidence * 100)}%` : ''}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {verification && (
        <VerificationReport report={verification} onClose={() => setVerification(null)} />
      )}
    </div>
  );
}

function VerificationReport({ report, onClose }) {
  const diags = report.diagnostics || [];
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0, 0, 0, 0.55)',
        backdropFilter: 'blur(4px)',
        display: 'grid', placeItems: 'center',
        zIndex: 100, padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{ maxWidth: 900, width: '100%', maxHeight: '80vh', overflow: 'auto' }}
      >
        <div className="panel-head">
          <div>
            <div style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600,
            }}>Verification · diagnostic cycle</div>
            <h2 style={{ margin: '4px 0 0' }}>System check · all subsystems</h2>
          </div>
          <button className="btn small" onClick={onClose}>Close</button>
        </div>

        <div className="kpi-row" style={{ marginBottom: 16 }}>
          <div className="kpi">
            <div className="kpi-label">Tickers scanned</div>
            <div className="kpi-value">{report.tickers_scanned}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Actionable</div>
            <div className="kpi-value">{report.actionable_count}</div>
            <div className="kpi-sub">of {report.tickers_scanned}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Auto-execute</div>
            <div className="kpi-value">{report.auto_execute ? 'on' : 'off'}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Min confidence</div>
            <div className="kpi-value">
              {Math.round((report.min_confidence || 0) * 100)}%
            </div>
          </div>
        </div>

        {diags.map((d, i) => (
          <div key={i} className="panel" style={{ marginBottom: 12, background: 'var(--panel-2)' }}>
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
              <h3 style={{ margin: 0 }}>{d.ticker}</h3>
              <div className="row" style={{ gap: 8, fontSize: 11.5, color: 'var(--muted)' }}>
                <span>price <strong className="accent-data">${d.snapshot?.price?.toFixed(2)}</strong></span>
                <span>RSI <strong className="accent-data">{d.snapshot?.rsi?.toFixed(0)}</strong></span>
                <span>VIX <strong className="accent-data">{d.snapshot?.vix?.toFixed(1)}</strong></span>
              </div>
            </div>
            <div style={{ display: 'grid', gap: 4 }}>
              {(d.strategies || []).map((s, j) => (
                <div key={j} className="row" style={{
                  justifyContent: 'space-between',
                  padding: '6px 10px',
                  borderRadius: 6,
                  background: s.would_act ? 'var(--accent-soft)' : 'transparent',
                  border: s.would_act ? '1px solid var(--accent-border)' : '1px solid transparent',
                }}>
                  <div className="row" style={{ gap: 8 }}>
                    <span style={{ fontSize: 12, fontWeight: 500, minWidth: 140 }}>{s.name}</span>
                    <span className={ACTION_PILL[s.action] || 'pill off'}>{s.action}</span>
                    {s.would_act && <span className="pill on">would act</span>}
                  </div>
                  <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
                    {s.reason}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
