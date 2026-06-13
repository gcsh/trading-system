/**
 * Phase-1 Authority Spine.
 *
 * The persistent always-visible header that answers "can I trust
 * what the bot is doing?" before any other question. Lives on every
 * page; replaces the old pill-spam topbar.
 *
 * Reads /authority/status; polls every 5s.
 */
import React, { useCallback, useEffect, useState } from 'react';
import WarningsChip from './WarningsChip.jsx';
import DataQualityChip from './DataQualityChip.jsx';

const PILLAR_NAMES = ['data', 'model', 'council', 'risk', 'execution', 'learning'];

const TIER_CLASS = {
  ok: 'pill on',
  mid: 'pill warn',
  bad: 'pill danger',
  unknown: 'pill off',
};

const CONFIDENCE_CLASS = {
  CONFIDENT: 'pill on',
  WATCHING: 'pill warn',
  RESTRICTED: 'pill danger',
};

const LEVEL_CLASS = {
  SHADOW: 'pill purple',
  PAPER: 'pill info',
  GATED: 'pill warn',
  AUTONOMOUS: 'pill on',
};

const DISSENT_CLASS = {
  Normal: 'pill on',
  Elevated: 'pill warn',
  High: 'pill danger',
};

const ATTENTION_CLASS = {
  low: 'pill on',
  medium: 'pill warn',
  high: 'pill danger',
};

function formatAge(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function formatEta(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function PillarChip({ pillar, onClick }) {
  const tier = pillar.tier || 'unknown';
  return (
    <button
      onClick={onClick}
      className="pillar-chip"
      title={pillar.why}
      style={{
        all: 'unset',
        cursor: 'pointer',
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        padding: '8px 12px',
        borderRadius: 10,
        background: 'var(--panel-2)',
        border: '1px solid var(--border)',
        minWidth: 122,
        transition: 'all 0.15s',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = 'var(--border-strong)';
        e.currentTarget.style.transform = 'translateY(-1px)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--border)';
        e.currentTarget.style.transform = '';
      }}
    >
      <div style={{
        fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
        color: 'var(--muted)', fontWeight: 600,
      }}>
        {pillar.name}
      </div>
      <div className={TIER_CLASS[tier]} style={{ alignSelf: 'flex-start' }}>
        {pillar.label}
      </div>
    </button>
  );
}

function PillarDetailModal({ pillar, onClose }) {
  if (!pillar) return null;
  const tier = pillar.tier || 'unknown';
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0, 0, 0, 0.55)',
        backdropFilter: 'blur(4px)',
        display: 'grid', placeItems: 'center',
        zIndex: 100,
        padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="panel"
        style={{ maxWidth: 600, width: '100%' }}
      >
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
          <div>
            <div style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600, marginBottom: 4,
            }}>
              Pillar · {pillar.name}
            </div>
            <h3 style={{ margin: 0, fontSize: 22 }}>
              <span className={TIER_CLASS[tier]}>{pillar.label}</span>
            </h3>
          </div>
          <button className="btn small" onClick={onClose}>Close</button>
        </div>
        <div style={{ fontSize: 14, color: 'var(--text-soft)', marginBottom: 16 }}>
          {pillar.why}
        </div>

        <div className="section-title">Contract</div>
        <div style={{ display: 'grid', gap: 6, marginBottom: 16, fontSize: 12.5 }}>
          {[
            ['ok', 'Reliable / healthy'],
            ['mid', 'Warning / watching'],
            ['bad', 'Breached'],
          ].map(([k, label]) => (
            <div key={k} className="row" style={{ alignItems: 'flex-start', gap: 10 }}>
              <span className={TIER_CLASS[k]} style={{ minWidth: 80 }}>{label}</span>
              <span style={{ color: 'var(--muted)', flex: 1 }}>
                {pillar.contract?.[k] || '—'}
              </span>
            </div>
          ))}
        </div>

        {pillar.signals && Object.keys(pillar.signals).length > 0 && (
          <>
            <div className="section-title">Live signals</div>
            <pre style={{
              background: 'var(--panel-2)',
              border: '1px solid var(--border)',
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              color: 'var(--text-soft)',
              overflow: 'auto',
              margin: 0,
            }}>{JSON.stringify(pillar.signals, null, 2)}</pre>
          </>
        )}
      </div>
    </div>
  );
}

