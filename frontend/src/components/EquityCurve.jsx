/**
 * EquityCurve — portfolio value over time, with a range selector and
 * adaptive axes. Reads its own data so the range selector can
 * refetch without round-tripping through the Layout context.
 *
 * Ranges: 1D · 1W · 1M · 3M · 6M · 1Y · ALL.
 *   The backend (/portfolio/equity?range=…) decimates large windows to
 *   a max of 500 points so the chart stays responsive at multi-year
 *   horizons. The latest point is always preserved so "now" matches
 *   the topbar.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ReferenceLine,
  ReferenceDot,
} from 'recharts';
import { money, shortDate, shortTime } from '../lib/format.js';

const RANGES = [
  { id: '1d',    label: '1D',  poll: 10_000 },
  { id: '1w',    label: '1W',  poll: 30_000 },
  { id: '1m',    label: '1M',  poll: 60_000 },
  { id: '3m',    label: '3M',  poll: 120_000 },
  { id: '6m',    label: '6M',  poll: 300_000 },
  { id: '1y',    label: '1Y',  poll: 600_000 },
  { id: 'all',   label: 'ALL', poll: 600_000 },
];

function fmtDateTime(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;
}

function Stat({ label, value, accent, sub }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{
        fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
        color: 'var(--muted)', fontWeight: 600, marginBottom: 2,
      }}>{label}</div>
      <div style={{
        fontSize: 14, fontWeight: 700,
        color: accent || 'var(--text)',
        fontFeatureSettings: '"tnum"',
      }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 1 }}>{sub}</div>}
    </div>
  );
}

function Empty() {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Portfolio value</h2>
        <span className="panel-sub">no snapshots yet</span>
      </div>
      <div className="empty">
        Run the bot for a few cycles and the equity curve will appear here.
      </div>
    </div>
  );
}

export default function EquityCurve({ data: initialData }) {
  // Range state — defaults to ALL so the operator sees the full trajectory
  // on first visit. Persisted to localStorage so refresh keeps the choice.
  const [range, setRange] = useState(() => {
    if (typeof window === 'undefined') return 'all';
    return localStorage.getItem('tb-eq-range') || 'all';
  });
  const [data, setData] = useState(initialData || []);
  const [datasetNote, setDatasetNote] = useState('');
  const [loading, setLoading] = useState(false);
  const rangeMeta = RANGES.find((r) => r.id === range) || RANGES[RANGES.length - 1];

  // Fetch own data on range change + on a range-appropriate polling
  // interval. Default `range=trial` returns the legacy slice; we map
  // 'all' onto the new `?range=` query so old links still work.
  // MITS-P9.4: the backend may return a wrapped payload
  // ``{snapshots:[…], dataset_note, range:"last_session"}`` when 1d
  // falls back to the most recent session. Handle both shapes.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const r = await fetch(`/portfolio/equity?range=${encodeURIComponent(range)}&limit=500`);
        if (!r.ok) throw new Error(r.statusText);
        const j = await r.json();
        if (cancelled) return;
        if (Array.isArray(j)) {
          setData(j);
          setDatasetNote('');
        } else if (j && Array.isArray(j.snapshots)) {
          setData(j.snapshots);
          setDatasetNote(j.dataset_note || '');
        } else {
          setData([]);
          setDatasetNote('');
        }
      } catch (_e) {
        // silent — keep old data on transient errors
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    const id = setInterval(load, rangeMeta.poll);
    return () => { cancelled = true; clearInterval(id); };
  }, [range, rangeMeta.poll]);

  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('tb-eq-range', range);
  }, [range]);

  // Derived stats — start / current / peak / trough / drawdown.
  const stats = useMemo(() => {
    if (!data || data.length === 0) return null;
    const vals = data.map((d) => d.portfolio_value).filter((v) => Number.isFinite(v));
    if (vals.length === 0) return null;
    const first = data[0];
    const last = data[data.length - 1];
    const startVal = first.portfolio_value;
    const currentVal = last.portfolio_value;
    const change = currentVal - startVal;
    const changePct = startVal ? (change / startVal) * 100 : 0;
    const peak = Math.max(...vals);
    const trough = Math.min(...vals);
    // Max drawdown over the visible window
    let maxDD = 0; let running = -Infinity;
    for (const v of vals) {
      if (v > running) running = v;
      const dd = running > 0 ? (v - running) / running : 0;
      if (dd < maxDD) maxDD = dd;
    }
    const peakIdx = vals.indexOf(peak);
    const troughIdx = vals.indexOf(trough);
    return {
      first, last, startVal, currentVal, change, changePct,
      peak, trough, peakIdx, troughIdx, maxDDPct: maxDD * 100,
    };
  }, [data]);

  if (!data || data.length === 0) return <Empty />;
  if (!stats) return <Empty />;

  const positive = stats.change >= 0;
  const lineColor = positive ? 'var(--accent)' : 'var(--danger)';

  // Decide axis label formatter based on visible span.
  const firstTs = new Date(stats.first.timestamp).getTime();
  const lastTs = new Date(stats.last.timestamp).getTime();
  const spanMs = lastTs - firstTs;
  const DAY = 86_400_000;
  const xFmt = spanMs < DAY ? shortTime
    : spanMs < 90 * DAY ? shortDate
    : (ts) => {
        const d = new Date(ts);
        return `${d.toLocaleString(undefined, { month: 'short' })} ${String(d.getFullYear()).slice(2)}`;
      };

  // Auto-scale Y axis with padding around the actual move.
  const pad = Math.max((stats.peak - stats.trough) * 0.15, stats.peak * 0.002, 1);
  const yDomain = [Math.max(0, stats.trough - pad), stats.peak + pad];

  return (
    <div className="panel">
      <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>Portfolio value</h2>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 3 }}>
            {fmtDateTime(firstTs)} → {fmtDateTime(lastTs)} · {data.length} pts
            {loading && <span style={{ marginLeft: 8 }}> · refreshing…</span>}
          </div>
          {datasetNote && (
            <div style={{ fontSize: 11, color: 'var(--warn)', marginTop: 4 }}>
              {datasetNote}
            </div>
          )}
        </div>
        <div className="row" style={{ gap: 4, padding: 4,
            background: 'var(--panel-2)', border: '1px solid var(--border)',
            borderRadius: 8 }}>
          {RANGES.map((r) => (
            <button
              key={r.id}
              onClick={() => setRange(r.id)}
              className={`btn small ${range === r.id ? 'primary' : ''}`}
              style={{
                background: range === r.id ? undefined : 'transparent',
                border: range === r.id ? undefined : '1px solid transparent',
                padding: '4px 10px', fontSize: 11, fontWeight: 700,
                letterSpacing: '0.04em',
              }}
            >{r.label}</button>
          ))}
        </div>
      </div>

      {/* Summary strip */}
      <div className="row" style={{
        gap: 24, padding: '12px 4px 14px', flexWrap: 'wrap',
        borderBottom: '1px solid var(--border)', marginBottom: 12,
      }}>
        <Stat label="Start" value={money(stats.startVal)}
          sub={shortDate(stats.first.timestamp)} />
        <Stat
          label="Current"
          value={money(stats.currentVal)}
          accent={positive ? 'var(--accent)' : 'var(--danger)'}
          sub={`${positive ? '+' : ''}${money(stats.change)} (${positive ? '+' : ''}${stats.changePct.toFixed(2)}%)`}
        />
        <Stat label="Peak" value={money(stats.peak)} accent="var(--accent-2)" />
        <Stat label="Trough" value={money(stats.trough)} accent="var(--muted)" />
        <Stat label="Max drawdown" value={`${stats.maxDDPct.toFixed(2)}%`}
          accent={stats.maxDDPct < -5 ? 'var(--danger)'
                  : stats.maxDDPct < -2 ? 'var(--warn)' : 'var(--muted)'} />
      </div>

      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
            <defs>
              <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={lineColor} stopOpacity={0.45} />
                <stop offset="60%" stopColor={lineColor} stopOpacity={0.10} />
                <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="var(--border)" strokeDasharray="2 4" vertical={false} />
            <XAxis
              dataKey="timestamp"
              tickFormatter={xFmt}
              tick={{ fontSize: 11, fill: 'var(--muted)' }}
              stroke="var(--border)"
              minTickGap={56}
              tickLine={false}
            />
            <YAxis
              dataKey="portfolio_value"
              domain={yDomain}
              allowDecimals
              tick={{ fontSize: 11, fill: 'var(--muted)' }}
              tickFormatter={(v) => money(v)}
              width={84}
              stroke="var(--border)"
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--panel)',
                border: '1px solid var(--border-strong)',
                borderRadius: 10,
                fontSize: 12,
                boxShadow: '0 8px 24px -8px rgba(0,0,0,0.5)',
              }}
              labelFormatter={(label) => fmtDateTime(new Date(label).getTime())}
              formatter={(v) => [money(v), 'Equity']}
            />
            <ReferenceLine
              y={stats.startVal}
              stroke="var(--muted-2)"
              strokeDasharray="3 3"
              label={{ value: `Start ${money(stats.startVal)}`,
                position: 'insideTopLeft', fill: 'var(--muted)', fontSize: 10 }}
            />
            <ReferenceDot
              x={data[stats.peakIdx]?.timestamp}
              y={stats.peak}
              r={3} fill="var(--accent-2)" stroke="none"
            />
            <ReferenceDot
              x={data[stats.troughIdx]?.timestamp}
              y={stats.trough}
              r={3} fill="var(--danger)" stroke="none"
            />
            <Area
              type="monotone"
              dataKey="portfolio_value"
              stroke={lineColor}
              strokeWidth={2}
              fill="url(#eqFill)"
              dot={false}
              activeDot={{ r: 5, stroke: lineColor, strokeWidth: 2, fill: 'var(--panel)' }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
