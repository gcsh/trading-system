/* MITS Phase 19 Stream 0 — placeholder for v2 child routes.
 * Stream 1/2/3 will replace these with real implementations. */
import React from 'react';
import { useLocation, Link } from 'react-router-dom';
import { Card, EmptyState, Pill } from '../design/Components.jsx';

export default function V2Placeholder() {
  const loc = useLocation();
  return (
    <Card>
      <EmptyState
        icon="◌"
        message={`${loc.pathname} is reserved — Stream 1/2/3 will wire the real page here.`}
        action={
          <div style={{ display: 'flex', gap: 'var(--space-3)', justifyContent: 'center', alignItems: 'center' }}>
            <Pill tone="info" size="md">FOUNDATION ONLY</Pill>
            <Link to="/v2/" style={{ color: 'var(--accent-cyan)' }}>← Back to v2 home</Link>
          </div>
        }
      />
    </Card>
  );
}
