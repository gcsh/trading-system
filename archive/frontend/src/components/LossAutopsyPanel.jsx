import React, { useEffect, useState } from 'react';

/**
 * Loss Autopsy panel — shown inside TradeDetail for closed losing trades.
 * Fetches /autopsy/trade/{id} and renders the counterfactual bundle:
 *   • avoidable / mixed / variance tag chip
 *   • 5 flip hypotheses with FIRED/ok status
 *   • execution quality snapshot when available
 *
 * Stays silent (returns null) for profitable trades or when the backend
 * has no autopsy (legacy rows missing context).
 */
function TagChip({ tag, score }) {
  const color = tag === 'avoidable' ? 'var(--danger)'
              : tag === 'mixed' ? 'var(--warn, #d69e2e)'
              : 'var(--accent, #38a169)';
  const bg = tag === 'avoidable' ? 'var(--danger-soft)'
           : tag === 'mixed' ? 'rgba(214,158,46,0.18)'
           : 'var(--accent-soft)';
  return (
    <span style={{
      background: bg, color, border: `1px solid ${color}`,
      padding: '3px 10px', borderRadius: 4, fontWeight: 700,
      fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.06em',
    }}>
      {tag} · {(score * 100).toFixed(0)}%
    </span>
  );
}

function HypothesisRow({ h }) {
  const fired = h.triggered;
  const tone = fired ? 'var(--danger)' : 'var(--muted)';
  return (
    <li style={{ display: 'flex', gap: 10, alignItems: 'flex-start',
                  padding: '4px 0', borderBottom: '1px dashed var(--border)' }}>
      <span style={{
        color: tone, minWidth: 55,
        fontWeight: 700, fontSize: 11, textTransform: 'uppercase',
        letterSpacing: '0.06em', flexShrink: 0,
      }}>
        {fired ? '✗ fired' : '✓ ok'}
      </span>
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 600, fontSize: 12.5 }}>{h.name.replace(/_/g, ' ')}</div>
        <div style={{ color: 'var(--muted)', fontSize: 11.5, marginTop: 1 }}>{h.detail}</div>
      </div>
    </li>
  );
}

export default function LossAutopsyPanel({ tradeId, pnl }) {
  const [bundle, setBundle] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!tradeId || pnl == null || pnl >= 0) {
      setBundle(null);
      return;
    }
    let active = true;
    setLoading(true);
    fetch(`/autopsy/trade/${encodeURIComponent(tradeId)}`)
      .then((r) => {
        if (r.status === 404) return null;
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => { if (active) { setBundle(d); setError(null); } })
      .catch((e) => { if (active) setError(String(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [tradeId, pnl]);

  if (pnl == null || pnl >= 0) return null;
  if (loading) {
    return (
      <div className="panel" style={{ marginBottom: 14, background: 'var(--panel-2)' }}>
        <div className="panel-head"><h2 style={{ margin: 0, fontSize: 14 }}>🔍 Loss Autopsy</h2></div>
        <div style={{ color: 'var(--muted)', fontSize: 12 }}>analyzing...</div>
      </div>
    );
  }
  if (error || !bundle) return null;

  const fired = bundle.flip_hypotheses?.filter((h) => h.triggered) || [];

  return (
    <div className="panel" style={{
      marginBottom: 14, background: 'var(--panel-2)',
      borderColor: bundle.avoidable_tag === 'avoidable' ? 'var(--danger)' : undefined,
    }}>
      <div className="panel-head" style={{ display: 'flex',
            alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>🔍 Loss Autopsy</h2>
          <TagChip tag={bundle.avoidable_tag} score={bundle.avoidable_score} />
        </div>
        <span className="panel-sub">{fired.length} of {bundle.flip_hypotheses?.length || 0} gates would have caught this</span>
      </div>

      <div style={{ background: 'var(--bg-elev)', padding: '8px 10px',
                     borderRadius: 6, fontSize: 12.5, marginTop: 8,
                     color: 'var(--text-soft)' }}>
        {bundle.summary}
      </div>

      <div style={{ marginTop: 10 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)',
                       textTransform: 'uppercase', letterSpacing: '0.06em',
                       marginBottom: 6 }}>
          Counterfactual gates
        </div>
        <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {(bundle.flip_hypotheses || []).map((h, i) => (
            <HypothesisRow key={i} h={h} />
          ))}
        </ul>
      </div>

      {bundle.execution_quality && Object.keys(bundle.execution_quality).length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-soft)' }}>
          <strong style={{ color: 'var(--text)' }}>Execution:</strong>
          {' '}fill ${bundle.execution_quality.fill_price?.toFixed?.(2) || '—'} vs
          {' '}expected ${bundle.execution_quality.expected_price?.toFixed?.(2) || '—'}
          {bundle.execution_quality.slippage_bps != null && (
            <> · slippage {bundle.execution_quality.slippage_bps.toFixed(1)}bps</>
          )}
          {bundle.execution_quality.is_adverse && (
            <span style={{ color: 'var(--danger)' }}> · adverse fill</span>
          )}
        </div>
      )}
    </div>
  );
}
