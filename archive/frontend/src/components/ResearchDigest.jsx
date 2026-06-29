/**
 * Stage-16 — Research Digest "what changed today" widget.
 *
 * Surfaces /research/digest: a list of findings tagged info / warn / alert
 * across (agents, features, cohorts, feeds, cost). Lives on the AI Cockpit
 * as a daily-briefing panel.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const SEV_PILL = {
  info: 'pill info',
  warn: 'pill purple',
  alert: 'pill danger',
};

const AREA_ICON = {
  agents: '🏆',
  features: '🧬',
  cohorts: '🎯',
  feeds: '📡',
  cost: '💵',
};

export default function ResearchDigest() {
  const [digest, setDigest] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = () => {
    setRefreshing(true);
    setError(null);
    fetchJson('/research/digest')
      .then(setDigest)
      .catch((e) => setError(e.message))
      .finally(() => setRefreshing(false));
  };

  useEffect(load, []);

  if (error) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>🧠 What changed today</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!digest) {
    return <div className="panel col-12"><h3 style={{ marginTop: 0 }}>🧠 What changed today</h3>Loading…</div>;
  }

  const { findings = [], counts = {} } = digest;

  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>🧠 What changed today</h3>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          {counts.alert > 0 && <span className="pill danger">{counts.alert} alert</span>}
          {counts.warn > 0 && <span className="pill purple">{counts.warn} warn</span>}
          {counts.info > 0 && <span className="pill info">{counts.info} info</span>}
          <button className="btn small" onClick={load} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>
      {findings.length === 0 ? (
        <div style={{ color: 'var(--muted)' }}>
          Nothing changed today — agents stable, features stable, cohorts stable, feeds healthy.
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 6 }}>
          {findings.map((f, i) => (
            <div key={i} style={{
              padding: '8px 12px',
              background: 'var(--panel-2)',
              borderRadius: 6,
              border: '1px solid var(--border)',
              borderLeft: `3px solid ${
                f.severity === 'alert' ? 'var(--danger)'
                : f.severity === 'warn' ? 'var(--accent-2)'
                : 'var(--border-strong)'
              }`,
            }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                  <span style={{ fontSize: 16 }}>{AREA_ICON[f.area] || '·'}</span>
                  <span style={{ fontWeight: 600 }}>{f.title}</span>
                  <span className={SEV_PILL[f.severity] || 'pill info'}>{f.severity}</span>
                </div>
                {f.delta != null && (
                  <span style={{
                    fontSize: 12, fontWeight: 600,
                    color: f.delta >= 0 ? 'var(--accent)' : 'var(--danger)',
                  }}>
                    Δ {f.delta > 0 ? '+' : ''}{(f.delta * 100).toFixed(1)}%
                  </span>
                )}
              </div>
              <div style={{ color: 'var(--muted)', fontSize: 13, marginTop: 4 }}>
                {f.detail}
              </div>
            </div>
          ))}
        </div>
      )}
      <div style={{ color: 'var(--muted)', fontSize: 11, marginTop: 8 }}>
        Generated: {digest.generated_at}
      </div>
    </div>
  );
}
