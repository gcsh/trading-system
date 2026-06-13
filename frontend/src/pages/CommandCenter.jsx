/**
 * Phase-1 Command Center.
 *
 * Replaces /. Three-section layout answering the operator's questions
 * in order:
 *
 *   1. Can I trust it?    → Authority Spine (already at top) + pillars summary
 *   2. What is it doing?  → current state, current opportunities, next cycle
 *   3. What needs         → attention items, biased to pillars in mid/bad
 *      attention?
 *
 * No metric cards, no candle chart, no equity curve. Those exist on
 * other pages. This page answers governance questions only.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import CurrentlyHoldingStrip from '../components/CurrentlyHoldingStrip.jsx';
import DecisionFlowHeatmap from '../components/DecisionFlowHeatmap.jsx';
import EngineActivity from '../components/EngineActivity.jsx';
import LiveTapeChart from '../components/LiveTapeChart.jsx';
import ScanUniverseStrip from '../components/ScanUniverseStrip.jsx';
import SystemControls from '../components/SystemControls.jsx';

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const TIER_PILL = {
  ok: 'pill on',
  mid: 'pill warn',
  bad: 'pill danger',
  unknown: 'pill off',
};

const CONFIDENCE_PILL = {
  CONFIDENT: 'pill on',
  WATCHING: 'pill warn',
  RESTRICTED: 'pill danger',
};

const DECISION_TAG = {
  EXECUTE: 'decision-tag execute',
  SIZE_DOWN: 'decision-tag size-down',
  MONITOR: 'decision-tag monitor',
  ABSTAIN: 'decision-tag abstain',
};

function PillarRow({ pillar }) {
  return (
    <div className="row" style={{
      justifyContent: 'space-between',
      padding: '10px 14px',
      background: 'var(--panel-2)',
      border: '1px solid var(--border)',
      borderRadius: 10,
      gap: 12,
      alignItems: 'flex-start',
    }}>
      <div style={{ flex: 1 }}>
        <div className="row" style={{ gap: 10, alignItems: 'baseline' }}>
          <div style={{ fontWeight: 600, fontSize: 13, textTransform: 'capitalize' }}>
            {pillar.name}
          </div>
          <span className={TIER_PILL[pillar.tier] || 'pill off'}>
            {pillar.label}
          </span>
        </div>
        <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 3 }}>
          {pillar.why}
        </div>
      </div>
    </div>
  );
}

function CanITrustIt({ status, error, loading }) {
  if (error) {
    return (
      <div className="panel panel--bear">
        <h3>Authority status unavailable</h3>
        <div className="accent-bear">{error}</div>
      </div>
    );
  }
  if (!status) {
    return (
      <div className="panel">
        <h3>Loading authority…</h3>
      </div>
    );
  }
  const pillars = Object.values(status.pillars || {});
  const confidence = status.authority_confidence;
  const ok = pillars.filter((p) => p.tier === 'ok').length;
  const total = pillars.length;
  return (
    <div className="panel panel--governance">
      <div className="panel-head">
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>1 · Can I trust it?</div>
          <h2 style={{ margin: '4px 0 0' }}>System Governance</h2>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <span className={CONFIDENCE_PILL[confidence] || 'pill info'}>
            {confidence}
          </span>
          <span className="pill info">{ok}/{total} reliable</span>
        </div>
      </div>
      <div style={{ color: 'var(--text-soft)', fontSize: 13, marginBottom: 12 }}>
        {status.confidence_reason}
      </div>
      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {pillars.map((p) => <PillarRow key={p.name} pillar={p} />)}
      </div>
    </div>
  );
}

function WhatIsItDoing({ status, recentDecisions, decisionsLoading }) {
  const att = status?.attention || {};
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>2 · What is it doing?</div>
          <h2 style={{ margin: '4px 0 0' }}>Activity</h2>
        </div>
        <div className="row" style={{ gap: 8, fontSize: 12, color: 'var(--muted)' }}>
          {status?.next_cycle_eta_sec != null && (
            <span>Next cycle <strong className="accent-data">
              {status.next_cycle_eta_sec}s
            </strong></span>
          )}
          {status?.last_decision_age_sec != null && (
            <span>Last decision <strong className="accent-data">
              {Math.floor(status.last_decision_age_sec / 60)}m ago
            </strong></span>
          )}
        </div>
      </div>

      <div style={{ marginBottom: 14 }}>
        <div className="section-title">Most recent decisions</div>
        {decisionsLoading ? (
          <div className="empty"><div className="title">Loading…</div></div>
        ) : !recentDecisions.length ? (
          <div className="empty">
            <div className="title">No structured consensus yet</div>
            <div className="hint">Force a trade or wait for the next cycle.</div>
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 8 }}>
            {recentDecisions.slice(0, 6).map((d) => (
              <Link
                key={d.trade_id}
                to={`/mission-control?id=${d.trade_id}`}
                className="row"
                style={{
                  justifyContent: 'space-between',
                  padding: '10px 14px',
                  background: 'var(--panel-2)',
                  border: '1px solid var(--border)',
                  borderRadius: 10,
                  textDecoration: 'none',
                  color: 'inherit',
                  transition: 'all 0.15s',
                  gap: 12,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = 'var(--governance)';
                  e.currentTarget.style.transform = 'translateY(-1px)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = 'var(--border)';
                  e.currentTarget.style.transform = '';
                }}
              >
                <div className="row" style={{ gap: 10 }}>
                  <span style={{ color: 'var(--muted)', fontSize: 12, minWidth: 36 }}>
                    #{d.trade_id}
                  </span>
                  <strong style={{ minWidth: 60 }}>{d.ticker}</strong>
                  <span className={DECISION_TAG[d.decision] || 'decision-tag abstain'}>
                    {d.decision || '—'}
                  </span>
                </div>
                <div className="row" style={{ gap: 10, fontSize: 12, color: 'var(--muted)' }}>
                  <span>{d.conviction != null ? `${Math.round(d.conviction * 100)}% conv` : '—'}</span>
                  <span>×{(d.size_mod ?? 1).toFixed(2)}</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function WhatNeedsAttention({ status }) {
  if (!status) return null;
  const att = status.attention || {};
  const pillars = Object.values(status.pillars || {});
  const watching = pillars.filter((p) => p.tier === 'mid' || p.tier === 'bad');
  const dissent = status.dissent;
  const showDissent = dissent && dissent.share > 0.25 && dissent.window > 0;
  const showNoise = att.severity === 'low' && !watching.length && !showDissent;
  return (
    <div className={`panel panel--${att.severity === 'high' ? 'risk' : att.severity === 'medium' ? 'warn' : 'data'}`}>
      <div className="panel-head">
        <div>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>3 · What needs attention?</div>
          <h2 style={{ margin: '4px 0 0' }}>{att.title || 'Quiet'}</h2>
        </div>
        <span className={
          att.severity === 'high' ? 'pill danger'
            : att.severity === 'medium' ? 'pill warn' : 'pill on'
        }>
          {att.severity || 'low'}
        </span>
      </div>
      <div style={{ color: 'var(--text-soft)', fontSize: 13, marginBottom: 12 }}>
        {att.detail}
      </div>

      {watching.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div className="section-title">Pillars watching</div>
          <div style={{ display: 'grid', gap: 6 }}>
            {watching.map((p) => <PillarRow key={p.name} pillar={p} />)}
          </div>
        </div>
      )}

      {showDissent && (
        <div style={{ marginBottom: 12 }}>
          <div className="section-title">Council dissent</div>
          <div style={{ fontSize: 13, color: 'var(--muted)' }}>
            Mean dissent <strong className="accent-warn">
              {Math.round(dissent.share * 100)}%
            </strong>{' '}
            over last {dissent.window} decisions ({dissent.label}).
          </div>
        </div>
      )}

      {showNoise && (
        <div className="empty">
          <div className="title">Nothing actionable</div>
          <div className="hint">All pillars reliable; dissent within band; no recent breaches.</div>
        </div>
      )}
    </div>
  );
}

export default function CommandCenter() {
  const [status, setStatus] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [error, setError] = useState(null);
  const [decisionsLoading, setDecisionsLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const s = await api('/authority/status');
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const loadDecisions = useCallback(async () => {
    setDecisionsLoading(true);
    try {
      const trades = await api('/trades/list?limit=15');
      const results = await Promise.allSettled(
        trades.map((t) =>
          api(`/agents/consensus/${t.id}`).then((r) => ({ trade: t, consensus: r.consensus }))
        )
      );
      const rows = [];
      for (const r of results) {
        if (r.status !== 'fulfilled' || !r.value.consensus) continue;
        const c = r.value.consensus;
        const ch = c.chairman_report || {};
        if (!ch.decision) continue;
        rows.push({
          trade_id: r.value.trade.id,
          ticker: r.value.trade.ticker,
          decision: ch.decision,
          conviction: ch.conviction,
          size_mod: ch.position_size_modifier,
        });
      }
      setDecisions(rows);
    } catch (e) {
      // silent
    } finally {
      setDecisionsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    loadDecisions();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load, loadDecisions]);

  return (
    <div className="grid" style={{ gap: 18 }}>
      <div className="col-12">
        <CanITrustIt status={status} error={error} loading={!status} />
      </div>
      <div className="col-12">
        <ScanUniverseStrip />
      </div>
      <div className="col-12">
        <CurrentlyHoldingStrip />
      </div>
      <div className="col-12">
        <DecisionFlowHeatmap />
      </div>
      <div className="col-8" style={{ display: 'grid', gap: 18 }}>
        <EngineActivity />
        <LiveTapeChart />
        <WhatIsItDoing
          status={status}
          recentDecisions={decisions}
          decisionsLoading={decisionsLoading}
        />
      </div>
      <div className="col-4" style={{ display: 'grid', gap: 18 }}>
        <WhatNeedsAttention status={status} />
        <SystemControls />
      </div>
    </div>
  );
}
