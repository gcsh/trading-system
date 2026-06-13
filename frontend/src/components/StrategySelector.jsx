import React from 'react';
import { useStrategies } from '../hooks/useStrategies.js';

// "adaptive" is a meta-strategy (the regime-aware selector). It's not in
// the registry — it's the engine's default when no specific strategy is
// pinned. Prepend it here so the operator can choose it from the picker.
const ADAPTIVE_ENTRY = {
  slug: 'adaptive',
  label: 'Adaptive',
  description: 'Picks the best registered strategy based on regime',
  category: 'meta',
};

export default function StrategySelector({ value, onChange, onTest }) {
  const strategies = useStrategies();
  const items = [ADAPTIVE_ENTRY, ...strategies];

  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Strategy</h2>
        <span className="panel-sub">click to apply · "test" simulates</span>
      </div>
      <div className="strategy-cards">
        {items.map(({ slug, label, description }) => (
          <div
            key={slug}
            className={`strategy-card ${value === slug ? 'active' : ''}`}
            onClick={() => onChange(slug)}
          >
            <div className="name">{label}</div>
            <div className="desc">{description}</div>
            {onTest && slug !== 'adaptive' && (
              <div style={{ marginTop: 8, display: 'flex', justifyContent: 'flex-end' }}>
                <button
                  className="btn small ghost"
                  onClick={(e) => {
                    e.stopPropagation();
                    onTest(slug);
                  }}
                >
                  Test →
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
