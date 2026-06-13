/**
 * Stage-20-UI · Source Attribution.
 *
 * Reads /source-attribution/contributions and renders the per-source
 * contribution table — correlation with realized P&L per source over
 * recent closed trades. This is the page that tells you which
 * intelligence streams are actually paying off.
 *
 * Negative r ⇒ the source is anti-predictive (or your interpretation
 * of it is inverted). Near-zero r ⇒ adds noise without information.
 * "Need more data" rows mean the source hasn't crossed the min_trades
 * threshold yet — keep operating.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const SOURCE_PILL = {
  breadth: 'pill data',
  macro: 'pill data',
  edgar: 'pill purple',
  short_interest: 'pill warn',
  cot: 'pill purple',
  insider: 'pill heat',
};

function CorrelationBar({ value }) {
  if (value == null) {
    return (
      <div className="gauge-track" style={{ width: 140 }}>
        <div className="gauge-fill" style={{ width: 0 }} />
      </div>
    );
  }
  const pct = Math.abs(value) * 100;
  const positive = value >= 0;
  return (
    <div style={{ width: 140 }}>
      <div className="gauge-track">
        <div className="gauge-fill" style={{
          width: `${Math.min(100, pct)}%`,
          background: positive
            ? 'linear-gradient(90deg, var(--accent), var(--data))'
            : 'linear-gradient(90deg, var(--danger), var(--heat))',
        }} />
      </div>
      <div style={{
        fontSize: 11, color: positive ? 'var(--accent-2)' : 'var(--danger-2)',
        marginTop: 2, fontFeatureSettings: '"tnum"', textAlign: 'right',
      }}>
        r = {value >= 0 ? '+' : ''}{value.toFixed(3)}
      </div>
    </div>
  );
}

export default function SourceAttribution() {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [minTrades, setMinTrades] = useState(30);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api(`/source-attribution/contributions?min_trades=${minTrades}`);
      setReport(r);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [minTrades]);

  useEffect(() => { load(); }, [load]);

  const sources = report?.sources || [];
  const closedTrades = report?.closed_trades || 0;

  const stats = useMemo(() => {
    const withData = sources.filter((s) => s.correlation_with_pnl != null);
    const positive = withData.filter((s) => s.correlation_with_pnl > 0).length;
    const negative = withData.filter((s) => s.correlation_with_pnl < 0).length;
    const insufficient = sources.length - withData.length;
    const topShare = sources.reduce((m, s) => Math.max(m, s.contribution_share || 0), 0);
    return { positive, negative, insufficient, topShare };
  }, [sources]);

  return (
    <div>
      {error && (
        <div className="panel panel--bear" style={{ marginBottom: 16 }}>
          <div className="accent-bear">{error}</div>
        </div>
      )}

      <div className="hero" style={{ marginBottom: 24 }}>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div className="accent-data" style={{
              fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
              fontWeight: 600, marginBottom: 6,
            }}>Stage 19.2 · Source Contribution Tracker</div>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              Which intelligence streams are actually paying off?
            </h2>
            <div style={{ color: 'var(--muted)', marginTop: 8, fontSize: 13, maxWidth: 720 }}>
              Each closed trade gets a snapshot of per-source scores at decision time.
              The correlation with realized P&L tells you which sources predict outcomes
              and which add noise. Below ≥ <em>min_trades</em> non-null scores per source,
              correlation is suppressed.
            </div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <label style={{ marginBottom: 0, fontSize: 12 }}>min trades</label>
            <input
              type="number"
              min={5} max={500} step={5}
              value={minTrades}
              onChange={(e) => setMinTrades(Number(e.target.value))}
              style={{ width: 80 }}
            />
            <button className="btn small" onClick={load} disabled={loading}>
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </div>
        </div>
      </div>

      <div className="metric-strip">
        <div className="metric-card">
          <div className="label">Closed trades scored</div>
          <div className="value">{closedTrades}</div>
          <div className="delta">corpus size</div>
        </div>
        <div className="metric-card positive">
          <div className="label">Positive r</div>
          <div className="value">{stats.positive}</div>
          <div className="delta">helping P&L</div>
        </div>
        <div className="metric-card negative">
          <div className="label">Negative r</div>
          <div className="value">{stats.negative}</div>
          <div className="delta">anti-predictive</div>
        </div>
        <div className="metric-card">
          <div className="label">Insufficient data</div>
          <div className="value accent-muted">{stats.insufficient}</div>
          <div className="delta">need more trades</div>
        </div>
        <div className="metric-card">
          <div className="label">Top source share</div>
          <div className="value">{Math.round(stats.topShare * 100)}%</div>
          <div className="delta">of measurable r</div>
        </div>
        <div className="metric-card">
          <div className="label">Min-trades floor</div>
          <div className="value">{minTrades}</div>
          <div className="delta">tunable above</div>
        </div>
      </div>

      <div className="panel panel--data">
        <h3>Per-source correlation with realized P&L</h3>
        <div className="scroll" style={{ maxHeight: 600 }}>
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>r with P&L</th>
                <th className="num">Sample</th>
                <th className="num">Share</th>
                <th>Insight</th>
              </tr>
            </thead>
            <tbody>
              {sources.length ? sources.map((s) => (
                <tr key={s.source}>
                  <td>
                    <span className={SOURCE_PILL[s.source] || 'pill info'}>{s.source}</span>
                  </td>
                  <td>
                    <CorrelationBar value={s.correlation_with_pnl} />
                  </td>
                  <td className="num">{s.sample_size ?? '—'}</td>
                  <td className="num">
                    {s.contribution_share != null
                      ? `${Math.round(s.contribution_share * 100)}%`
                      : '—'}
                  </td>
                  <td style={{ color: 'var(--muted)', fontSize: 12 }}>
                    {s.insight || '—'}
                  </td>
                </tr>
              )) : (
                <tr><td colSpan={5}>
                  <div className="empty">
                    <div className="title">{loading ? 'Loading…' : 'No source data yet'}</div>
                    <div className="hint">Close some trades — the tracker needs realized P&L to compute correlations.</div>
                  </div>
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
