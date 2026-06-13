import React, { useCallback, useEffect, useState, useRef } from 'react';

function HealthAlertsBanner({ alerts, onAck }) {
  if (!alerts || alerts.length === 0) return null;
  const active = alerts.filter((a) => !a.resolved_at);
  if (active.length === 0) return null;
  const sevColor = (sev) => ({
    info: 'var(--info)',
    warning: 'var(--warn)',
    danger: 'var(--danger)',
    critical: 'var(--danger)',
  })[sev] || 'var(--warn)';
  return (
    <div className="panel" style={{
      padding: 12, marginTop: 12, marginBottom: 12,
      border: '1px solid var(--danger)',
      background: 'var(--danger-soft, rgba(255,90,95,0.08))',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ fontWeight: 600, color: 'var(--danger)' }}>
          Lake health: {active.length} active alert{active.length === 1 ? '' : 's'}
        </div>
      </div>
      <ul style={{ margin: '8px 0 0 16px', padding: 0, fontSize: 13 }}>
        {active.map((a) => (
          <li key={a.id} style={{ marginBottom: 4 }}>
            <span style={{ color: sevColor(a.severity), fontWeight: 600 }}>
              [{a.kind}]
            </span>{' '}
            {JSON.stringify(a.detail)}
            <button className="btn small" style={{ marginLeft: 8 }}
                    onClick={() => onAck(a.id)}>
              Acknowledge
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(1)} ${units[i]}`;
}

function fmtTs(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    return d.toLocaleString();
  } catch (_) {
    return ts;
  }
}

function LayerCard({ name, data }) {
  const stale = (() => {
    if (!data?.last_modified) return false;
    const ageHr = (Date.now() - new Date(data.last_modified).getTime()) / 3.6e6;
    return ageHr > 24;
  })();
  return (
    <div className="panel" style={{ padding: 16, minWidth: 220 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: 0, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
          {name}
        </h3>
        <span className={`pill ${stale ? 'danger' : 'success'}`} style={{ fontSize: 10 }}>
          {stale ? 'STALE' : 'FRESH'}
        </span>
      </div>
      <div style={{ marginTop: 12, fontSize: 13, color: 'var(--text-soft)' }}>
        <div><strong style={{ color: 'var(--text)' }}>{fmtBytes(data?.bytes)}</strong> total</div>
        <div>{data?.object_count ?? 0} objects</div>
        <div>last write: {fmtTs(data?.last_modified)}</div>
      </div>
    </div>
  );
}

function VectorCard({ stats }) {
  const entries = Object.entries(stats || {});
  if (!entries.length) {
    return (
      <div className="panel" style={{ padding: 16, minWidth: 260 }}>
        <h3 style={{ margin: 0 }}>VECTOR</h3>
        <div style={{ marginTop: 10, fontSize: 13, color: 'var(--muted)' }}>
          pgvector not reachable from this host or no entries indexed yet.
          Run <code>python bin/backfill_vectors.py</code> after deploy.
        </div>
      </div>
    );
  }
  const total = entries.reduce((a, [, s]) => a + (Number(s?.count) || 0), 0);
  return (
    <div className="panel" style={{ padding: 16, minWidth: 260 }}>
      <h3 style={{ margin: 0 }}>VECTOR</h3>
      <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-soft)' }}>
        <strong style={{ color: 'var(--text)' }}>{total.toLocaleString()}</strong> embeddings total
      </div>
      <table style={{ width: '100%', marginTop: 10, fontSize: 12 }}>
        <thead>
          <tr><th align="left">namespace</th><th align="right">count</th><th align="left">latest</th></tr>
        </thead>
        <tbody>
          {entries.map(([ns, s]) => (
            <tr key={ns}>
              <td>{ns}</td>
              <td align="right">{s?.count?.toLocaleString?.() ?? '—'}</td>
              <td>{fmtTs(s?.last_created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Sparkline({ points, width = 80, height = 24 }) {
  if (!points || !points.length) {
    return <div style={{ width, height, color: 'var(--muted)', fontSize: 10 }}>—</div>;
  }
  const values = points.map((p) => Number(p.rows_written) || 0);
  const max = Math.max(1, ...values);
  const dx = width / Math.max(1, points.length - 1);
  const pts = values
    .map((v, i) => `${(i * dx).toFixed(1)},${(height - (v / max) * height).toFixed(1)}`)
    .join(' ');
  const stroke = points[points.length - 1].status === 'red'
    ? 'var(--danger)'
    : points[points.length - 1].status === 'yellow'
      ? 'var(--warn, #e89a4c)'
      : 'var(--success, #5fc9ce)';
  return (
    <svg width={width} height={height} aria-label="rows written sparkline">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth={1.5} />
    </svg>
  );
}

function StatusDot({ status }) {
  const color = status === 'green' ? 'var(--success, #5fc9ce)'
    : status === 'yellow' ? 'var(--warn, #e89a4c)'
    : status === 'red' ? 'var(--danger)'
    : 'var(--muted)';
  return (
    <span title={status} style={{
      display: 'inline-block', width: 10, height: 10,
      borderRadius: 5, background: color, marginRight: 6,
    }} />
  );
}

function SourceCard({ source }) {
  const tooltip = source.last_error_text
    || (source.status === 'unknown'
        ? 'No backfill activity yet on this source.'
        : `${source.pulls_successful}/${source.pulls_attempted} successful pulls`);
  return (
    <div className="panel" style={{
      padding: 12, minWidth: 220, display: 'grid', gap: 6,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                          alignItems: 'center' }}>
        <div style={{ fontWeight: 600, fontSize: 12,
                            textTransform: 'uppercase', letterSpacing: '0.04em' }}>
          <StatusDot status={source.status} />
          {source.source}
        </div>
        <Sparkline points={source.sparkline} />
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-soft)' }}>
        <div>24h rows: <strong style={{ color: 'var(--text)' }}>
          {Number(source.rows_written_24h || 0).toLocaleString()}
        </strong></div>
        <div>Latest: {source.snapshot_date || '—'}</div>
        {source.avg_latency_ms != null && (
          <div>Latency: {Number(source.avg_latency_ms).toFixed(0)}ms</div>
        )}
        {source.last_error_text && (
          <div style={{ color: 'var(--danger)', fontSize: 11,
                              marginTop: 4 }} title={tooltip}>
            {String(source.last_error_text).slice(0, 80)}
          </div>
        )}
      </div>
    </div>
  );
}

function DataSourcesPanel({ data }) {
  if (!data) return null;
  return (
    <div className="panel" style={{ padding: 16, marginTop: 18 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                          alignItems: 'center', marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Data Sources (Phase 11)</h3>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          rollup:&nbsp;<StatusDot status={data.rollup_status} />
          {data.rollup_status}
          &nbsp;·&nbsp;
          green {data.count_by_status?.green ?? 0} ·
          yellow {data.count_by_status?.yellow ?? 0} ·
          red {data.count_by_status?.red ?? 0} ·
          unknown {data.count_by_status?.unknown ?? 0}
        </div>
      </div>
      <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
        {(data.sources || []).map((s) => (
          <SourceCard key={s.source} source={s} />
        ))}
      </div>
    </div>
  );
}

function DataQualityPanel({ data, onPickTicker }) {
  if (!data) return null;
  return (
    <div className="panel" style={{ padding: 16, marginTop: 18 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>Cross-vendor Parity (yfinance vs ThetaData)</h3>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          {data.total_audited_rows?.toLocaleString?.()} rows audited
        </div>
      </div>
      <div className="row" style={{ gap: 14, marginTop: 10, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>SUSPECT</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--danger)' }}>
            {Number(data.suspect_total || 0).toLocaleString()}
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            {((data.suspect_pct_of_total || 0) * 100).toFixed(1)}% of all audited rows
          </div>
        </div>
        <div style={{ minWidth: 360, flex: 1 }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
            Top suspect tickers (suspect days / audited days)
          </div>
          <table style={{ width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                <th align="left">ticker</th>
                <th align="right">suspect</th>
                <th align="right">audited</th>
                <th align="right">%</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {(data.top_suspect_tickers || []).map((row) => (
                <tr key={row.ticker}>
                  <td>{row.ticker}</td>
                  <td align="right">{row.suspect_days.toLocaleString()}</td>
                  <td align="right">{row.audited_days.toLocaleString()}</td>
                  <td align="right">{((row.suspect_pct || 0) * 100).toFixed(1)}%</td>
                  <td>
                    <button className="btn small" onClick={() => onPickTicker(row.ticker)}>
                      drill
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ minWidth: 280, flex: 1 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 2 }}>
            Daily divergence between yfinance and ThetaData close — warn + suspect rows only
          </div>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6, lineHeight: 1.4 }}>
            Warn = ≥0.5% divergence on the day. Suspect = ≥2% — these rows are filtered out of knowledge-graph aggregation.
          </div>
          {(data.divergence_histogram || []).length === 0 && (
            <div style={{ color: 'var(--muted)', fontSize: 12 }}>
              No warn/suspect rows yet.
            </div>
          )}
          {(data.divergence_histogram || []).length > 0 && (
            <svg width="100%" height="110" viewBox="0 0 260 110">
              {(() => {
                const buckets = data.divergence_histogram;
                const max = Math.max(1, ...buckets.map((b) => b.count));
                const plotX0 = 28;
                const plotX1 = 258;
                const plotW = plotX1 - plotX0;
                const bw = plotW / Math.max(1, buckets.length);
                const bars = buckets.map((b, i) => {
                  const h = (b.count / max) * 70;
                  const fill = b.lo_pct >= 2.0 ? 'var(--danger)' : 'var(--warn, #e89a4c)';
                  return (
                    <rect key={i} x={plotX0 + i * bw} y={80 - h}
                              width={Math.max(0, bw - 1)} height={h}
                              fill={fill} opacity={0.9}>
                      <title>{`${b.lo_pct.toFixed(2)}%–${b.hi_pct.toFixed(2)}%: ${b.count}`}</title>
                    </rect>
                  );
                });
                const lastIdx = buckets.length - 1;
                const midIdx = Math.floor(buckets.length / 2);
                const tickX = (i) => plotX0 + i * bw + bw / 2;
                return (
                  <g>
                    {bars}
                    <line x1={plotX0} y1="80" x2={plotX1} y2="80" stroke="var(--muted)" />
                    <text x={tickX(0)} y="92" fontSize="9" fill="var(--muted)" textAnchor="middle">
                      {`${buckets[0].lo_pct.toFixed(1)}%`}
                    </text>
                    {buckets.length > 2 && (
                      <text x={tickX(midIdx)} y="92" fontSize="9" fill="var(--muted)" textAnchor="middle">
                        {`${buckets[midIdx].lo_pct.toFixed(1)}%`}
                      </text>
                    )}
                    <text x={tickX(lastIdx)} y="92" fontSize="9" fill="var(--muted)" textAnchor="middle">
                      {`${buckets[lastIdx].hi_pct.toFixed(1)}%`}
                    </text>
                    <text x="143" y="105" textAnchor="middle" fontSize="10" fill="var(--muted)">
                      Divergence bucket (% close-to-close)
                    </text>
                    <text x="24" y="13" fontSize="9" fill="var(--muted)" textAnchor="end">
                      {max.toLocaleString()}
                    </text>
                    <text x="24" y="48" fontSize="9" fill="var(--muted)" textAnchor="end">
                      {Math.round(max / 2).toLocaleString()}
                    </text>
                    <text x="8" y="40" transform="rotate(-90 8 40)" fontSize="10" fill="var(--muted)" textAnchor="middle">
                      Rows
                    </text>
                  </g>
                );
              })()}
            </svg>
          )}
        </div>
      </div>
      <div style={{ marginTop: 10, fontSize: 11, color: 'var(--muted)',
                          fontStyle: 'italic' }}>
        {data.disclosure}
      </div>
    </div>
  );
}

function ParityDrilldown({ ticker, onClose }) {
  const [rows, setRows] = React.useState(null);
  React.useEffect(() => {
    if (!ticker) return;
    let active = true;
    fetch(`/data-quality/parity/${encodeURIComponent(ticker)}?limit=120`)
      .then((r) => r.json())
      .then((d) => { if (active) setRows(d.rows || []); })
      .catch(() => active && setRows([]));
    return () => { active = false; };
  }, [ticker]);
  if (!ticker) return null;
  return (
    <div className="panel" style={{ padding: 14, marginTop: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between',
                          alignItems: 'center' }}>
        <h4 style={{ margin: 0 }}>Parity drilldown — {ticker}</h4>
        <button className="btn small" onClick={onClose}>close</button>
      </div>
      <table style={{ width: '100%', fontSize: 12, marginTop: 8 }}>
        <thead>
          <tr>
            <th align="left">date</th>
            <th align="right">close_a (yf)</th>
            <th align="right">close_b (theta)</th>
            <th align="right">div_pct</th>
            <th align="left">severity</th>
          </tr>
        </thead>
        <tbody>
          {(rows || []).slice(0, 40).map((r) => (
            <tr key={r.id} style={{
              color: r.severity === 'suspect' ? 'var(--danger)'
                  : r.severity === 'warn' ? 'var(--warn, #e89a4c)' : undefined,
            }}>
              <td>{r.audit_date}</td>
              <td align="right">{(r.close_a ?? '—').toString()}</td>
              <td align="right">{(r.close_b ?? '—').toString()}</td>
              <td align="right">{r.divergence_pct != null
                ? `${(r.divergence_pct * 100).toFixed(2)}%` : '—'}</td>
              <td>{r.severity}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecentSnapshotsHeatmap({ snapshots }) {
  const entries = Object.entries(snapshots || {});
  if (!entries.length) {
    return <div style={{ color: 'var(--muted)' }}>No snapshots yet.</div>;
  }
  return (
    <div className="panel" style={{ padding: 16, marginTop: 18 }}>
      <h3 style={{ margin: 0 }}>Nightly snapshot health (last 7 dates)</h3>
      <table style={{ width: '100%', marginTop: 10, fontSize: 12 }}>
        <thead>
          <tr>
            <th align="left">table</th>
            <th align="left">most-recent</th>
            <th align="left">history</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([table, dates]) => (
            <tr key={table}>
              <td>{table}</td>
              <td>{dates.slice(-1)[0] || '—'}</td>
              <td>{dates.length ? dates.join(' · ') : 'none'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MemoryChip({ data }) {
  if (!data) return null;
  const color = data.color || 'green';
  const bg = ({
    green: 'rgba(56,176,0,0.18)',
    yellow: 'rgba(244,180,0,0.20)',
    red: 'rgba(255,90,95,0.22)',
  })[color] || 'rgba(56,176,0,0.18)';
  const fg = ({
    green: 'var(--ok, #2e8b57)',
    yellow: 'var(--warn, #b8860b)',
    red: 'var(--danger, #c82a2a)',
  })[color] || 'var(--ok)';
  const pct = (data.percent ?? 0).toFixed(1);
  const avail = (data.available_gb ?? 0).toFixed(1);
  const total = (data.total_gb ?? 0).toFixed(1);
  return (
    <span title={`Memory: ${pct}% used, ${avail}/${total} GB free`}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px',
            borderRadius: 999,
            fontSize: 11,
            fontWeight: 600,
            background: bg,
            color: fg,
            border: `1px solid ${fg}`,
          }}>
      MEM {pct}%
    </span>
  );
}

function DuckDBChip({ data }) {
  if (!data) return null;
  const ok = !!data.ok;
  const httpfs = !!data.httpfs;
  return (
    <span title={`DuckDB ${ok ? 'live' : 'down'} (httpfs ${httpfs ? 'loaded' : 'missing'})`}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px',
            borderRadius: 999,
            fontSize: 11,
            fontWeight: 600,
            background: ok ? 'rgba(56,176,0,0.18)' : 'rgba(255,90,95,0.22)',
            color: ok ? 'var(--ok, #2e8b57)' : 'var(--danger, #c82a2a)',
            border: `1px solid ${ok ? 'var(--ok, #2e8b57)' : 'var(--danger, #c82a2a)'}`,
          }}>
      DUCKDB {ok ? 'OK' : 'DOWN'}
    </span>
  );
}


export default function LakeStatus() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [alerts, setAlerts] = useState([]);
  const [sources, setSources] = useState(null);
  const [parity, setParity] = useState(null);
  const [drillTicker, setDrillTicker] = useState(null);
  const [memory, setMemory] = useState(null);
  const [duckdb, setDuckdb] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/lake/status');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStatus(data);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
    try {
      const ar = await fetch('/lake/health/alerts');
      if (ar.ok) {
        const aj = await ar.json();
        setAlerts(aj.alerts || []);
      }
    } catch (_) { /* alert endpoint is optional */ }
    try {
      const sr = await fetch('/lake-status/sources');
      if (sr.ok) setSources(await sr.json());
    } catch (_) { /* optional */ }
    try {
      const pr = await fetch('/data-quality/parity');
      if (pr.ok) setParity(await pr.json());
    } catch (_) { /* optional */ }
    // MITS Phase 11.1 #9 — memory pressure chip.
    try {
      const mr = await fetch('/lake/memory');
      if (mr.ok) setMemory(await mr.json());
    } catch (_) { /* optional */ }
    // MITS Phase 11.1 #7 — DuckDB read-layer health.
    try {
      const dr = await fetch('/lake/duckdb');
      if (dr.ok) setDuckdb(await dr.json());
    } catch (_) { /* optional */ }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const ackAlert = useCallback(async (id) => {
    try {
      await fetch(`/lake/health/alerts/${id}/ack`, { method: 'POST' });
      await load();
    } catch (e) {
      alert(`Ack failed: ${e}`);
    }
  }, [load]);

  const forceSnapshot = async () => {
    const secret = window.prompt('Admin secret (X-Lake-Admin-Secret):');
    if (!secret) return;
    setBusy(true);
    try {
      const res = await fetch('/lake/snapshot/now', {
        method: 'POST',
        headers: { 'X-Lake-Admin-Secret': secret },
      });
      const data = await res.json();
      alert(JSON.stringify(data).slice(0, 600));
      await load();
    } catch (e) {
      alert(`Snapshot failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  const showRestoreCmd = async () => {
    const dt = window.prompt('Restore date (YYYY-MM-DD)?');
    if (!dt) return;
    try {
      const res = await fetch(`/lake/restore?date=${encodeURIComponent(dt)}`, { method: 'POST' });
      const data = await res.json();
      window.alert(data?.ssm_command || JSON.stringify(data));
    } catch (e) {
      alert(`Failed: ${e}`);
    }
  };

  if (!status) {
    return <div className="panel" style={{ padding: 16 }}>Loading lake status…{error ? ` (${error})` : ''}</div>;
  }

  const layers = status.layers || {};

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h2 style={{ margin: 0 }}>Data Lake</h2>
          <div style={{ color: 'var(--muted)', fontSize: 12 }}>
            Bucket {status.bucket} ({status.region}) — bronze enabled: {String(status.enabled)}
          </div>
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <MemoryChip data={memory} />
          <DuckDBChip data={duckdb} />
          <button className="btn small" onClick={load}>Refresh</button>
          <button className="btn small" disabled={busy} onClick={forceSnapshot}>Force snapshot now</button>
          <button className="btn small danger" onClick={showRestoreCmd}>Restore…</button>
        </div>
      </div>

      <HealthAlertsBanner alerts={alerts} onAck={ackAlert} />

      <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginTop: 18 }}>
        <LayerCard name="BRONZE" data={layers.bronze} />
        <LayerCard name="SILVER" data={layers.silver} />
        <LayerCard name="GOLD" data={layers.gold} />
        <LayerCard name="ATHENA" data={layers.athena} />
        <VectorCard stats={status.vectors} />
      </div>

      <DataSourcesPanel data={sources} />
      <DataQualityPanel data={parity} onPickTicker={setDrillTicker} />
      <ParityDrilldown ticker={drillTicker} onClose={() => setDrillTicker(null)} />

      <RecentSnapshotsHeatmap snapshots={status.recent_snapshots} />
    </div>
  );
}
