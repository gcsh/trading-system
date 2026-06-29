/**
 * Stage-11.7 Feature Importance widget — global model importance.
 *
 * Hits /explain/importance, draws a horizontal bar chart of the top features
 * by permutation importance. Falls back gracefully to a "uniform fallback"
 * message when no model is trained yet (Stage-5 cold-start).
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

export default function FeatureImportance({ topK = 12 }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = (force = false) => {
    setRefreshing(true);
    setError(null);
    fetchJson(`/explain/importance?top_k=${topK}${force ? '&force=true' : ''}`)
      .then(setReport)
      .catch((e) => setError(e.message))
      .finally(() => setRefreshing(false));
  };

  useEffect(() => { load(false); }, [topK]);

  if (error) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>🧬 Model Feature Importance</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!report) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>🧬 Model Feature Importance</h3>
        <div style={{ color: 'var(--muted)' }}>Loading…</div>
      </div>
    );
  }
  const max = Math.max(...report.importances.map((i) => Math.abs(i.importance)), 0.001);
  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>🧬 Model Feature Importance</h3>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          {report.model_version && (
            <span className="pill purple">model {report.model_version}</span>
          )}
          <span className={`pill ${report.method === 'permutation' ? 'on' : 'off'}`}>
            {report.method}
          </span>
          {report.sample_size > 0 && (
            <span className="pill info">{report.sample_size} samples</span>
          )}
          <button className="btn small" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>
      {(report.warnings || []).map((w, i) => (
        <div key={i} style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 6 }}>{w}</div>
      ))}
      <div style={{ display: 'grid', gap: 6 }}>
        {report.importances.map((fi) => (
          <div key={fi.feature} className="row" style={{ alignItems: 'center', gap: 10 }}>
            <div style={{ minWidth: 160, fontWeight: 600 }}>{fi.feature}</div>
            <div style={{
              flex: 1,
              background: 'var(--panel-2)', borderRadius: 4, height: 10,
              overflow: 'hidden',
            }}>
              <div style={{
                width: `${Math.max(2, (Math.abs(fi.importance) / max) * 100)}%`,
                height: '100%', background: 'var(--accent)',
              }} />
            </div>
            <span style={{ color: 'var(--muted)', fontSize: 12, minWidth: 90 }}>
              {(fi.importance * 100).toFixed(3)}%
              {fi.std > 0 && ` ± ${(fi.std * 100).toFixed(2)}`}
            </span>
            <span className="pill off" style={{ minWidth: 80, textAlign: 'center' }}>
              {fi.kind}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
