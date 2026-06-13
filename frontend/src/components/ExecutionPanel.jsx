/**
 * MITS Phase 18-FU (Gap 13) — Decision Cockpit execution / counterfactual /
 * learning insights formatted panels.
 *
 * Before this file, /decision/cockpit/{id} returned six populated keys
 *
 *   execution.fill_snapshot
 *   execution.sizing_chain
 *   execution.chain_selection
 *   execution.exit_policy_result
 *   counterfactuals
 *   learning_insights
 *
 * that DecisionCockpit.jsx never consumed — the operator could see the
 * cockpit and have no idea the data even existed. This module ships six
 * focused presentational components that read the same shapes the backend
 * persists in Trade rows (17.B/C/D/E) and in compute_all_counterfactuals
 * (18.B) / latest_attribution_rows (18.A-D), then renders them as labeled
 * panels matching the cockpit's existing dark theme.
 *
 * Every component is null-safe: missing or partial input renders the "no
 * data yet" affordance rather than crashing the cockpit. Each panel is
 * also responsive (`flex-wrap`) so it survives narrow viewports.
 *
 * Styling parameters are shared with DecisionCockpit.jsx's existing
 * Panel / PanelHeader / Pill primitives, but those primitives are
 * defined inside DecisionCockpit.jsx so we re-declare the tiny set we
 * need here rather than churning DecisionCockpit's exports.
 */
import React from 'react';

// ── shared mini-primitives ────────────────────────────────────────────

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
    <span
      title={title || undefined}
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: 12,
        fontSize: 11,
        fontWeight: 600,
        background: c.bg,
        color: c.fg,
        border: `1px solid ${c.border}`,
        marginRight: 4,
      }}>
      {children}
    </span>
  );
}

function Panel({ children }) {
  return (
    <div style={{
      background: '#111827',
      borderRadius: 8,
      padding: 16,
      border: '1px solid #1f2937',
      marginBottom: 16,
    }}>
      {children}
    </div>
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

function fmtNumber(n, digits = 2) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  const v = Number(n);
  if (Math.abs(v) >= 10000) return v.toLocaleString(undefined, { maximumFractionDigits: digits });
  return v.toFixed(digits);
}

function fmtPct(n, digits = 1) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return `${(Number(n) * 100).toFixed(digits)}%`;
}

function fmtAbsPct(n, digits = 1) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return `${Number(n).toFixed(digits)}%`;
}

function KV({ label, value, mono, color }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between',
      alignItems: 'baseline', gap: 8,
      padding: '4px 0', fontSize: 12,
    }}>
      <span style={{ color: '#9ca3af' }}>{label}</span>
      <span style={{
        color: color || '#e5e7eb',
        fontFamily: mono ? 'monospace' : 'inherit',
        fontWeight: 600,
      }}>{value}</span>
    </div>
  );
}

function EmptyHint({ text }) {
  return (
    <div style={{ color: '#9ca3af', fontSize: 13 }}>{text}</div>
  );
}

// ── 1) FillSnapshotPanel ─────────────────────────────────────────────

/**
 * Renders Trade.fill_snapshot_json.
 *
 * Schema (17.B): the JSON either has flat keys (`bid`, `ask`, `mid`,
 * `spread_pct`, `iv`, `delta`, `quote_age_sec`, etc.) for a stock or a
 * single-leg option, OR an array key `legs` for a multi-leg spread where
 * each leg carries the same shape. We support both shapes here.
 */
