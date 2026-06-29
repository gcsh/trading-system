/**
 * Stage-16 — Data Quality composite score widget.
 *
 * Surfaces /data-quality/current: composite 0-100 score, band tag, per-feed
 * scores, confidence multiplier, and stale-feed list. Lives on the Risk
 * page since data degradation is fundamentally a risk consideration.
 */
import React, { useEffect, useState } from 'react';

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const BAND_PILL = {
  excellent: 'pill on',
  good: 'pill info',
  degraded: 'pill purple',
  poor: 'pill danger',
};

function feedColor(score) {
  if (score >= 85) return 'var(--accent)';
  if (score >= 60) return 'var(--text)';
  if (score >= 30) return 'var(--muted)';
  return 'var(--danger)';
}

export default function DataQualityWidget() {
  const [body, setBody] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchJson('/data-quality/current')
      .then(setBody)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="panel col-12">
        <h3 style={{ marginTop: 0 }}>📡 Data Quality</h3>
        <div style={{ color: 'var(--danger)' }}>{error}</div>
      </div>
    );
  }
  if (!body) {
    return <div className="panel col-12"><h3 style={{ marginTop: 0 }}>📡 Data Quality</h3>Loading…</div>;
  }

  const { composite, band, completeness, feed_scores, stale_feeds, confidence_multiplier, should_abstain } = body;

  return (
    <div className="panel col-12">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>📡 Data Quality</h3>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: 24, fontWeight: 600 }}>{composite}</span>
          <span className={BAND_PILL[band] || 'pill info'}>{band}</span>
          <span className={`pill ${confidence_multiplier >= 1.0 ? 'on' : confidence_multiplier >= 0.75 ? 'info' : 'danger'}`}>
            confidence × {confidence_multiplier.toFixed(2)}
          </span>
          {should_abstain && <span className="pill danger">would abstain</span>}
        </div>
      </div>
      <div className="row" style={{ gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div style={{ flex: '1 1 200px' }}>
          <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 4 }}>Feed scores</div>
          {Object.entries(feed_scores || {}).map(([feed, s]) => (
            <div key={feed} className="row" style={{ alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <span style={{ minWidth: 90, fontWeight: 500 }}>{feed}</span>
              <div style={{ flex: 1, background: 'var(--panel-2)', borderRadius: 3, height: 8, overflow: 'hidden' }}>
                <div style={{
                  width: `${Math.max(2, s)}%`, height: '100%',
                  background: feedColor(s),
                }} />
              </div>
              <span style={{ color: feedColor(s), minWidth: 32, textAlign: 'right', fontSize: 12, fontWeight: 600 }}>
                {s}
              </span>
            </div>
          ))}
        </div>
        <div style={{ minWidth: 200 }}>
          <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 4 }}>Snapshot completeness</div>
          <div style={{ fontSize: 18, fontWeight: 600 }}>{completeness}%</div>
          {stale_feeds?.length > 0 && (
            <>
              <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 10 }}>Stale feeds</div>
              <div className="row" style={{ gap: 4, flexWrap: 'wrap', marginTop: 4 }}>
                {stale_feeds.map((f) => (
                  <span key={f} className="pill danger">{f}</span>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
