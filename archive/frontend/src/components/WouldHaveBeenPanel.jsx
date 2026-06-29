/**
 * Feature-Merge F3 — WouldHaveBeenPanel.
 *
 * Surfaces `/decision/cockpit/{id}.would_have_been` on non-submitted
 * decisions (hold / abstain / decision_stale / blocked / etc.) so the
 * operator can read the four execution provenance strings that WOULD
 * have been written if the decision had been executed:
 *
 *   • fill_snapshot      — projected fill price + spread + slippage
 *   • sizing_chain       — projected qty after sizing chain
 *   • chain_selection    — projected option contract (if applicable)
 *   • exit_policy_result — projected first-fire exit trigger
 *
 * Backend persists these as plain-English text strings on every
 * non-submitted DecisionProvenance row (BotEngine._compute_would_have_been).
 * Legacy rows persisted before Phase 19 ships have NULL — UI falls back
 * to the EmptyState in that case.
 *
 * On `event_status === 'submitted'` we render a small affordance
 * pointing the operator at the live execution panels below (which
 * carry the REAL fill / sizing / chain / exit data, not the hypothesized
 * version).
 *
 * Styling matches the original DecisionCockpit.jsx Panel/PanelHeader/Pill
 * chrome (locally re-declared to avoid touching DecisionCockpit's
 * exports).
 */
import React from 'react';

// ── tiny chrome primitives matching DecisionCockpit.jsx ──────────────

function Pill({ tone = 'info', children, title }) {
  const palette = {
    on: { bg: '#064e3b', fg: '#6ee7b7', border: '#10b981' },
    off: { bg: '#1f2937', fg: '#9ca3af', border: '#374151' },
    info: { bg: '#1e3a8a', fg: '#93c5fd', border: '#3b82f6' },
    warn: { bg: '#78350f', fg: '#fcd34d', border: '#f59e0b' },
    danger: { bg: '#7f1d1d', fg: '#fca5a5', border: '#ef4444' },
    purple: { bg: '#4c1d95', fg: '#c4b5fd', border: '#8b5cf6' },
  };
  const c = palette[tone] || palette.info;
  return (
    <span title={title || undefined} style={{
      display: 'inline-block', padding: '2px 8px', borderRadius: 12,
      fontSize: 11, fontWeight: 600,
      background: c.bg, color: c.fg, border: `1px solid ${c.border}`,
      marginRight: 4,
    }}>{children}</span>
  );
}

function Panel({ children }) {
  return (
    <div style={{
      background: '#111827', borderRadius: 8, padding: 16,
      border: '1px solid #1f2937', marginBottom: 16,
    }}>{children}</div>
  );
}

function PanelHeader({ icon, title, right }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between',
      alignItems: 'center', marginBottom: 12,
    }}>
      <h3 style={{ margin: 0, fontSize: 16, color: '#e5e7eb' }}>
        {icon} {title}
      </h3>
      <div>{right}</div>
    </div>
  );
}

// ── row primitive ────────────────────────────────────────────────────

function ProjectionRow({ label, text, tip }) {
  const filled = text != null && String(text).trim() !== '';
  return (
    <div style={{
      padding: '10px 12px', background: '#0a0a0a',
      border: '1px solid #1f2937', borderRadius: 6,
      marginBottom: 8,
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'baseline', marginBottom: 4,
      }}>
        <div style={{
          fontSize: 11, color: '#93c5fd', fontWeight: 600,
          textTransform: 'uppercase', letterSpacing: '0.05em',
        }} title={tip}>
          {label}
        </div>
        {!filled && <Pill tone="off">not projected</Pill>}
      </div>
      <div style={{
        fontSize: 13, color: filled ? '#e5e7eb' : '#6b7280',
        lineHeight: 1.55,
      }}>
        {filled ? String(text) : '—'}
      </div>
    </div>
  );
}

// ── public component ─────────────────────────────────────────────────

const SUBMITTED_STATUSES = new Set(['submitted', 'executed', 'filled']);

export default function WouldHaveBeenPanel({ eventStatus, wouldHaveBeen }) {
  const isSubmitted = SUBMITTED_STATUSES.has(
    String(eventStatus || '').toLowerCase()
  );

  // Submitted → operator should look at the REAL execution panels.
  if (isSubmitted) {
    return (
      <Panel>
        <PanelHeader
          icon="(Q)"
          title='"Would have been" projection'
          right={<Pill tone="on">trade was executed</Pill>}
        />
        <div style={{
          color: '#9ca3af', fontSize: 13, lineHeight: 1.55,
        }}>
          Trade was executed — the projection panel only fires on
          non-submitted decisions (HOLD / ABSTAIN / blocked). See the
          live Fill snapshot / Sizing chain / Chain selection / Exit
          policy panels below for the actual execution provenance.
        </div>
      </Panel>
    );
  }

  // Non-submitted but no projection persisted.
  if (!wouldHaveBeen || typeof wouldHaveBeen !== 'object') {
    return (
      <Panel>
        <PanelHeader
          icon="(Q)"
          title='"Would have been" projection'
          right={<Pill tone="off">no projection yet</Pill>}
        />
        <div style={{
          color: '#9ca3af', fontSize: 13, lineHeight: 1.55,
        }}>
          No projection persisted for this decision. Likely a legacy
          row written before Phase 19's <code>_compute_would_have_been</code>
          hook shipped — new HOLDs will populate automatically.
        </div>
      </Panel>
    );
  }

  const fill = wouldHaveBeen.fill_snapshot;
  const sizing = wouldHaveBeen.sizing_chain;
  const chain = wouldHaveBeen.chain_selection;
  const exit = wouldHaveBeen.exit_policy_result;
  const anyFilled = [fill, sizing, chain, exit].some(
    (s) => s != null && String(s).trim() !== ''
  );

  return (
    <Panel>
      <PanelHeader
        icon="(Q)"
        title='"Would have been" projection'
        right={
          <>
            <Pill tone={anyFilled ? 'purple' : 'off'}>
              {anyFilled ? 'projected' : 'empty'}
            </Pill>
            <Pill tone="info" title="event_status">
              {eventStatus || '—'}
            </Pill>
          </>
        }
      />
      <div style={{
        fontSize: 12, color: '#9ca3af', marginBottom: 10, lineHeight: 1.55,
      }}>
        This decision did not execute. Below is what the engine projects
        WOULD have happened — useful when reviewing whether a HOLD was
        too cautious or whether an ABSTAIN dodged a bad fill.
      </div>
      <ProjectionRow
        label="Fill snapshot"
        text={fill}
        tip="Projected fill price, spread, and slippage"
      />
      <ProjectionRow
        label="Sizing chain"
        text={sizing}
        tip="Projected qty after the full sizing chain"
      />
      <ProjectionRow
        label="Chain selection"
        text={chain}
        tip="Projected option contract (if options decision)"
      />
      <ProjectionRow
        label="Exit policy"
        text={exit}
        tip="Projected first-fire exit trigger"
      />
    </Panel>
  );
}
