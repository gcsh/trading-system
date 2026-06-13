/**
 * MITS Phase 3 — Tomorrow's Setup page.
 *
 * Displays rank-ordered EodAnalysis rows for a given date. Each row is
 * a card with the top pattern, posterior win-rate, suggested action,
 * invalidation list, and a deep-link into /analysis/:ticker.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

function fmtPct(v, digits = 0) {
  if (v == null || isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

function todayIso() {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}


function SetupCard({ row, rank }) {
  const sa = row.suggested_action;
  const post = row.top_posterior;
  const big = post != null ? (post * 100).toFixed(0) : '—';
  return (
    <div className="panel" style={{ padding: 14, marginBottom: 10 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 6 }}>
        <span style={{
          fontSize: 22, fontWeight: 800,
          color: 'var(--muted)', minWidth: 36,
        }}>
          #{rank}
        </span>
        <Link
          to={`/analysis/${encodeURIComponent(row.ticker)}?pattern=${encodeURIComponent(row.top_pattern || '')}`}
          style={{ textDecoration: 'none', color: 'var(--text)' }}
        >
          <span style={{ fontSize: 18, fontWeight: 700 }}>{row.ticker}</span>
        </Link>
        <span className="pill info" style={{ fontSize: 11 }}>
          {row.top_pattern || '—'}
        </span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 28, fontWeight: 800, color: post >= 0.6 ? 'var(--accent)' : 'var(--text)' }}>
          {big}%
        </span>
        <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 4 }}>
          posterior · N={row.top_sample_size ?? '?'}
        </span>
      </div>

      {row.headline && (
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>
          {row.headline}
        </div>
      )}
      {row.thesis_paragraph && (
        <div style={{ fontSize: 13, color: 'var(--text-soft)', lineHeight: 1.45, marginBottom: 8 }}>
          {row.thesis_paragraph}
        </div>
      )}

      {sa && (
        <div style={{
          padding: 10, background: 'var(--panel-2)',
          border: '1px dashed var(--accent)', borderRadius: 8,
          fontSize: 12, marginBottom: 8,
        }}>
          <div style={{ fontWeight: 700, color: 'var(--accent)', marginBottom: 4 }}>
            Suggested setup
          </div>
          <div>
            {sa.action} · strike <strong>{sa.strike ?? '—'}</strong> · DTE {sa.dte}
            {sa.strike_source && (
              <span
                title={sa.strike_source === 'chain'
                  ? 'Strike read from the listed options chain via ThetaData.'
                  : 'Chain unavailable — strike arithmetic-snapped to the nearest standard increment.'}
                style={{
                  marginLeft: 6, fontSize: 10, opacity: 0.75,
                  color: sa.strike_source === 'chain' ? '#5fc9ce' : '#e89a4c',
                }}
              >
                {sa.strike_source === 'chain' ? '(from chain)' : '(snap fallback)'}
              </span>
            )}
          </div>
          <div>target +{sa.target_premium_pct}% / stop -{sa.stop_premium_pct}%</div>
          {sa.rationale && (
            <div style={{ marginTop: 4, color: 'var(--muted)' }}>{sa.rationale}</div>
          )}
        </div>
      )}

      {Array.isArray(row.invalidation) && row.invalidation.length > 0 && (
        <div style={{ fontSize: 12 }}>
          <div style={{ color: 'var(--muted)', fontSize: 11, marginBottom: 3 }}>
            Invalidation
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {row.invalidation.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <Link
          to={`/analysis/${encodeURIComponent(row.ticker)}?pattern=${encodeURIComponent(row.top_pattern || '')}`}
          className="btn small primary"
          style={{ textDecoration: 'none' }}
        >
          View on chart →
        </Link>
      </div>
    </div>
  );
}


export default function Tomorrow() {
  const [dateStr, setDateStr] = useState(todayIso());
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [rebuilding, setRebuilding] = useState(false);

  const load = async (d) => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch(`/tomorrow?date=${encodeURIComponent(d)}&limit=20`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      setRows(body.rows || []);
      setCount(body.count || 0);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(dateStr); }, [dateStr]);

  const rebuild = async () => {
    setRebuilding(true);
    try {
      await fetch(`/tomorrow/rebuild?date=${encodeURIComponent(dateStr)}`, {
        method: 'POST',
      });
      // Wait a moment then reload — the pass runs async on a daemon thread.
      setTimeout(() => { load(dateStr); setRebuilding(false); }, 1500);
    } catch (e) {
      setErr(e.message); setRebuilding(false);
    }
  };

  const header = (
    <div className="panel-head" style={{ marginBottom: 8 }}>
      <div>
        <h2 style={{ margin: 0 }}>Tomorrow's Setup</h2>
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          {count} opportunit{count === 1 ? 'y' : 'ies'} ranked by historical edge.
        </div>
      </div>
      <div className="row" style={{ gap: 6 }}>
        <input
          type="date"
          value={dateStr}
          onChange={(e) => setDateStr(e.target.value)}
          style={{
            padding: '6px 8px', border: '1px solid var(--border)',
            background: 'var(--panel-2)', color: 'var(--text)', borderRadius: 6,
            fontFamily: 'inherit',
          }}
        />
        <button
          className="btn small"
          disabled={rebuilding}
          onClick={rebuild}
        >
          {rebuilding ? 'Rebuilding...' : 'Rebuild'}
        </button>
      </div>
    </div>
  );

  if (loading) return (<div>{header}<div style={{ padding: 24 }}>Loading...</div></div>);
  if (err) return (<div>{header}<div className="pill warning">{err}</div></div>);

  if (rows.length === 0) {
    return (
      <div>
        {header}
        <div className="panel" style={{ padding: 24, textAlign: 'center' }}>
          <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 6 }}>
            No setups for {dateStr}
          </div>
          <div style={{ color: 'var(--muted)', fontSize: 13, marginBottom: 12 }}>
            Next EOD pass scheduled at 16:30 ET on weekdays.
            <br />Rebuild manually with the button above.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      {header}
      {rows.map((r, i) => (
        <SetupCard key={`${r.ticker}-${r.id}`} row={r} rank={i + 1} />
      ))}
    </div>
  );
}
