/* MITS Phase 19 Cluster D — Diagnostics (/v2/diagnostics).
 *
 * System-health dashboard. Composed from several backend surfaces:
 *
 *   GET /bot/status              running / cycles / last_cycle_at
 *   GET /audit/health            account + reconciliation + recent violations
 *   GET /lake-status/sources     per-source freshness ledger
 *   GET /monitoring/health       feed-breach summary
 *   GET /system/warnings         recent log warnings
 *   GET /data-quality/current    composite + feed scores + band
 *   GET /data-quality/score      session pricing-provider mix
 *   GET /lake/status             S3 bronze/silver/gold storage
 *   GET /diagnostics/cycle       last cycle scan diagnostics
 *
 * Page is purely informational — no buttons send commands.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, EmptyState, AlertBanner,
  BotHealthChip, KPIWidget, Sparkline,
} from '../../design/Components.jsx';

const POLL_MS = 15_000;

function fmtN(n, d = 0) {
  if (n == null || !isFinite(n)) return '—';
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
}
function fmtBytes(b) {
  if (b == null || !isFinite(b)) return '—';
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
function fmtAge(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function fmtUptime(sec) {
  if (sec == null || !isFinite(sec)) return '—';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/* ── Top KPI strip ──────────────────────────────────────────────────── */
function TopStrip({ status, score, lakeStatus, audit }) {
  const running = status?.running;
  const cycles = status?.cycles;
  const lastCycle = status?.last_cycle_at;
  const uptime = score?.uptime_seconds;
  const composite = score?.composite ?? null;
  const band = score?.band;
  const auditOk = audit?.ok;
  return (
    <div className="v2-diag-strip">
      <Card glow={running ? 'green' : 'red'}>
        <div className="v2-diag-engine">
          <div className="v2-diag-engine__title">Engine</div>
          <BotHealthChip status={running ? 'running' : 'paused'} cycles={cycles} lastCycleAt={lastCycle} />
          <div className="v2-diag-engine__sub mono">
            uptime {fmtUptime(uptime)}
          </div>
        </div>
      </Card>
      <Card>
        <Stat label="Last Cycle"
              value={fmtAge(lastCycle)}
              mono
              hint="When the engine last completed a full scan cycle" />
      </Card>
      <Card>
        <Stat label="Cycles Today"
              value={fmtN(cycles)}
              mono
              hint="Total engine cycles since session start" />
      </Card>
      <Card glow={band === 'good' ? 'green' : band === 'degraded' ? 'red' : 'cyan'}>
        <Stat label="Data Quality"
              value={composite != null ? `${composite}/100` : '—'}
              delta={band || ''}
              deltaPositive={band === 'good'}
              mono
              hint="Composite data-feed health score" />
      </Card>
      <Card glow={auditOk ? 'green' : 'red'}>
        <Stat label="Audit"
              value={auditOk ? 'OK' : 'VIOLATIONS'}
              deltaPositive={auditOk}
              delta={audit?.recent_trade_violations?.length
                     ? `${audit.recent_trade_violations.length} recent` : 'clean'}
              hint="Trade-audit reconciliation + recent violations" />
      </Card>
      <Card>
        <Stat label="Lake (Bronze)"
              value={lakeStatus?.layers?.bronze
                     ? fmtBytes(lakeStatus.layers.bronze.bytes) : '—'}
              delta={lakeStatus?.layers?.bronze
                     ? `${fmtN(lakeStatus.layers.bronze.object_count)} objs` : ''}
              mono
              hint="S3 bronze-layer storage (raw ingest)" />
      </Card>
      <style>{`
        .v2-diag-strip {
          display: grid;
          grid-template-columns: repeat(6, 1fr);
          gap: var(--space-3);
          margin-bottom: var(--space-4);
        }
        .v2-diag-engine {
          display: flex; flex-direction: column; gap: 8px;
        }
        .v2-diag-engine__title {
          font-size: var(--font-size-xs);
          font-weight: 600;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }
        .v2-diag-engine__sub {
          font-size: 11px;
          color: var(--text-tertiary);
        }
        @media (max-width: 1280px) {
          .v2-diag-strip { grid-template-columns: repeat(3, 1fr); }
        }
        @media (max-width: 700px) {
          .v2-diag-strip { grid-template-columns: repeat(2, 1fr); }
        }
      `}</style>
    </div>
  );
}