export default function AuthoritySpine({ compact = false }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [drillPillar, setDrillPillar] = useState(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch('/authority/status');
      if (!r.ok) throw new Error(`status ${r.status}`);
      setStatus(await r.json());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  if (!status) {
    return (
      <div className="authority-spine" style={{
        padding: '12px 28px', background: 'var(--panel)',
        borderBottom: '1px solid var(--border)',
        color: 'var(--muted)', fontSize: 12,
      }}>
        {error || 'Loading authority status…'}
      </div>
    );
  }

  const pillars = PILLAR_NAMES.map((n) => status.pillars[n]).filter(Boolean);
  const att = status.attention || {};

  // Top row: authority level + confidence + dissent + attention summary.
  // Bottom row: 6 pillar chips + cycle/decision meta.
  return (
    <div
      className="authority-spine"
      style={{
        background: 'var(--panel)',
        borderBottom: '1px solid var(--border)',
        padding: compact ? '10px 28px' : '14px 28px',
      }}
    >
      <div className="row" style={{
        justifyContent: 'space-between', gap: 14, marginBottom: 10,
        alignItems: 'flex-start',
      }}>
        <div className="row" style={{ gap: 18, alignItems: 'baseline', flexWrap: 'wrap' }}>
          <div>
            <div style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600,
            }}>Authority</div>
            <div className="row" style={{ gap: 6, marginTop: 4, alignItems: 'baseline' }}>
              <span className={LEVEL_CLASS[status.authority_level] || 'pill info'}
                  style={{ fontSize: 12 }}>
                {status.authority_level}
              </span>
            </div>
          </div>
          <div>
            <div style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600,
            }}>Confidence</div>
            <div className="row" style={{ gap: 6, marginTop: 4, alignItems: 'baseline' }}>
              <span className={CONFIDENCE_CLASS[status.authority_confidence] || 'pill info'}
                  style={{ fontSize: 12 }}>
                {status.authority_confidence}
              </span>
              <span style={{ fontSize: 11.5, color: 'var(--muted)', maxWidth: 280 }}>
                {status.confidence_reason}
              </span>
            </div>
          </div>
          <div>
            <div style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600,
            }}>Dissent</div>
            <div className="row" style={{ gap: 6, marginTop: 4, alignItems: 'baseline' }}>
              <span className={DISSENT_CLASS[status.dissent?.label] || 'pill off'}
                  style={{ fontSize: 12 }}>
                {status.dissent?.label || '—'}
              </span>
              <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
                {status.dissent?.window
                  ? `${Math.round((status.dissent.share || 0) * 100)}% · last ${status.dissent.window}`
                  : 'no data'}
              </span>
            </div>
          </div>
        </div>

        <div style={{
          padding: '8px 12px',
          background: 'var(--panel-2)',
          borderRadius: 10,
          border: '1px solid var(--border)',
          minWidth: 260,
          maxWidth: 420,
        }}>
          <div className="row" style={{ gap: 8 }}>
            <span style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600,
            }}>Attention</span>
            <span className={ATTENTION_CLASS[att.severity] || 'pill off'}
                style={{ fontSize: 10 }}>
              {att.severity || 'low'}
            </span>
            <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 6 }}>
              <DataQualityChip />
              <WarningsChip />
            </span>
          </div>
          <div style={{ fontWeight: 600, fontSize: 13, marginTop: 2 }}>
            {att.title || 'No action required'}
          </div>
          {att.detail && (
            <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 2 }}>
              {att.detail}
            </div>
          )}
        </div>
      </div>

      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        {pillars.map((p) => (
          <PillarChip key={p.name} pillar={p} onClick={() => setDrillPillar(p)} />
        ))}
        <div style={{ flex: 1 }} />
        <div className="row" style={{
          gap: 14, color: 'var(--muted)', fontSize: 11.5,
        }}>
          <span>Next cycle <strong style={{ color: 'var(--text-soft)' }}>{formatEta(status.next_cycle_eta_sec)}</strong></span>
          <span>Last decision <strong style={{ color: 'var(--text-soft)' }}>{formatAge(status.last_decision_age_sec)}</strong></span>
        </div>
      </div>

      {drillPillar && (
        <PillarDetailModal pillar={drillPillar} onClose={() => setDrillPillar(null)} />
      )}
    </div>
  );
}
