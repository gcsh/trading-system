/**
 * MITS Phase 9.2 — Knowledge page wrapper.
 *
 * Adds a tab strip at the top:
 *
 *   [Theory Studio] [Knowledge Graph (existing)]
 *
 * Theory Studio is the new editable chart. Knowledge Graph is the
 * existing pattern-cohort matrix preserved as-is.
 */
import React, { useState } from 'react';
import KnowledgeGraph from './KnowledgeGraph.jsx';
import TheoryStudio from './TheoryStudio.jsx';

const TABS = [
  { id: 'studio',  label: 'Theory Studio' },
  { id: 'graph',   label: 'Knowledge Graph' },
];

export default function Knowledge() {
  const [tab, setTab] = useState('studio');
  return (
    <div>
      <div className="row" style={{ gap: 6, marginBottom: 10 }}>
        {TABS.map((t) => (
          <button key={t.id}
                  className={`btn small ${tab === t.id ? 'primary' : ''}`}
                  onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'studio' && <TheoryStudio />}
      {tab === 'graph'  && <KnowledgeGraph />}
    </div>
  );
}
