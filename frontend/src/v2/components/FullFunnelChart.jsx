/* MITS Phase 19 Cluster C — FullFunnelChart.
 *
 * Vertical conversion funnel. One row per stage:
 *
 *   [stage name | n_passed bar (full-width, scaled) | n_dropped sliver | pass_rate%]
 *
 * Underneath the bar, top-3 drop reasons render as a thin stacked
 * accent strip — so the operator instantly sees which rule killed
 * the throughput at each stage without expanding anything.
 *
 * Hovering a stage row highlights it. Clicking bubbles the stage
 * up so a side panel can show full drop reasons + counterfactual.
 */
import React, { useMemo } from 'react';
import { EmptyState } from '../../design/Components.jsx';

const REASON_COLORS = [
  'var(--accent-red)',
  'var(--accent-yellow)',
  'var(--accent-purple)',
];

function fmtN(v) {
  if (v == null || !isFinite(v)) return '0';
  return Number(v).toLocaleString();
}

export default function FullFunnelChart({ stages = [], onSelect }) {
  if (!stages || stages.length === 0) {
    return <EmptyState icon="∅" message="No funnel snapshot yet — run /learning/funnel writer." />;
  }

  // Use the first stage's n_decisions as the funnel's denominator for
  // visual scaling — every bar is a fraction of "everything we evaluated".
  const denom = useMemo(() => {
    const top = stages[0]?.n_decisions || 0;
    return Math.max(top, 1);
  }, [stages]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {stages.map((s, idx) => {
        const passRatePct = (s.pass_rate ?? 0) * 100;
        const widthPct = Math.max(0.5, ((s.n_passed || 0) / denom) * 100);
        const totalReasonN = (s.top_3_drop_reasons || [])
          .reduce((acc, r) => acc + (r.n || 0), 0);
        return (
          <div
            key={s.name + idx}
            onClick={() => onSelect && onSelect(s)}
            className="v2-card"
            style={{
              cursor: 'pointer',
              padding: '10px 14px',
              display: 'grid',
              gridTemplateColumns: 'minmax(180px,220px) 1fr 110px',
              gap: 12,
              alignItems: 'center',
            }}
          >
            <div>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-primary)' }}>
                {idx + 1}. {s.name.replace(/_/g, ' ')}
              </div>
              <div className="mono" style={{
                fontSize: 10.5, color: 'var(--text-tertiary)', marginTop: 2,
              }}>
                in {fmtN(s.n_decisions)} → out {fmtN(s.n_passed)}
              </div>
              {s.note && (
                <div style={{
                  fontSize: 10, color: 'var(--text-muted)', marginTop: 2,
                  fontStyle: 'italic',
                }}>{s.note.slice(0, 60)}{s.note.length > 60 ? '…' : ''}</div>
              )}
            </div>

            <div>
              <div style={{
                position: 'relative',
                height: 22,
                background: 'var(--bg-secondary)',
                borderRadius: 4,
                overflow: 'hidden',
                border: '1px solid var(--border-subtle)',
              }}>
                <div style={{
                  position: 'absolute',
                  left: 0, top: 0, bottom: 0,
                  width: `${widthPct}%`,
                  background: 'linear-gradient(90deg, var(--accent-cyan-dim), var(--accent-green-dim))',
                  display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
                  paddingRight: 6,
                  fontSize: 10.5,
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--bg-primary)',
                  fontWeight: 700,
                  borderRadius: 3,
                }}>
                  {widthPct >= 6 ? fmtN(s.n_passed) : ''}
                </div>
              </div>

              {/* Top-3 drop reasons stacked strip */}
              {totalReasonN > 0 && (
                <div style={{
                  display: 'flex',
                  height: 6,
                  marginTop: 4,
                  borderRadius: 3,
                  overflow: 'hidden',
                  background: 'var(--bg-secondary)',
                }}>
                  {(s.top_3_drop_reasons || []).slice(0, 3).map((r, ri) => {
                    const w = ((r.n || 0) / totalReasonN) * 100;
                    return (
                      <div
                        key={r.rule + ri}
                        title={`${r.rule}: ${fmtN(r.n)}`}
                        style={{
                          width: `${w}%`,
                          background: REASON_COLORS[ri] || 'var(--text-muted)',
                        }}
                      />
                    );
                  })}
                </div>
              )}
              {totalReasonN > 0 && (
                <div style={{
                  display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 4,
                  fontSize: 10, color: 'var(--text-tertiary)',
                }}>
                  {(s.top_3_drop_reasons || []).slice(0, 3).map((r, ri) => (
                    <span key={r.rule + ri}>
                      <span style={{
                        display: 'inline-block', width: 8, height: 8,
                        background: REASON_COLORS[ri] || 'var(--text-muted)',
                        borderRadius: 2, marginRight: 4,
                        verticalAlign: 'middle',
                      }}/>
                      <span className="mono">{r.rule}</span>{' '}
                      <span style={{ color: 'var(--text-muted)' }}>· {fmtN(r.n)}</span>
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div style={{ textAlign: 'right' }}>
              <div className="mono" style={{
                fontSize: 18, fontWeight: 700,
                color: passRatePct >= 50 ? 'var(--accent-green)'
                       : passRatePct >= 1 ? 'var(--accent-yellow)'
                       : 'var(--accent-red)',
              }}>
                {passRatePct.toFixed(passRatePct < 1 ? 2 : 1)}%
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>
                pass rate
              </div>
              {s.n_dropped > 0 && (
                <div style={{ fontSize: 10, color: 'var(--accent-red)', marginTop: 2 }}>
                  -{fmtN(s.n_dropped)} dropped
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