export function FillSnapshotPanel({ snapshot }) {
  if (!snapshot) {
    return (
      <Panel>
        <PanelHeader icon="(X)" title="Fill snapshot" />
        <EmptyHint text="No fill snapshot persisted (Trade.fill_snapshot_json is NULL)." />
      </Panel>
    );
  }
  const legs = Array.isArray(snapshot?.legs) ? snapshot.legs : null;
  return (
    <Panel>
      <PanelHeader
        icon="(X)"
        title="Fill snapshot"
        right={
          <>
            {snapshot.instrument && <Pill tone="info">{snapshot.instrument}</Pill>}
            {snapshot.source && <Pill tone="off">src {snapshot.source}</Pill>}
            {snapshot.quote_age_sec != null && (
              <Pill tone={Number(snapshot.quote_age_sec) > 30 ? 'warn' : 'on'}>
                age {Math.round(Number(snapshot.quote_age_sec))}s
              </Pill>
            )}
          </>
        }
      />
      {legs ? (
        <div style={{ display: 'grid', gap: 12 }}>
          {legs.map((leg, i) => (
            <LegBlock key={i} leg={leg} index={i + 1} total={legs.length} />
          ))}
        </div>
      ) : (
        <LegBlock leg={snapshot} index={null} total={null} />
      )}
    </Panel>
  );
}

function LegBlock({ leg, index, total }) {
  // Render either a stock fill (8 fields) or an option fill (16-ish).
  const isOption = leg.option_type != null || leg.strike != null
    || leg.expiry != null || leg.iv != null || leg.delta != null;
  return (
    <div style={{
      padding: 10, background: '#0a0a0a',
      border: '1px solid #1f2937', borderRadius: 6,
    }}>
      {index != null && (
        <div style={{
          fontSize: 11, color: '#93c5fd',
          textTransform: 'uppercase', letterSpacing: '0.05em',
          marginBottom: 6, fontWeight: 600,
        }}>
          Leg {index} of {total}
        </div>
      )}
      <div style={{
        display: 'grid', gap: 4,
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
      }}>
        {isOption && (
          <>
            {leg.option_type != null && (
              <KV label="Type" value={String(leg.option_type).toUpperCase()} />
            )}
            {leg.strike != null && (
              <KV label="Strike" value={fmtNumber(leg.strike, 2)} mono />
            )}
            {leg.expiry != null && (
              <KV label="Expiry" value={String(leg.expiry)} mono />
            )}
            {leg.dte != null && (
              <KV label="DTE" value={fmtNumber(leg.dte, 0)} />
            )}
          </>
        )}
        {leg.bid != null && (
          <KV label="Bid" value={fmtNumber(leg.bid, 4)} mono />
        )}
        {leg.ask != null && (
          <KV label="Ask" value={fmtNumber(leg.ask, 4)} mono />
        )}
        {leg.mid != null && (
          <KV label="Mid" value={fmtNumber(leg.mid, 4)} mono />
        )}
        {leg.fill_price != null && (
          <KV label="Fill" value={fmtNumber(leg.fill_price, 4)} mono color="#10b981" />
        )}
        {leg.spread_pct != null && (
          <KV label="Spread %" value={fmtAbsPct(leg.spread_pct, 2)} />
        )}
        {leg.slippage_bps != null && (
          <KV label="Slippage (bps)" value={fmtNumber(leg.slippage_bps, 1)} />
        )}
        {leg.iv != null && (
          <KV label="IV" value={fmtAbsPct(leg.iv, 1)} />
        )}
        {leg.delta != null && (
          <KV label="Delta" value={fmtNumber(leg.delta, 3)} />
        )}
        {leg.gamma != null && (
          <KV label="Gamma" value={fmtNumber(leg.gamma, 4)} />
        )}
        {leg.theta != null && (
          <KV label="Theta" value={fmtNumber(leg.theta, 4)} />
        )}
        {leg.vega != null && (
          <KV label="Vega" value={fmtNumber(leg.vega, 4)} />
        )}
        {leg.open_interest != null && (
          <KV label="OI" value={fmtNumber(leg.open_interest, 0)} />
        )}
        {leg.volume != null && (
          <KV label="Volume" value={fmtNumber(leg.volume, 0)} />
        )}
        {leg.quote_age_sec != null && index != null && (
          <KV label="Age (s)" value={fmtNumber(leg.quote_age_sec, 0)} />
        )}
      </div>
    </div>
  );
}

