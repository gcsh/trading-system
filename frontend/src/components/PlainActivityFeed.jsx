import React, { useCallback, useEffect, useState } from 'react';
import { plainEnglish, isInteresting } from '../lib/plainEnglish.js';

const GRADE_STYLE = {
  'A+': { bg: 'var(--accent-soft)', fg: 'var(--accent)',  border: 'var(--accent)' },
  'A':  { bg: 'var(--accent-soft)', fg: 'var(--accent)',  border: 'var(--accent)' },
  'B':  { bg: 'rgba(35,131,226,0.15)', fg: 'var(--info)', border: 'var(--info)' },
  'C':  { bg: 'rgba(214,158,46,0.18)', fg: 'var(--warn)', border: 'var(--warn)' },
  'Reject': { bg: 'var(--danger-soft)', fg: 'var(--danger)', border: 'var(--danger)' },
};

function GradeChip({ grade, prob }) {
  if (!grade) return null;
  const style = GRADE_STYLE[grade] || GRADE_STYLE.C;
  const pct = prob != null ? `${Math.round(prob * 100)}%` : null;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      fontSize: 10.5, fontWeight: 700, padding: '1px 6px', borderRadius: 4,
      background: style.bg, color: style.fg, border: `1px solid ${style.border}`,
      marginLeft: 6,
    }}>
      {grade}{pct ? ` · ${pct}` : ''}
    </span>
  );
}

function AnalyticsBreakdown({ ev }) {
  const a = ev.analytics;
  if (!a) return null;
  const r = a.regime || {};
  const p = a.probability || {};
  const k = a.rank || {};
  const f = a.features || {};
  const meta = ev.meta;
  const pr = ev.portfolio_risk;
  return (
    <div style={{ marginTop: 6, display: 'grid', gap: 6, fontSize: 11.5 }}>
      <div>
        <span style={{ color: 'var(--muted)' }}>Regime · </span>
        <strong>{r.label || '—'}</strong>
        {r.confidence != null && <span style={{ color: 'var(--muted)' }}> ({Math.round(r.confidence * 100)}% confidence)</span>}
      </div>
      <div>
        <span style={{ color: 'var(--muted)' }}>Rank breakdown · </span>
        {Object.entries(k.components || {}).map(([key, v]) => (
          <span key={key} style={{ marginRight: 10 }}>
            <span style={{ color: 'var(--muted)' }}>{key}</span> {Math.round(v * 100)}%
          </span>
        ))}
      </div>
      {k.reasoning && k.reasoning.length > 0 && (
        <ul style={{ margin: '2px 0 0 18px', padding: 0, color: 'var(--text-soft)' }}>
          {k.reasoning.map((line, i) => <li key={i} style={{ marginBottom: 2 }}>{line}</li>)}
        </ul>
      )}
      <div>
        <span style={{ color: 'var(--muted)' }}>Win probability components · </span>
        {Object.entries(p.components || {}).map(([key, v]) => (
          <span key={key} style={{ marginRight: 10 }}>
            <span style={{ color: 'var(--muted)' }}>{key.replace(/_/g, ' ')}</span> {v > 0 ? '+' : ''}{(v * 100).toFixed(0)}%
          </span>
        ))}
      </div>
      {pr && (pr.concentration_flags?.length || pr.macro_risk === 'HIGH') && (
        <div style={{ padding: '4px 8px', borderRadius: 6, background: 'var(--warn-soft, rgba(214,158,46,0.12))', border: '1px solid var(--warn)' }}>
          ⚠ <strong>Portfolio risk · {pr.macro_risk}</strong>
          {pr.concentration_flags?.length ? ' — ' + pr.concentration_flags.join(' · ') : ''}
          {pr.net_beta ? ` · net beta ${pr.net_beta}` : ''}
        </div>
      )}
      {meta && (
        <div style={{ padding: '4px 8px', borderRadius: 6, background: 'var(--panel-2)', border: '1px solid var(--border)' }}>
          🧭 <strong>Meta-AI strategist · {meta.approve ? `approved (size ${Math.round((meta.risk_modifier || 1) * 100)}%)` : 'vetoed'}</strong>
          {meta.reasoning?.length ? (
            <ul style={{ margin: '4px 0 0 18px', padding: 0 }}>
              {meta.reasoning.map((line, i) => <li key={i}>{line}</li>)}
            </ul>
          ) : null}
        </div>
      )}
    </div>
  );
}