/* ── Data layer ─────────────────────────────────────────────────────── */
function DataLayer({ sources, score, qual }) {
  const list = Array.isArray(sources) ? sources : [];
  return (
    <Card>
      <h3 className="v2-diag-h3">Data Layer Health</h3>

      {qual && (
        <div className="v2-diag-qual">
          {Object.entries(qual.feed_scores || {}).map(([k, v]) => (
            <div key={k} className="v2-diag-qual__item">
              <div className="v2-diag-qual__lbl">{k}</div>
              <div className="v2-diag-qual__val mono">{v}/100</div>
              <div className="v2-diag-qual__bar">
                <div className="v2-diag-qual__bar-fill"
                     style={{
                       width: `${v}%`,
                       background: v >= 80 ? 'var(--accent-green)'
                                  : v >= 50 ? 'var(--accent-yellow)'
                                  : 'var(--accent-red)',
                     }} />
              </div>
            </div>
          ))}
        </div>
      )}

      {score?.providers && Object.keys(score.providers).length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="v2-diag-sub">Pricing-provider session mix</div>
          <div className="v2-diag-providers">
            {Object.entries(score.providers).map(([k, v]) => (
              <Pill key={k} tone={k.includes('reject') ? 'error' : k === 'none' ? 'warning' : 'info'}>
                {k}: {fmtN(v)}
              </Pill>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <div className="v2-diag-sub">Lake source ledger</div>
        {list.length === 0
          ? <EmptyState icon="📡" message="No lake sources reported." />
          : (
            <div style={{ overflowX: 'auto' }}>
              <table className="v2-table v2-table--striped">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Status</th>
                    <th style={{ textAlign: 'right' }}>Rows / 24h</th>
                    <th style={{ textAlign: 'right' }}>Pulls OK / Attempted</th>
                    <th>Last Error</th>
                    <th>Sparkline</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map(s => {
                    const spark = (s.sparkline || []).map(d => d.rows_written);
                    const tone = s.status === 'green' ? 'success'
                                : s.status === 'yellow' ? 'warning'
                                : s.status === 'red' ? 'error' : 'neutral';
                    const ratio = s.pulls_attempted ? (s.pulls_successful / s.pulls_attempted) : null;
                    return (
                      <tr key={s.source}>
                        <td className="mono v2-diag-src">{s.source}</td>
                        <td><Pill tone={tone}>{s.status || '—'}</Pill></td>
                        <td className="mono" style={{ textAlign: 'right' }}>{fmtN(s.rows_written_24h)}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>
                          {fmtN(s.pulls_successful)} / {fmtN(s.pulls_attempted)}
                          {ratio != null && (
                            <span style={{
                              marginLeft: 4,
                              color: ratio >= 0.99 ? 'var(--accent-green)'
                                    : ratio >= 0.9 ? 'var(--accent-yellow)'
                                    : 'var(--accent-red)',
                            }}>
                              ({(ratio * 100).toFixed(0)}%)
                            </span>
                          )}
                        </td>
                        <td style={{ maxWidth: 240, fontSize: 11, color: 'var(--text-tertiary)' }}
                            title={s.last_error_text || ''}>
                          {s.last_error_text
                            ? s.last_error_text.slice(0, 80) + '…'
                            : '—'}
                        </td>
                        <td>
                          <Sparkline data={spark} width={100} height={28} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
      </div>
      <style>{`
        .v2-diag-qual {
          display: grid;
          grid-template-columns: repeat(5, 1fr);
          gap: 12px;
        }
        .v2-diag-qual__item {
          background: var(--bg-secondary);
          border-radius: var(--radius-md);
          padding: 8px 10px;
          border: 1px solid var(--border-subtle);
        }
        .v2-diag-qual__lbl {
          font-size: 10px;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .v2-diag-qual__val {
          font-size: var(--font-size-lg);
          font-weight: 700;
          color: var(--text-primary);
          margin: 2px 0 6px;
        }
        .v2-diag-qual__bar {
          height: 4px;
          background: var(--bg-primary);
          border-radius: 2px;
          overflow: hidden;
        }
        .v2-diag-qual__bar-fill { height: 100%; }
        .v2-diag-sub {
          font-size: 11px;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          font-weight: 700;
          margin-bottom: 6px;
        }
        .v2-diag-providers { display: flex; flex-wrap: wrap; gap: 6px; }
        .v2-diag-src {
          font-size: 11px;
          max-width: 220px;
          color: var(--accent-cyan);
        }
        @media (max-width: 900px) {
          .v2-diag-qual { grid-template-columns: repeat(2, 1fr); }
        }
      `}</style>
    </Card>
  );
}

/* ── Engine layer ───────────────────────────────────────────────────── */
function EngineLayer({ status, warnings }) {
  const signals = Array.isArray(status?.recent_signals) ? status.recent_signals.slice(0, 6) : [];
  const recentWarn = Array.isArray(warnings?.records) ? warnings.records.slice(0, 8) : [];
  // Derive cycle durations from log timestamps spaced by interval (best-effort).
  return (
    <Card>
      <h3 className="v2-diag-h3">Engine Layer</h3>
      <div className="v2-diag-eng-grid">
        <div>
          <div className="v2-diag-sub">Recent Signals</div>
          {signals.length === 0
            ? <EmptyState icon="📡" message="No recent signals in /bot/status." />
            : (
              <ul className="v2-diag-signals">
                {signals.map((s, i) => (
                  <li key={i}>
                    <span className="mono v2-diag-signals__tk">{s.ticker}</span>
                    <Pill tone={s.action?.includes('HOLD') ? 'neutral' : 'info'}>
                      {s.action}
                    </Pill>
                    {s.strategy && <span className="mono v2-diag-signals__meta">{s.strategy}</span>}
                    <span className="mono v2-diag-signals__age">{fmtAge(s.timestamp)}</span>
                  </li>
                ))}
              </ul>
            )}
        </div>
        <div>
          <div className="v2-diag-sub">Recent Warnings / Errors</div>
          {recentWarn.length === 0
            ? <EmptyState icon="✓" message="No warnings in /system/warnings." />
            : (
              <ul className="v2-diag-warn">
                {recentWarn.map((w, i) => (
                  <li key={i}>
                    <Pill tone={w.level === 'ERROR' ? 'error'
                              : w.level === 'WARNING' ? 'warning'
                              : 'neutral'}>
                      {w.level || 'INFO'}
                    </Pill>
                    <span className="v2-diag-warn__msg">{w.message}</span>
                    <span className="mono v2-diag-warn__path">
                      {w.path}:{w.line}
                    </span>
                    <span className="mono v2-diag-warn__age">{fmtAge(w.timestamp)}</span>
                  </li>
                ))}
              </ul>
            )}
        </div>
      </div>
      <style>{`
        .v2-diag-eng-grid {
          display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
        }
        .v2-diag-signals,
        .v2-diag-warn {
          list-style: none; margin: 0; padding: 0;
          display: flex; flex-direction: column; gap: 6px;
          font-size: var(--font-size-sm);
        }
        .v2-diag-signals li,
        .v2-diag-warn li {
          display: flex; align-items: center; gap: 8px;
          padding: 6px 8px;
          background: var(--bg-secondary);
          border-radius: var(--radius-md);
        }
        .v2-diag-signals__tk { color: var(--accent-cyan); font-weight: 700; min-width: 60px; }
        .v2-diag-signals__meta { color: var(--text-tertiary); font-size: 11px; }
        .v2-diag-signals__age { margin-left: auto; color: var(--text-tertiary); font-size: 11px; }
        .v2-diag-warn__msg { flex: 1; color: var(--text-secondary); font-size: 12px; }
        .v2-diag-warn__path { color: var(--text-tertiary); font-size: 10px; }
        .v2-diag-warn__age { color: var(--text-tertiary); font-size: 10px; }
        @media (max-width: 900px) {
          .v2-diag-eng-grid { grid-template-columns: 1fr; }
        }
      `}</style>
    </Card>
  );
}

/* ── Storage layer ──────────────────────────────────────────────────── */
function StorageLayer({ lakeStatus, audit }) {
  const layers = lakeStatus?.layers || {};
  return (
    <Card>
      <h3 className="v2-diag-h3">Storage</h3>
      <div className="v2-diag-stor">
        {Object.entries(layers).map(([name, info]) => (
          <div key={name} className="v2-diag-stor__item">
            <div className="v2-diag-stor__name">{name}</div>
            <div className="v2-diag-stor__val mono">{fmtBytes(info?.bytes)}</div>
            <div className="v2-diag-stor__sub mono">
              {fmtN(info?.object_count)} objs
            </div>
            <div className="v2-diag-stor__sub mono">
              {info?.last_modified ? fmtAge(info.last_modified) : '—'}
            </div>
          </div>
        ))}
      </div>
      {audit?.account && (
        <div style={{ marginTop: 12 }}>
          <div className="v2-diag-sub">Paper Account Snapshot</div>
          <div className="v2-diag-acc">
            <div><span className="v2-diag-acc__k">Cash</span> <span className="mono">${fmtN(audit.account.cash, 2)}</span></div>
            <div><span className="v2-diag-acc__k">Positions MV</span> <span className="mono">${fmtN(audit.account.positions_market_value, 2)}</span></div>
            <div><span className="v2-diag-acc__k">Portfolio</span> <span className="mono">${fmtN(audit.account.portfolio_value, 2)}</span></div>
            <div><span className="v2-diag-acc__k">Realized P&L</span> <span className="mono">${fmtN(audit.account.realized_pnl, 2)}</span></div>
          </div>
        </div>
      )}
      <style>{`
        .v2-diag-stor {
          display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
        }
        .v2-diag-stor__item {
          background: var(--bg-secondary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-md);
          padding: 10px 12px;
        }
        .v2-diag-stor__name {
          font-size: 10px;
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.06em;
          font-weight: 700;
        }
        .v2-diag-stor__val {
          font-size: var(--font-size-lg);
          font-weight: 700;
          color: var(--text-primary);
          margin: 4px 0 2px;
        }
        .v2-diag-stor__sub { font-size: 11px; color: var(--text-tertiary); }
        .v2-diag-acc {
          display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
        }
        .v2-diag-acc__k {
          font-size: 11px; color: var(--text-tertiary);
          display: block;
        }
        @media (max-width: 700px) {
          .v2-diag-stor,
          .v2-diag-acc { grid-template-columns: repeat(2, 1fr); }
        }
      `}</style>
    </Card>
  );
}

/* ── Audit violations ──────────────────────────────────────────────── */
function AuditPanel({ audit }) {
  if (!audit) return null;
  const violations = audit.recent_trade_violations || [];
  return (
    <Card>
      <h3 className="v2-diag-h3">
        Audit
        <span style={{ marginLeft: 8 }}>
          {audit.ok
            ? <Pill tone="success">PASS</Pill>
            : <Pill tone="error">VIOLATIONS</Pill>}
        </span>
      </h3>
      <div className="v2-diag-sub">
        Reconciliation: <Pill tone={audit.reconciliation?.ok ? 'success' : 'error'}>
          {audit.reconciliation?.ok ? 'OK' : 'FAIL'}
        </Pill>
        <span style={{ marginLeft: 12 }}>
          Expired options:
        </span>
        <Pill tone={audit.expired_options?.ok ? 'success' : 'error'}>
          {audit.expired_options?.ok ? 'OK' : 'FAIL'}
        </Pill>
      </div>

      {violations.length === 0
        ? <EmptyState icon="✓" message="No recent trade-audit violations." />
        : (
          <div style={{ overflowX: 'auto', marginTop: 8 }}>
            <table className="v2-table v2-table--striped">
              <thead>
                <tr>
                  <th>Trade</th>
                  <th>Ticker</th>
                  <th>Violation</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {violations.slice(0, 12).map((v, i) => (
                  <tr key={i}>
                    <td className="mono">#{v.trade_id}</td>
                    <td className="mono">{v.ticker}</td>
                    <td><Pill tone="error">{v.name}</Pill></td>
                    <td style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{v.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {violations.length > 12 && (
              <div className="v2-diag-sub" style={{ marginTop: 6 }}>
                Showing 12 of {violations.length} violations.
              </div>
            )}
          </div>
        )}
    </Card>
  );
}

/* ── Page ──────────────────────────────────────────────────────────── */
export default function Diagnostics() {
  const [status, setStatus] = useState(null);
  const [audit, setAudit] = useState(null);
  const [sources, setSources] = useState([]);
  const [monitoring, setMonitoring] = useState(null);
  const [warnings, setWarnings] = useState(null);
  const [qual, setQual] = useState(null);
  const [score, setScore] = useState(null);
  const [lakeStatus, setLakeStatus] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function fetchAll() {
      const safe = async (url, fb = null) => {
        try {
          const r = await fetch(url);
          if (!r.ok) throw new Error(`${url} ${r.status}`);
          const ct = r.headers.get('content-type') || '';
          if (!ct.includes('json')) throw new Error(`${url} non-JSON`);
          return await r.json();
        } catch (e) { return fb; }
      };
      const [st, au, src, mo, wa, qu, sc, lk] = await Promise.all([
        safe('/bot/status'),
        safe('/audit/health'),
        safe('/lake-status/sources'),
        safe('/monitoring/health'),
        safe('/system/warnings'),
        safe('/data-quality/current'),
        safe('/data-quality/score'),
        safe('/lake/status'),
      ]);
      if (cancelled) return;
      setStatus(st);
      setAudit(au);
      setSources(src?.sources || (Array.isArray(src) ? src : []));
      setMonitoring(mo);
      setWarnings(wa);
      setQual(qu);
      setScore(sc);
      setLakeStatus(lk);
      setErr(st == null ? '/bot/status failed' : null);
    }
    fetchAll();
    const id = setInterval(fetchAll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const breaches = useMemo(() => {
    if (!monitoring) return [];
    return monitoring.breached_feeds || [];
  }, [monitoring]);

  return (
    <div className="v2-root v2-diag">
      <Section title="System Diagnostics"
               subtitle={status ? `${status.cycles} cycles · v${import.meta.env?.MODE || 'prod'}` : 'Loading…'}>
        {err && <AlertBanner severity="critical">{err}</AlertBanner>}

        {monitoring?.any_breach && (
          <AlertBanner severity="critical">
            Feed monitoring reports {breaches.length} breach{breaches.length === 1 ? '' : 'es'}:
            <span className="mono"> {breaches.join(', ')}</span>
          </AlertBanner>
        )}

        {qual?.should_abstain && (
          <AlertBanner severity="warning">
            Data quality score {qual.composite}/100 — bot is in
            <code className="mono"> {qual.band} </code> mode (×{qual.confidence_multiplier} confidence haircut).
          </AlertBanner>
        )}

        {audit && !audit.ok && (
          <AlertBanner severity="warning">
            Audit OK: false — recent trade violations present. Review the Audit panel below.
          </AlertBanner>
        )}

        {/* ROW 1 — KPIs */}
        <TopStrip status={status} score={score} lakeStatus={lakeStatus} audit={audit} />

        {/* ROW 2 — data layer */}
        <DataLayer sources={sources} score={score} qual={qual} />

        {/* ROW 3 — engine */}
        <EngineLayer status={status} warnings={warnings} />

        {/* ROW 4 — storage */}
        <StorageLayer lakeStatus={lakeStatus} audit={audit} />

        {/* ROW 5 — audit */}
        <AuditPanel audit={audit} />
      </Section>

      <style>{`
        .v2-diag { padding: var(--space-4) var(--space-6); }
        .v2-diag-h3 {
          font-size: var(--font-size-base);
          font-weight: 700;
          color: var(--text-primary);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin: 0 0 var(--space-3);
        }
      `}</style>
    </div>
  );
}