// ── 2) SizingChainPanel ──────────────────────────────────────────────

/**
 * Renders Trade.sizing_chain_json (17.C).
 *
 * Schema: {base_qty, steps: [{name, input, factor, output, reason?}, ...],
 *          rounded_final}. We render base → ordered steps → final with
 * input × factor = output rows so the operator can see exactly which
 * multiplier pulled the size in which direction.
 */
export function SizingChainPanel({ chain }) {
  if (!chain) {
    return (
      <Panel>
        <PanelHeader icon="(Z)" title="Sizing chain" />
        <EmptyHint text="No sizing chain persisted (Trade.sizing_chain_json is NULL)." />
      </Panel>
    );
  }
  const steps = Array.isArray(chain.steps) ? chain.steps : [];
  return (
    <Panel>
      <PanelHeader
        icon="(Z)"
        title="Sizing chain"
        right={
          <>
            {chain.base_qty != null && (
              <Pill tone="off">base {fmtNumber(chain.base_qty, 0)}</Pill>
            )}
            {chain.rounded_final != null && (
              <Pill tone="on">final {fmtNumber(chain.rounded_final, 0)}</Pill>
            )}
          </>
        }
      />
      <div style={{ display: 'grid', gap: 6 }}>
        <div style={{
          padding: 8, background: '#0a0a0a',
          border: '1px solid #1f2937', borderRadius: 6,
          fontSize: 12, color: '#9ca3af',
        }}>
          <strong style={{ color: '#e5e7eb' }}>Base qty</strong>:
          {' '}{fmtNumber(chain.base_qty, 4)}
        </div>
        {steps.map((s, i) => {
          const factor = Number(s.factor);
          const dir = !Number.isFinite(factor) || factor === 1 ? null
                    : factor < 1 ? 'down' : 'up';
          const dirColor = dir === 'down' ? '#fcd34d'
                         : dir === 'up' ? '#6ee7b7'
                         : '#9ca3af';
          return (
            <div key={i} style={{
              padding: 8, background: '#0a0a0a',
              border: '1px solid #1f2937', borderRadius: 6,
            }}>
              <div style={{
                fontSize: 11, color: '#93c5fd',
                textTransform: 'uppercase', letterSpacing: '0.05em',
                marginBottom: 4, fontWeight: 600,
              }}>
                Step {i + 1}: {s.name || '(unnamed)'}
              </div>
              <div style={{ fontSize: 12, color: '#d1d5db' }}>
                input <strong style={{ color: '#e5e7eb' }}>
                  {fmtNumber(s.input, 4)}
                </strong>
                {' x '}
                factor <strong style={{ color: dirColor }}>
                  {fmtNumber(s.factor, 4)}
                </strong>
                {' = '}
                output <strong style={{ color: '#e5e7eb' }}>
                  {fmtNumber(s.output, 4)}
                </strong>
              </div>
              {s.reason && (
                <div style={{
                  marginTop: 4, fontSize: 11, color: '#9ca3af',
                }}>
                  {String(s.reason)}
                </div>
              )}
            </div>
          );
        })}
        <div style={{
          padding: 8, background: '#064e3b',
          border: '1px solid #10b981', borderRadius: 6,
          fontSize: 12, color: '#6ee7b7',
        }}>
          <strong>Rounded final qty</strong>:
          {' '}{fmtNumber(chain.rounded_final, 0)}
          {chain.cap_applied && (
            <span style={{ marginLeft: 10, color: '#fcd34d' }}>
              (cap applied: {chain.cap_applied})
            </span>
          )}
        </div>
      </div>
    </Panel>
  );
}

// ── 3) ChainSelectionPanel ───────────────────────────────────────────

