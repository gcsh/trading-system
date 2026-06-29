/**
 * Stage-20-UI · Shadow Comparison.
 *
 * Side-by-side: legacy `recommendation` vs `chairman_report.decision`
 * across recent trades. Surfaces:
 *   • the divergence rate (how often Chairman would have changed
 *     the decision if it were authoritative)
 *   • per-trade divergence list with quick links to Mission Control
 *
 * This is the page that justifies (or doesn't) flipping the authority
 * flag. Before promotion, watch this page; once divergence is rare
 * and the differences look right, flip TB_CHAIRMAN_AUTHORITATIVE=true.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const RECO_LABEL = {
  execute: { className: 'decision-tag execute', text: 'EXECUTE' },
  size_down: { className: 'decision-tag size-down', text: 'SIZE_DOWN' },
  abstain: { className: 'decision-tag abstain', text: 'ABSTAIN' },
};

const CHAIRMAN_LABEL = {
  EXECUTE: { className: 'decision-tag execute', text: 'EXECUTE' },
  SIZE_DOWN: { className: 'decision-tag size-down', text: 'SIZE_DOWN' },
  MONITOR: { className: 'decision-tag monitor', text: 'MONITOR' },
  ABSTAIN: { className: 'decision-tag abstain', text: 'ABSTAIN' },
};

// Equivalence map — legacy recommendation maps onto a coarse Chairman
// decision so we can detect "would have changed the outcome".
const LEGACY_TO_CHAIRMAN = {
  execute: 'EXECUTE',
  size_down: 'SIZE_DOWN',
  abstain: 'ABSTAIN',
};

function divergence(legacy, chairman) {
  if (!chairman) return 'no_chairman';
  const mapped = LEGACY_TO_CHAIRMAN[legacy];
  if (!mapped) return 'unknown';
  if (mapped === chairman) return 'agree';
  // Severity: agree < soft_divergence (size_down vs execute) < hard_divergence (abstain vs execute)
  const order = ['ABSTAIN', 'MONITOR', 'SIZE_DOWN', 'EXECUTE'];
  const li = order.indexOf(mapped);
  const ci = order.indexOf(chairman);
  if (li === -1 || ci === -1) return 'soft_divergence';
  return Math.abs(li - ci) >= 2 ? 'hard_divergence' : 'soft_divergence';
}

const DIVERGENCE_PILL = {
  agree: 'pill on',
  soft_divergence: 'pill warn',
  hard_divergence: 'pill danger',
  no_chairman: 'pill off',
  unknown: 'pill off',
};

const DIVERGENCE_LABEL = {
  agree: 'agree',
  soft_divergence: 'soft Δ',
  hard_divergence: 'hard Δ',
  no_chairman: 'pre-20b',
  unknown: '—',
};

export default function ShadowComparison() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all'); // all | divergent | agree

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const trades = await api('/trades/list?limit=100');
      const results = await Promise.allSettled(
        trades.map((t) =>
          api(`/agents/consensus/${t.id}`).then((r) => ({ trade: t, consensus: r.consensus }))
        ),
      );
      const out = [];
      for (const r of results) {
        if (r.status !== 'fulfilled') continue;
        const { trade, consensus } = r.value;
        if (!consensus) continue;
        const chairman = consensus.chairman_report || {};
        const chairmanDecision = chairman.decision || null;
        out.push({
          trade_id: trade.id,
          ticker: trade.ticker,
          action: trade.action,
          timestamp: trade.timestamp,
          recommendation: consensus.recommendation,
          chairman_decision: chairmanDecision,
          chairman_reason: chairman.decision_reason,
          divergence: divergence(consensus.recommendation, chairmanDecision),
          conviction: chairman.conviction,
          size_mod: chairman.position_size_modifier,
          dissent_share: chairman.dissent?.dissent_share,
          quorum_met: consensus.quorum_met,
          authority: trade.consensus_authority || null,
        });
      }
      setRows(out);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const stats = useMemo(() => {
    const buckets = { agree: 0, soft_divergence: 0, hard_divergence: 0, no_chairman: 0 };
    for (const r of rows) buckets[r.divergence] = (buckets[r.divergence] || 0) + 1;
    const withChairman = rows.length - buckets.no_chairman;
    const divergent = buckets.soft_divergence + buckets.hard_divergence;
    return {
      total: rows.length,
      withChairman,
      ...buckets,
      divergent,
      divergence_rate: withChairman ? divergent / withChairman : 0,
      agreement_rate: withChairman ? buckets.agree / withChairman : 0,
    };
  }, [rows]);

  const filtered = useMemo(() => {
    if (filter === 'divergent') return rows.filter((r) => r.divergence === 'soft_divergence' || r.divergence === 'hard_divergence');
    if (filter === 'agree') return rows.filter((r) => r.divergence === 'agree');
    return rows;
  }, [rows, filter]);

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
            <div className="accent-governance" style={{
              fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
              fontWeight: 600, marginBottom: 6,
            }}>Stage 20b · Shadow Run</div>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              Chairman vs Legacy — would promotion change outcomes?
            </h2>
            <div style={{ color: 'var(--muted)', marginTop: 8, fontSize: 13, maxWidth: 720 }}>
              By default the engine consumes the legacy aggregate <code>recommendation</code>.
              The Chairman computes <code>decision</code> in shadow — this page shows where they
              agree and where they diverge. Flip <code>TB_CHAIRMAN_AUTHORITATIVE=true</code> when
              you're satisfied with what you see here.
            </div>
          </div>
          <button className="btn small" onClick={load} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="metric-strip">
        <div className="metric-card positive">
          <div className="label">Agreement</div>
          <div className="value">{Math.round(stats.agreement_rate * 100)}%</div>
          <div className="delta">{stats.agree} of {stats.withChairman} trades</div>
        </div>
        <div className="metric-card">
          <div className="label">Soft Δ</div>
          <div className="value accent-warn">{stats.soft_divergence}</div>
          <div className="delta">execute ↔ size_down etc.</div>
        </div>
        <div className="metric-card negative">
          <div className="label">Hard Δ</div>
          <div className="value">{stats.hard_divergence}</div>
          <div className="delta">execute ↔ abstain</div>
        </div>
        <div className="metric-card">
          <div className="label">Sample size</div>
          <div className="value">{stats.withChairman}</div>
          <div className="delta">last {stats.total} trades</div>
        </div>
        <div className="metric-card">
          <div className="label">Pre-Stage-20b</div>
          <div className="value accent-muted">{stats.no_chairman}</div>
          <div className="delta">no shadow data</div>
        </div>
        <div className="metric-card">
          <div className="label">Divergence rate</div>
          <div className="value">{Math.round(stats.divergence_rate * 100)}%</div>
          <div className="delta">across structured trades</div>
        </div>
      </div>

      <div className="panel panel--governance">
        <div className="panel-head">
          <h2>Trade-by-trade divergence</h2>
          <div className="row" style={{ gap: 4 }}>
            {['all', 'divergent', 'agree'].map((k) => (
              <button
                key={k}
                className={`btn small ${filter === k ? 'primary' : 'ghost'}`}
                onClick={() => setFilter(k)}
              >
                {k}
              </button>
            ))}
          </div>
        </div>

        {!filtered.length ? (
          <div className="empty">
            <div className="title">{loading ? 'Loading…' : 'No structured consensus persisted yet'}</div>
            <div className="hint">
              Run a few cycles with Stage 20a/b live. Trades persisted before Stage 20b show as <em>pre-20b</em>.
            </div>
          </div>
        ) : (
          <div className="scroll" style={{ maxHeight: 540 }}>
            <table>
              <thead>
                <tr>
                  <th>Trade</th>
                  <th>Ticker</th>
                  <th>Legacy</th>
                  <th>Chairman</th>
                  <th>Δ</th>
                  <th>Why</th>
                  <th className="num">Conviction</th>
                  <th className="num">Size ×</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => {
                  const legacyTag = RECO_LABEL[r.recommendation] || { className: 'decision-tag abstain', text: r.recommendation || '—' };
                  const chTag = r.chairman_decision
                    ? CHAIRMAN_LABEL[r.chairman_decision] || { className: 'decision-tag abstain', text: r.chairman_decision }
                    : null;
                  return (
                    <tr key={r.trade_id}>
                      <td><Link to={`/mission-control?id=${r.trade_id}`}>#{r.trade_id}</Link></td>
                      <td><strong>{r.ticker}</strong></td>
                      <td><span className={legacyTag.className}>{legacyTag.text}</span></td>
                      <td>
                        {chTag ? <span className={chTag.className}>{chTag.text}</span>
                          : <span className="pill off">pre-20b</span>}
                      </td>
                      <td><span className={DIVERGENCE_PILL[r.divergence]}>{DIVERGENCE_LABEL[r.divergence]}</span></td>
                      <td style={{ fontSize: 11, color: 'var(--muted)' }}>
                        {(r.chairman_reason || '').replace(/_/g, ' ')}
                      </td>
                      <td className="num">
                        {r.conviction != null ? `${Math.round(r.conviction * 100)}%` : '—'}
                      </td>
                      <td className="num">
                        {r.size_mod != null ? Number(r.size_mod).toFixed(2) : '—'}
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
