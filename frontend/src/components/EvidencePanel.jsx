import React from 'react';
import { useEvidence } from '../hooks/useKnowledge.js';

/**
 * MITS Phase 1 — inline evidence panel.
 * MITS Phase 2 (P2.5) — module-cached lookup so multiple mounts on the
 *   same page produce ONE network call. Routes through `useEvidence`
 *   in `hooks/useKnowledge.js`.
 *
 * Two modes:
 *
 *  1. (ticker, pattern) given — renders the most populated knowledge-graph
 *     cell for that pair as a one-liner:
 *
 *       "Knowledge says: 347 historical analogs, win rate 71%
 *        (posterior 68%), avg move +2.9% at 1d horizon."
 *
 *  2. (ticker) only — renders the top-N populated cells for the ticker
 *     across patterns.
 *
 * Renders nothing if no cell exists yet (cold corpus).
 */
export default function EvidencePanel({
  ticker, pattern, horizon = '1d', topN = 3,
}) {
  const { cells, primary } = useEvidence(ticker, pattern, horizon, topN);

  if (!ticker) return null;

  // Pattern-mode render: single cell row.
  if (pattern && primary) {
    const cell = primary;
    const wr = cell.win_rate != null ? (cell.win_rate * 100).toFixed(0) : '-';
    const post = cell.posterior_win_rate != null
      ? (cell.posterior_win_rate * 100).toFixed(0) : '-';
    const avg = cell.avg_return_pct != null
      ? `${(cell.avg_return_pct * 100).toFixed(1)}%` : '-';
    const lo = cell.confidence_lower != null
      ? (cell.confidence_lower * 100).toFixed(0) : '-';
    const hi = cell.confidence_upper != null
      ? (cell.confidence_upper * 100).toFixed(0) : '-';
    // MITS Phase 6 — when we have separate live (out_of_sample) and
    // historical (in_sample) rows for the same cohort, render the
    // source breakdown so the operator can see which one is steering
    // the combined posterior.
    const cohortMatches = (a, b) => a && b
      && a.pattern === b.pattern
      && a.ticker === b.ticker
      && a.regime === b.regime
      && a.vol_state === b.vol_state
      && a.time_bucket === b.time_bucket
      && a.horizon === b.horizon;
    const live = cells.find(
      (c) => c.sample_split === 'out_of_sample' && cohortMatches(c, primary)
    );
    const hist = cells.find(
      (c) => c.sample_split === 'in_sample' && cohortMatches(c, primary)
    );
    const combined = cells.find(
      (c) => c.sample_split === 'combined' && cohortMatches(c, primary)
    );
    return (
      <div className="panel panel--intel" style={{ padding: 10, fontSize: 12 }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'baseline',
                            flexWrap: 'wrap' }}>
          <strong style={{ fontSize: 11, letterSpacing: '0.04em',
                                    textTransform: 'uppercase', color: 'var(--muted)' }}>
            Knowledge says
          </strong>
          <span>{cell.sample_size} historical analog{cell.sample_size === 1 ? '' : 's'}</span>
          <span>· win rate <strong>{wr}%</strong></span>
          <span>(posterior {post}%)</span>
          <span>· avg move <strong>{avg}</strong></span>
          <span style={{ color: 'var(--muted)' }}>at {cell.horizon}</span>
          <span style={{ color: 'var(--muted)', fontSize: 11 }}>
            CI [{lo}%, {hi}%]
          </span>
        </div>
        {(live || hist) && (
          <div style={{ marginTop: 6, display: 'grid', gap: 2,
                              fontSize: 11, color: 'var(--text-soft)' }}>
            {live && live.sample_size > 0 && (
              <div>
                <span className="pill on" style={{ fontSize: 10, marginRight: 6 }}>live</span>
                {live.sample_size} trade{live.sample_size === 1 ? '' : 's'}
                {' · '}
                {(live.win_rate != null ? (live.win_rate * 100).toFixed(0) : '-')}% WR
                {' · '}
                {(live.posterior_win_rate != null ? live.posterior_win_rate.toFixed(2) : '-')} posterior
              </div>
            )}
            {hist && hist.sample_size > 0 && (
              <div>
                <span className="pill info" style={{ fontSize: 10, marginRight: 6 }}>historical</span>
                {hist.sample_size} obs
                {' · '}
                {(hist.win_rate != null ? (hist.win_rate * 100).toFixed(0) : '-')}% WR
                {' · '}
                {(hist.posterior_win_rate != null ? hist.posterior_win_rate.toFixed(2) : '-')} posterior
              </div>
            )}
            {combined && combined.posterior_win_rate != null && (
              <div style={{ color: 'var(--accent-2)' }}>
                <span className="pill purple" style={{ fontSize: 10, marginRight: 6 }}>combined</span>
                {combined.posterior_win_rate.toFixed(2)} posterior (live-weighted)
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // No-pattern mode: top-N cells for the ticker.
  if (!pattern && cells.length > 0) {
    return (
      <div className="panel panel--intel" style={{ padding: 10, fontSize: 12 }}>
        <div style={{ fontSize: 11, letterSpacing: '0.04em',
                              textTransform: 'uppercase', color: 'var(--muted)',
                              marginBottom: 6 }}>
          Knowledge says (top {cells.length} pattern{cells.length === 1 ? '' : 's'} for {ticker})
        </div>
        <div style={{ display: 'grid', gap: 4 }}>
          {cells.map((c) => {
            const wr = c.win_rate != null ? `${(c.win_rate * 100).toFixed(0)}%` : '-';
            const post = c.posterior_win_rate != null
              ? `${(c.posterior_win_rate * 100).toFixed(0)}%` : '-';
            const avg = c.avg_return_pct != null
              ? `${(c.avg_return_pct * 100).toFixed(1)}%` : '-';
            return (
              <div key={`${c.pattern}-${c.regime}-${c.vol_state}-${c.time_bucket}-${c.horizon}`}
                       style={{ display: 'flex', gap: 8, flexWrap: 'wrap',
                                       alignItems: 'baseline' }}>
                <strong style={{ minWidth: 100 }}>{c.pattern}</strong>
                <span style={{ color: 'var(--muted)' }}>{c.regime} · {c.vol_state}</span>
                <span>N={c.sample_size}</span>
                <span>WR <strong>{wr}</strong> (post {post})</span>
                <span style={{ color: 'var(--muted)' }}>avg {avg} at {c.horizon}</span>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return null;
}