/**
 * Renders Trade.chain_selection_json (17.D).
 *
 * Schema: {requested: {delta_band, dte_band, ...},
 *          chosen: {symbol, strike, expiry, ...},
 *          candidates: [{symbol, strike, expiry, delta, score,
 *                        rejection_reason?, chosen?}, ...]}.
 *
 * Renders a table of candidates with rejection_reason column; the chosen
 * row is highlighted green.
 */
export function ChainSelectionPanel({ selection }) {
  if (!selection) {
    return (
      <Panel>
        <PanelHeader icon="(K)" title="Chain selection" />
        <EmptyHint text="No chain selection persisted (Trade.chain_selection_json is NULL)." />
      </Panel>
    );
  }
  const candidates = Array.isArray(selection.candidates) ? selection.candidates : [];
  const requested = selection.requested || {};
  const chosen = selection.chosen || {};
  return (
    <Panel>
      <PanelHeader
        icon="(K)"
        title="Chain selection"
        right={
          <>
            {requested.delta_band && (
              <Pill tone="info">delta {String(requested.delta_band)}</Pill>
            )}
            {requested.dte_band && (
              <Pill tone="info">DTE {String(requested.dte_band)}</Pill>
            )}
            {selection.chain_freshness_sec != null && (
              <Pill tone={Number(selection.chain_freshness_sec) > 60 ? 'warn' : 'on'}>
                chain {Math.round(Number(selection.chain_freshness_sec))}s
              </Pill>
            )}
          </>
        }
      />
      {chosen && (chosen.symbol || chosen.strike) && (
        <div style={{
          padding: 8, marginBottom: 10, background: '#064e3b',
          border: '1px solid #10b981', borderRadius: 6,
          fontSize: 12, color: '#6ee7b7',
        }}>
          <strong>Chosen</strong>:
          {' '}{chosen.symbol || '—'}
          {chosen.strike != null && (<> | strike {fmtNumber(chosen.strike, 2)}</>)}
          {chosen.expiry && (<> | exp {chosen.expiry}</>)}
          {chosen.delta != null && (<> | delta {fmtNumber(chosen.delta, 3)}</>)}
        </div>
      )}
      {candidates.length === 0 ? (
        <EmptyHint text="No candidate set persisted." />
      ) : (
        <div style={{
          overflowX: 'auto',
          background: '#0a0a0a', borderRadius: 6,
          border: '1px solid #1f2937',
        }}>
          <table style={{
            width: '100%', borderCollapse: 'collapse', fontSize: 12,
          }}>
            <thead>
              <tr style={{ background: '#111827', color: '#9ca3af' }}>
                <th style={{ textAlign: 'left', padding: 8 }}>Symbol</th>
                <th style={{ textAlign: 'right', padding: 8 }}>Strike</th>
                <th style={{ textAlign: 'left', padding: 8 }}>Expiry</th>
                <th style={{ textAlign: 'right', padding: 8 }}>Delta</th>
                <th style={{ textAlign: 'right', padding: 8 }}>Score</th>
                <th style={{ textAlign: 'left', padding: 8 }}>Status</th>
                <th style={{ textAlign: 'left', padding: 8 }}>Rejection reason</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c, i) => {
                const isChosen = !!c.chosen
                  || (chosen?.symbol && c.symbol === chosen.symbol);
                return (
                  <tr key={i} style={{
                    borderTop: '1px solid #1f2937',
                    background: isChosen ? 'rgba(16,185,129,0.08)' : 'transparent',
                  }}>
                    <td style={{
                      padding: 8, color: isChosen ? '#6ee7b7' : '#e5e7eb',
                      fontFamily: 'monospace',
                    }}>{c.symbol || '—'}</td>
                    <td style={{
                      padding: 8, textAlign: 'right',
                      color: '#d1d5db', fontFamily: 'monospace',
                    }}>{fmtNumber(c.strike, 2)}</td>
                    <td style={{
                      padding: 8, color: '#d1d5db', fontFamily: 'monospace',
                    }}>{c.expiry || '—'}</td>
                    <td style={{
                      padding: 8, textAlign: 'right',
                      color: '#d1d5db', fontFamily: 'monospace',
                    }}>{fmtNumber(c.delta, 3)}</td>
                    <td style={{
                      padding: 8, textAlign: 'right',
                      color: '#d1d5db', fontFamily: 'monospace',
                    }}>{fmtNumber(c.score, 3)}</td>
                    <td style={{ padding: 8 }}>
                      {isChosen ? (
                        <Pill tone="on">chosen</Pill>
                      ) : c.rejection_reason ? (
                        <Pill tone="danger">rejected</Pill>
                      ) : (
                        <Pill tone="off">considered</Pill>
                      )}
                    </td>
                    <td style={{
                      padding: 8, color: '#9ca3af', fontSize: 11,
                    }}>{c.rejection_reason || '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ── 4) ExitPolicyResultPanel ─────────────────────────────────────────

/**
 * Renders Trade.exit_policy_result_json (17.E).
 *
 * Schema: {chosen_trigger: {rule_name, legacy_action, severity, reason},
 *          rule_evaluations: [{rule_name, severity, fired, reason,
 *                              evidence}, ...]}.
 *
 * Renders the headline trigger + a list of every rule that was
 * evaluated, fired or not.
 */
export function ExitPolicyResultPanel({ result }) {
  if (!result) {
    return (
      <Panel>
        <PanelHeader icon="(E)" title="Exit policy result" />
        <EmptyHint text="No exit policy result persisted (entry trade or non-exit_manager close)." />
      </Panel>
    );
  }
  const trigger = result.chosen_trigger || result.trigger || null;
  const evals = Array.isArray(result.rule_evaluations)
    ? result.rule_evaluations
    : (Array.isArray(result.evaluations) ? result.evaluations : []);
  return (
    <Panel>
      <PanelHeader
        icon="(E)"
        title="Exit policy result"
        right={
          trigger ? (
            <>
              <Pill tone="danger">{trigger.rule_name || trigger.name || '—'}</Pill>
              {trigger.severity && (
                <Pill tone="warn">{trigger.severity}</Pill>
              )}
            </>
          ) : (
            <Pill tone="off">no trigger</Pill>
          )
        }
      />
      {trigger && (
        <div style={{
          padding: 10, background: '#7f1d1d',
          border: '1px solid #ef4444', borderRadius: 6,
          fontSize: 12, color: '#fca5a5', marginBottom: 10,
        }}>
          <div style={{
            fontSize: 11, fontWeight: 700,
            textTransform: 'uppercase', letterSpacing: '0.05em',
            marginBottom: 4,
          }}>
            Chosen trigger
          </div>
          <div>
            <strong style={{ color: '#fee2e2' }}>{trigger.rule_name || trigger.name}</strong>
            {trigger.legacy_action && (<> | action <strong>{trigger.legacy_action}</strong></>)}
          </div>
          {trigger.reason && (
            <div style={{ marginTop: 4, color: '#fecaca' }}>
              {String(trigger.reason)}
            </div>
          )}
        </div>
      )}
      {evals.length === 0 ? (
        <EmptyHint text="No rule evaluations recorded." />
      ) : (
        <div style={{ display: 'grid', gap: 6 }}>
          {evals.map((ev, i) => {
            const fired = !!(ev.fired ?? ev.triggered);
            return (
              <div key={i} style={{
                padding: 8, background: '#0a0a0a',
                border: `1px solid ${fired ? '#ef4444' : '#1f2937'}`,
                borderRadius: 6,
              }}>
                <div style={{
                  display: 'flex', justifyContent: 'space-between',
                  alignItems: 'baseline', marginBottom: 4,
                }}>
                  <div style={{
                    fontSize: 12, fontWeight: 600,
                    color: fired ? '#fca5a5' : '#e5e7eb',
                  }}>
                    {ev.rule_name || ev.name || `Rule ${i + 1}`}
                  </div>
                  <div>
                    {fired ? (
                      <Pill tone="danger">fired</Pill>
                    ) : (
                      <Pill tone="off">passed</Pill>
                    )}
                    {ev.severity && (
                      <Pill tone={ev.severity === 'hard' ? 'danger' : 'warn'}>
                        {ev.severity}
                      </Pill>
                    )}
                  </div>
                </div>
                {ev.reason && (
                  <div style={{ fontSize: 11, color: '#9ca3af' }}>
                    {String(ev.reason)}
                  </div>
                )}
                {ev.evidence && typeof ev.evidence === 'object'
                    && Object.keys(ev.evidence).length > 0 && (
                  <div style={{
                    marginTop: 4, padding: 4,
                    background: '#111827', borderRadius: 4,
                    fontSize: 11, color: '#d1d5db',
                    fontFamily: 'monospace',
                  }}>
                    {Object.entries(ev.evidence).map(([k, v]) => (
                      <div key={k}>{k}: {typeof v === 'object' ? JSON.stringify(v) : String(v)}</div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

// ── 5) CounterfactualsPanel ──────────────────────────────────────────

/**
 * Renders `counterfactuals` from /decision/cockpit (18.B).
 *
 * Schema: {sizing: {pnl_curve?, ...}, policy: {current?, alternative?, ...},
 *          consensus: {current?, alternative?, ...}}.
 *
 * Three side-by-side cards so the operator can see "if we'd done X" at a
 * glance without opening the Studio.
 */
export function CounterfactualsPanel({ cf }) {
  if (!cf) {
    return (
      <Panel>
        <PanelHeader icon="(W)" title="Counterfactuals (What If?)" />
        <EmptyHint text="No counterfactual bundle yet (decision pre-execution or compute deferred)." />
      </Panel>
    );
  }
  const wrap = cf.counterfactuals || cf.bundle || cf || {};
  const sizing = wrap.sizing || null;
  const policy = wrap.policy || null;
  const consensus = wrap.consensus || null;
  return (
    <Panel>
      <PanelHeader
        icon="(W)"
        title="Counterfactuals (What If?)"
        right={<Pill tone="off">live snapshot</Pill>}
      />
      <div style={{
        display: 'grid', gap: 10,
        gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
      }}>
        <CFCard title="Sizing" data={sizing} renderBody={renderSizingCFBody} />
        <CFCard title="Policy" data={policy} renderBody={renderPolicyCFBody} />
        <CFCard title="Consensus" data={consensus} renderBody={renderConsensusCFBody} />
      </div>
    </Panel>
  );
}

function CFCard({ title, data, renderBody }) {
  return (
    <div style={{
      padding: 10, background: '#0a0a0a',
      border: '1px solid #1f2937', borderRadius: 6,
    }}>
      <div style={{
        fontSize: 11, color: '#93c5fd',
        textTransform: 'uppercase', letterSpacing: '0.05em',
        marginBottom: 6, fontWeight: 600,
      }}>{title}</div>
      {data ? renderBody(data) : (
        <div style={{ color: '#6b7280', fontSize: 11 }}>not computed</div>
      )}
    </div>
  );
}

function renderSizingCFBody(s) {
  const curve = s.pnl_curve || s.curve || null;
  return (
    <div style={{ fontSize: 12, color: '#d1d5db' }}>
      {s.original_factor != null && (
        <KV label="Original factor" value={fmtNumber(s.original_factor, 2)} mono />
      )}
      {s.realized_pnl_pct != null && (
        <KV label="Realized P&L"
          value={fmtAbsPct(s.realized_pnl_pct, 2)}
          color={Number(s.realized_pnl_pct) >= 0 ? '#10b981' : '#ef4444'} mono />
      )}
      {curve && typeof curve === 'object' && (
        <div style={{
          marginTop: 6, padding: 6, background: '#111827',
          borderRadius: 4, fontFamily: 'monospace', fontSize: 11,
        }}>
          {Object.entries(curve).map(([k, v]) => {
            const num = Number(v);
            return (
              <div key={k} style={{
                color: num >= 0 ? '#6ee7b7' : '#fca5a5',
              }}>x{k}: {Number.isFinite(num) ? num.toFixed(2) : String(v)}%</div>
            );
          })}
        </div>
      )}
      {s.notes && (
        <div style={{ marginTop: 4, color: '#9ca3af', fontSize: 11 }}>
          {String(s.notes)}
        </div>
      )}
    </div>
  );
}

function renderPolicyCFBody(p) {
  return (
    <div style={{ fontSize: 12, color: '#d1d5db' }}>
      {p.rule_name && <KV label="Rule" value={p.rule_name} mono />}
      {p.current != null && (
        <KV label="Current" value={typeof p.current === 'object' ? JSON.stringify(p.current) : String(p.current)} mono />
      )}
      {p.alternative != null && (
        <KV label="Alternative" value={typeof p.alternative === 'object' ? JSON.stringify(p.alternative) : String(p.alternative)} mono />
      )}
      {p.notes && (
        <div style={{ marginTop: 4, color: '#9ca3af', fontSize: 11 }}>
          {String(p.notes)}
        </div>
      )}
    </div>
  );
}

function renderConsensusCFBody(c) {
  return (
    <div style={{ fontSize: 12, color: '#d1d5db' }}>
      {c.agent && <KV label="Agent" value={c.agent} mono />}
      {c.current != null && (
        <KV label="Current" value={typeof c.current === 'object' ? JSON.stringify(c.current) : String(c.current)} mono />
      )}
      {c.alternative != null && (
        <KV label="Alternative" value={typeof c.alternative === 'object' ? JSON.stringify(c.alternative) : String(c.alternative)} mono />
      )}
      {c.delta_confidence != null && (
        <KV label="Delta conf" value={fmtNumber(c.delta_confidence, 3)} />
      )}
      {c.notes && (
        <div style={{ marginTop: 4, color: '#9ca3af', fontSize: 11 }}>
          {String(c.notes)}
        </div>
      )}
    </div>
  );
}

// ── 6) LearningInsightsPanel ─────────────────────────────────────────

/**
 * Renders /decision/cockpit's learning_insights block (18.A-D summary).
 *
 * Schema: {
 *   attribution_summary: {computed_at, window_days, n_rows, by_scope?},
 *   active_policy_recommendations: {advisory_enabled, auto_apply_enabled,
 *                                    rows: [...], computed_at, n_recommendations},
 *   active_weight_proposals: {advisory_enabled, apply_enabled,
 *                              rows: [...], computed_at, n_proposals,
 *                              known_agents}
 * }.
 */
export function LearningInsightsPanel({ insights }) {
  if (!insights) {
    return (
      <Panel>
        <PanelHeader icon="(L)" title="Learning insights" />
        <EmptyHint text="No learning insights yet (18.A-D passes not run, or compute failed)." />
      </Panel>
    );
  }
  const attr = insights.attribution_summary;
  const policy = insights.active_policy_recommendations;
  const weight = insights.active_weight_proposals;
  return (
    <Panel>
      <PanelHeader
        icon="(L)"
        title="Learning insights"
        right={
          <>
            {policy?.advisory_enabled && (
              <Pill tone={policy.auto_apply_enabled ? 'on' : 'warn'}>
                policy {policy.auto_apply_enabled ? 'auto-apply' : 'advisory'}
              </Pill>
            )}
            {weight?.advisory_enabled && (
              <Pill tone={weight.apply_enabled ? 'on' : 'warn'}>
                weights {weight.apply_enabled ? 'apply' : 'advisory'}
              </Pill>
            )}
          </>
        }
      />
      <div style={{ display: 'grid', gap: 10 }}>
        {attr && (
          <div style={{
            padding: 10, background: '#0a0a0a',
            border: '1px solid #1f2937', borderRadius: 6,
          }}>
            <div style={{
              fontSize: 11, color: '#93c5fd',
              textTransform: 'uppercase', letterSpacing: '0.05em',
              fontWeight: 600, marginBottom: 6,
            }}>Attribution summary</div>
            <div style={{
              display: 'grid', gap: 4,
              gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            }}>
              <KV label="Computed at" value={attr.computed_at || '—'} />
              <KV label="Window" value={attr.window_days != null ? `${attr.window_days}d` : '—'} />
              <KV label="N rows" value={fmtNumber(attr.n_rows, 0)} />
            </div>
            {attr.by_scope && typeof attr.by_scope === 'object' && (
              <div style={{
                marginTop: 6, fontSize: 11, color: '#9ca3af',
              }}>
                by scope: {Object.entries(attr.by_scope)
                  .map(([k, v]) => `${k}=${v}`).join(' | ')}
              </div>
            )}
            {attr.note && (
              <div style={{
                marginTop: 6, fontSize: 11, color: '#fcd34d',
              }}>{String(attr.note)}</div>
            )}
          </div>
        )}
        {policy && (
          <RecRows
            title={`Active policy recommendations (n=${policy.n_recommendations || 0})`}
            rows={policy.rows || []}
            cols={[
              { key: 'rule_name', label: 'Rule', mono: true },
              { key: 'current_value', label: 'Current' },
              { key: 'recommended_value', label: 'Recommended' },
              { key: 'recommendation_confidence', label: 'Confidence' },
            ]}
            empty="No policy recommendations yet."
          />
        )}
        {weight && (
          <RecRows
            title={`Active weight proposals (n=${weight.n_proposals || 0})`}
            rows={weight.rows || []}
            cols={[
              { key: 'agent', label: 'Agent', mono: true },
              { key: 'base_weight', label: 'Base', fmt: (v) => fmtNumber(v, 2) },
              { key: 'weight_proposed', label: 'Proposed', fmt: (v) => fmtNumber(v, 2) },
              { key: 'adaptive_multiplier', label: 'Multiplier', fmt: (v) => fmtNumber(v, 3) },
              { key: 'n_closed', label: 'n_closed' },
              { key: 'confidence_level', label: 'Confidence' },
            ]}
            empty="No weight proposals yet."
          />
        )}
      </div>
    </Panel>
  );
}

function RecRows({ title, rows, cols, empty }) {
  return (
    <div style={{
      padding: 10, background: '#0a0a0a',
      border: '1px solid #1f2937', borderRadius: 6,
    }}>
      <div style={{
        fontSize: 11, color: '#93c5fd',
        textTransform: 'uppercase', letterSpacing: '0.05em',
        fontWeight: 600, marginBottom: 6,
      }}>{title}</div>
      {rows.length === 0 ? (
        <div style={{ color: '#6b7280', fontSize: 11 }}>{empty}</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{
            width: '100%', borderCollapse: 'collapse', fontSize: 12,
          }}>
            <thead>
              <tr style={{ color: '#9ca3af' }}>
                {cols.map((c) => (
                  <th key={c.key} style={{
                    textAlign: 'left', padding: 6, fontWeight: 600,
                  }}>{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} style={{ borderTop: '1px solid #1f2937' }}>
                  {cols.map((c) => {
                    const raw = r[c.key];
                    const display = c.fmt ? c.fmt(raw) : (raw == null ? '—' : String(raw));
                    return (
                      <td key={c.key} style={{
                        padding: 6, color: '#d1d5db',
                        fontFamily: c.mono ? 'monospace' : 'inherit',
                      }}>{display}</td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
