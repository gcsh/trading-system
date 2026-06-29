/* MITS Phase 19 Cluster D — StrategyTemplateCard.
 *
 * Renders one strategy template (from /strategies/catalog) as a compact
 * card. When a candidate (from /strategy/matrix/{ticker}) is supplied,
 * also shows fit / cohort / rank for the currently-selected ticker.
 *
 * Props:
 *   tmpl:      { slug, label, description, category }
 *   candidate: { strategy_name, fit_score, cohort_win_rate, cohort_n,
 *                ranked_position, final_score } or null
 *   selected:  bool — highlight ring
 *   onClick:   fn(slug)
 */
import React from 'react';
import { Pill } from '../../design/Components.jsx';

const CATEGORY_TONE = {
  stock:            'info',
  defined_risk:     'success',
  long_option:      'info',
  complex:          'warning',
  premium_selling:  'success',
};

function fmtPct(v) {
  if (v == null || !isFinite(v)) return '—';
  return `${(Number(v) * 100).toFixed(0)}%`;
}

export default function StrategyTemplateCard({
  tmpl, candidate, selected = false, onClick,
}) {
  if (!tmpl) return null;
  const tone = CATEGORY_TONE[tmpl.category] || 'neutral';
  const rank = candidate?.ranked_position;
  const fit = candidate?.fit_score;
  const winRate = candidate?.cohort_win_rate;
  const n = candidate?.cohort_n;

  return (
    <button type="button"
            className={`v2-tmpl ${selected ? 'v2-tmpl--selected' : ''}`}
            onClick={() => onClick && onClick(tmpl.slug)}
            aria-pressed={selected}>
      <div className="v2-tmpl__head">
        <span className="v2-tmpl__label">{tmpl.label}</span>
        {rank != null && (
          <span className="v2-tmpl__rank mono" title="Rank for current ticker">
            #{rank}
          </span>
        )}
      </div>
      <div className="v2-tmpl__cat">
        <Pill tone={tone} size="sm">{(tmpl.category || 'other').replace('_', ' ')}</Pill>
      </div>
      <div className="v2-tmpl__desc">{tmpl.description || ''}</div>
      {candidate && (
        <div className="v2-tmpl__metrics">
          <div className="v2-tmpl__metric">
            <div className="v2-tmpl__metric-lbl">Fit</div>
            <div className="v2-tmpl__metric-val mono">{fmtPct(fit)}</div>
            <div className="v2-tmpl__bar">
              <div className="v2-tmpl__bar-fill"
                   style={{
                     width: `${Math.max(0, Math.min(100, (fit || 0) * 100))}%`,
                     background: 'var(--accent-cyan)',
                   }} />
            </div>
          </div>
          <div className="v2-tmpl__metric">
            <div className="v2-tmpl__metric-lbl">Cohort Win</div>
            <div className="v2-tmpl__metric-val mono">{fmtPct(winRate)}</div>
            <div className="v2-tmpl__bar">
              <div className="v2-tmpl__bar-fill"
                   style={{
                     width: `${Math.max(0, Math.min(100, (winRate || 0) * 100))}%`,
                     background: 'var(--accent-green)',
                   }} />
            </div>
          </div>
          <div className="v2-tmpl__n mono">n={n != null ? n.toLocaleString() : '—'}</div>
        </div>
      )}
      <style>{`
        .v2-tmpl {
          text-align: left;
          width: 100%;
          background: var(--bg-tertiary);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-md);
          padding: 12px;
          color: var(--text-primary);
          cursor: pointer;
          display: flex;
          flex-direction: column;
          gap: 6px;
          transition: border-color var(--transition-fast), background var(--transition-fast);
          font-family: var(--font-display);
        }
        .v2-tmpl:hover {
          background: var(--bg-elevated);
          border-color: var(--border-default);
        }
        .v2-tmpl--selected {
          border-color: var(--accent-cyan);
          box-shadow: var(--shadow-glow-cyan);
        }
        .v2-tmpl__head {
          display: flex; align-items: center; justify-content: space-between;
          gap: 8px;
        }
        .v2-tmpl__label {
          font-weight: 700; font-size: var(--font-size-sm);
        }
        .v2-tmpl__rank {
          font-size: 11px;
          color: var(--accent-cyan);
          font-weight: 700;
        }
        .v2-tmpl__desc {
          font-size: 11px;
          color: var(--text-tertiary);
          line-height: 1.4;
        }
        .v2-tmpl__metrics {
          display: flex; flex-direction: column; gap: 6px;
          padding-top: 6px;
          border-top: 1px dashed var(--border-subtle);
        }
        .v2-tmpl__metric { display: grid; grid-template-columns: 70px 40px 1fr; align-items: center; gap: 6px; }
        .v2-tmpl__metric-lbl { font-size: 10px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.04em; }
        .v2-tmpl__metric-val { font-size: 11px; color: var(--text-primary); }
        .v2-tmpl__bar { height: 4px; background: var(--bg-primary); border-radius: 2px; overflow: hidden; }
        .v2-tmpl__bar-fill { height: 100%; }
        .v2-tmpl__n {
          font-size: 10px;
          color: var(--text-tertiary);
          text-align: right;
        }
      `}</style>
    </button>
  );
}