export default function PlainActivityFeed() {
  const [events, setEvents] = useState([]);
  const [busy, setBusy] = useState(null);
  const [note, setNote] = useState(null);
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch('/bot/status');
      if (!r.ok) return;
      const s = await r.json();
      setEvents(s.recent_signals || []);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  const runCycle = async () => {
    setBusy('cycle'); setNote(null);
    try {
      const r = await fetch('/bot/run-cycle', { method: 'POST' });
      const d = await r.json();
      const n = (d.events || []).filter((e) => e.status === 'submitted').length;
      setNote(n ? `Scan done — placed ${n} trade(s).` : 'Scan done — no strong setups right now.');
      await load();
    } catch (e) { setNote('Scan failed.'); }
    finally { setBusy(null); }
  };

  const testTrade = async () => {
    setBusy('force'); setNote(null);
    try {
      const r = await fetch('/bot/force-trade', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const d = await r.json();
      if (d.error) setNote(`Couldn't place a test trade: ${d.error}`);
      else setNote(`Test trade placed: ${d.event?.action?.replace(/_/g, ' ')} ${d.event?.ticker}.`);
      await load();
    } catch (e) { setNote('Test trade failed.'); }
    finally { setBusy(null); }
  };

  const shown = (showAll ? events : events.filter(isInteresting)).slice().reverse();

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>🗒️ What the AI has been doing</h2>
        <div className="row">
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--muted)', margin: 0 }}>
            <input type="checkbox" style={{ width: 'auto' }} checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> show everything
          </label>
          <button className="btn small" onClick={runCycle} disabled={!!busy}>{busy === 'cycle' ? 'Scanning…' : '🔍 Scan now'}</button>
          <button className="btn small" onClick={testTrade} disabled={!!busy}>{busy === 'force' ? 'Trading…' : '⚡ Test a trade'}</button>
        </div>
      </div>
      {note && <div style={{ fontSize: 12.5, color: 'var(--text-soft)', marginBottom: 10, padding: '6px 10px', background: 'var(--panel-2)', borderRadius: 8 }}>{note}</div>}
      {shown.length === 0 ? (
        <div className="empty" style={{ padding: '28px 12px' }}>
          <div className="title">Nothing yet</div>
          <div className="hint">Turn on the AI or press "Scan now" to see it work. Each action shows up here in plain English.</div>
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 2, maxHeight: 360, overflowY: 'auto' }}>
          {shown.map((ev, i) => {
            const p = plainEnglish(ev);
            const isBrain = ev.strategy === 'ai_brain';
            const reason = (ev.reason || '').trim();
            const hasWhy = reason && reason.toLowerCase() !== 'no signal';
            const conf = ev.confidence != null ? `${Math.round(ev.confidence * 100)}%` : null;
            const grade = ev.analytics?.rank?.grade;
            const prob = ev.analytics?.probability?.probability;
            const regimeLabel = ev.analytics?.regime?.label;
            return (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '7px 8px', borderBottom: '1px solid var(--border)' }}>
                <span style={{ fontSize: 14, width: 20, textAlign: 'center' }}>{p.icon}</span>
                <span style={{ fontSize: 11, color: 'var(--muted)', width: 64, flexShrink: 0, fontFeatureSettings: '"tnum"', paddingTop: 1 }}>{p.time}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <span className={p.tone === 'muted' ? '' : p.tone} style={{ fontSize: 13, color: p.tone === 'muted' ? 'var(--muted)' : undefined }}>{p.text}</span>
                  <GradeChip grade={grade} prob={prob} />
                  {regimeLabel && <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 6 }}>· {regimeLabel}</span>}
                  {(hasWhy || ev.analytics) && (
                    <details style={{ marginTop: 3 }} open={isBrain && ev.status === 'submitted'}>
                      <summary style={{ cursor: 'pointer', fontSize: 11, color: isBrain ? 'var(--accent)' : 'var(--muted)', userSelect: 'none' }}>
                        {isBrain ? '🧠 AI reasoning' : 'Why?'}{ev.approach ? ` · ${ev.approach}` : ''}{conf ? ` · ${conf} conviction` : ''}
                      </summary>
                      {hasWhy && (
                        <div style={{ fontSize: 12, color: 'var(--text-soft)', marginTop: 4, lineHeight: 1.55, whiteSpace: 'pre-wrap', borderLeft: '2px solid var(--border-strong)', paddingLeft: 8 }}>{reason}</div>
                      )}
                      <AnalyticsBreakdown ev={ev} />
                    </details>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
