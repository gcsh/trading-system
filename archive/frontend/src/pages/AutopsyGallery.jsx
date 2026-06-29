/**
 * Stage-20-UI · Autopsy Gallery.
 *
 * List view of Stage-9 loss autopsies — each closed losing trade gets
 * an automated postmortem with 5 flip hypotheses:
 *   • event_hold        — held into earnings / macro print
 *   • abstain_band      — borderline grade we should have passed on
 *   • spread_too_wide   — execution cost ate the edge
 *   • low_grade         — grade was below the dynamic threshold
 *   • kelly_oversize    — sized too aggressively relative to win prob
 *
 * Each autopsy gets a verdict: avoidable / mixed / variance — telling
 * you whether the loss was a process failure or a real-world outcome.
 *
 * Backend: /autopsy/recent (list), /autopsy/trade/{id} (detail).
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const VERDICT_TAG = {
  avoidable: { className: 'decision-tag execute', text: 'AVOIDABLE' },
  mixed: { className: 'decision-tag size-down', text: 'MIXED' },
  variance: { className: 'decision-tag abstain', text: 'VARIANCE' },
};

const HYPOTHESIS_PILL = {
  event_hold: { className: 'pill warn', label: 'held into event' },
  abstain_band: { className: 'pill heat', label: 'should have abstained' },
  spread_too_wide: { className: 'pill heat', label: 'spread ate edge' },
  low_grade: { className: 'pill purple', label: 'low grade' },
  kelly_oversize: { className: 'pill danger', label: 'oversized' },
};

function HypothesisChips({ fired }) {
  if (!fired?.length) {
    return <span className="accent-muted" style={{ fontSize: 12 }}>—</span>;
  }
  return (
    <div className="row" style={{ gap: 6 }}>
      {fired.map((h) => {
        const cfg = HYPOTHESIS_PILL[h] || { className: 'pill info', label: h };
        return <span key={h} className={cfg.className}>{cfg.label}</span>;
      })}
    </div>
  );
}

export default function AutopsyGallery() {
  const [autopsies, setAutopsies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all'); // all | avoidable | mixed | variance

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api('/autopsy/recent?limit=60');
      setAutopsies(r.autopsies || r || []);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const stats = useMemo(() => {
    const buckets = { avoidable: 0, mixed: 0, variance: 0 };
    let totalLoss = 0;
    for (const a of autopsies) {
      const v = (a.verdict || '').toLowerCase();
      if (buckets[v] != null) buckets[v] += 1;
      totalLoss += Math.abs(Number(a.pnl) || 0);
    }
    return { ...buckets, total: autopsies.length, totalLoss };
  }, [autopsies]);

  const filtered = useMemo(() => {
    if (filter === 'all') return autopsies;
    return autopsies.filter((a) => (a.verdict || '').toLowerCase() === filter);
  }, [autopsies, filter]);

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
            <div className="accent-risk" style={{
              fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
              fontWeight: 600, marginBottom: 6,
            }}>Stage 9 · Loss Autopsy</div>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              Which losses were process failures and which were just variance?
            </h2>
            <div style={{ color: 'var(--muted)', marginTop: 8, fontSize: 13, maxWidth: 720 }}>
              Each losing trade is replayed against 5 flip hypotheses to see whether a
              different policy choice would have avoided the loss. <strong className="accent-bear">Avoidable</strong>{' '}
              losses are the ones that should drive a config change; <strong className="accent-muted">variance</strong>{' '}
              losses are the cost of having a positive-edge system.
            </div>
          </div>
          <button className="btn small" onClick={load} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="metric-strip">
        <div className="metric-card negative">
          <div className="label">Total autopsies</div>
          <div className="value">{stats.total}</div>
          <div className="delta">closed losers</div>
        </div>
        <div className="metric-card">
          <div className="label">Avoidable</div>
          <div className="value accent-markets">{stats.avoidable}</div>
          <div className="delta">{stats.total ? Math.round(stats.avoidable / stats.total * 100) : 0}% of losses</div>
        </div>
        <div className="metric-card">
          <div className="label">Mixed</div>
          <div className="value accent-warn">{stats.mixed}</div>
          <div className="delta">policy contributed</div>
        </div>
        <div className="metric-card">
          <div className="label">Variance</div>
          <div className="value accent-muted">{stats.variance}</div>
          <div className="delta">expected losses</div>
        </div>
        <div className="metric-card">
          <div className="label">Total $ lost</div>
          <div className="value accent-bear">${stats.totalLoss.toFixed(0)}</div>
          <div className="delta">summed |pnl|</div>
        </div>
        <div className="metric-card">
          <div className="label">Filter</div>
          <div style={{ marginTop: 6 }}>
            <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ padding: '4px 24px 4px 8px', fontSize: 12 }}>
              <option value="all">all</option>
              <option value="avoidable">avoidable</option>
              <option value="mixed">mixed</option>
              <option value="variance">variance</option>
            </select>
          </div>
        </div>
      </div>

      <div className="panel panel--risk">
        <h3>Recent autopsies</h3>
        {!filtered.length ? (
          <div className="empty">
            <div className="title">{loading ? 'Loading…' : 'No autopsies yet'}</div>
            <div className="hint">Close some losing trades and Stage-9 will produce postmortems.</div>
          </div>
        ) : (
          <div className="scroll" style={{ maxHeight: 620 }}>
            <table>
              <thead>
                <tr>
                  <th>Trade</th>
                  <th>Ticker</th>
                  <th className="num">P&L</th>
                  <th>Verdict</th>
                  <th className="num">Confidence</th>
                  <th>Hypotheses fired</th>
                  <th>Narrative</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((a) => {
                  const v = (a.verdict || 'variance').toLowerCase();
                  const tag = VERDICT_TAG[v] || VERDICT_TAG.variance;
                  return (
                    <tr key={a.trade_id}>
                      <td>
                        <Link to={`/mission-control?id=${a.trade_id}`}>#{a.trade_id}</Link>
                      </td>
                      <td><strong>{a.ticker}</strong></td>
                      <td className="num neg">
                        ${Number(a.pnl ?? 0).toFixed(2)}
                      </td>
                      <td><span className={tag.className}>{tag.text}</span></td>
                      <td className="num">
                        {a.confidence != null ? `${Math.round(a.confidence * 100)}%` : '—'}
                      </td>
                      <td><HypothesisChips fired={a.hypotheses_fired || a.fired || []} /></td>
                      <td style={{ color: 'var(--muted)', fontSize: 12, maxWidth: 360 }}>
                        {a.narrative || a.summary || '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
