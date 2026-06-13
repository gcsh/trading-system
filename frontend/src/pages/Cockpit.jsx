import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import AnnotatedStrategyChart from '../components/AnnotatedStrategyChart.jsx';
import PlainActivityFeed from '../components/PlainActivityFeed.jsx';
import AIPicks from '../components/AIPicks.jsx';
import StrategyScout from '../components/StrategyScout.jsx';
import { GexMini, FlowStrip } from '../components/EdgeWidgets.jsx';
import NarrativeStrip from '../components/NarrativeStrip.jsx';
import PortfolioRiskWidget from '../components/PortfolioRiskWidget.jsx';
import AuditHealthBanner from '../components/AuditHealthBanner.jsx';
import MetricsCard from '../components/MetricsCard.jsx';
import CohortHeatWidget from '../components/CohortHeatWidget.jsx';
import ResearchDigest from '../components/ResearchDigest.jsx';
import LessonsLearned from '../components/LessonsLearned.jsx';
import TickerSearch from '../components/TickerSearch.jsx';
import { money, pct, shares } from '../lib/format.js';

function AutonomySwitch({ on, busy, onToggle }) {
  return (
    <button
      onClick={() => onToggle(!on)}
      disabled={busy}
      style={{
        position: 'relative', width: 184, height: 60, borderRadius: 999, cursor: busy ? 'wait' : 'pointer',
        border: `1px solid ${on ? 'var(--accent)' : 'var(--border-strong)'}`,
        background: on ? 'linear-gradient(135deg, var(--accent), var(--accent-2))' : 'var(--panel-2)',
        boxShadow: on ? '0 0 24px rgba(47,179,137,0.45)' : 'none',
        transition: 'all .25s', padding: 0, overflow: 'hidden',
      }}
      title={on ? 'AI is trading for you — click to pause' : 'Click to let the AI trade for you'}
    >
      <span style={{
        position: 'absolute', top: 5, left: on ? 128 : 5, width: 50, height: 48, borderRadius: 999,
        background: '#fff', transition: 'left .25s cubic-bezier(.4,1.4,.6,1)', display: 'grid', placeItems: 'center',
        fontSize: 22, boxShadow: '0 2px 6px rgba(0,0,0,0.25)',
      }}>{on ? '🤖' : '⏸'}</span>
      <span style={{
        position: 'absolute', top: 0, bottom: 0, left: on ? 16 : 64, display: 'flex', alignItems: 'center',
        color: on ? '#fff' : 'var(--muted)', fontWeight: 700, fontSize: 12, letterSpacing: '0.04em',
        textTransform: 'uppercase', maxWidth: 70, lineHeight: 1.15,
      }}>{on ? 'AI Trading' : 'Watch only'}</span>
    </button>
  );
}

function HeroStat({ label, value, sub, tone }) {
  return (
    <div style={{ minWidth: 120 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>{label}</div>
      <div className={tone} style={{ fontSize: 24, fontWeight: 700, fontFeatureSettings: '"tnum"', marginTop: 2 }}>{value}</div>
      {sub != null && <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 1 }}>{sub}</div>}
    </div>
  );
}

function Readiness({ readiness }) {
  if (!readiness) return null;
  const { score, max, checks, verdict } = readiness;
  const pctScore = (score / max) * 100;
  const color = score >= 5 ? 'var(--accent)' : score >= 3 ? 'var(--warn)' : 'var(--danger)';
  return (
    <div className="panel">
      <div className="panel-head">
        <h2>🎯 Ready to go live?</h2>
        <span style={{ fontWeight: 700, color }}>{score} / {max}</span>
      </div>
      <div className="gauge" style={{ marginBottom: 12 }}>
        <div className="gauge-track"><div className="gauge-fill" style={{ width: `${pctScore}%`, background: color }} /></div>
      </div>
      <div style={{ display: 'grid', gap: 8 }}>
        {checks.map((c) => (
          <div key={c.label} style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
            <span style={{ color: c.pass ? 'var(--accent)' : 'var(--muted-2)', fontWeight: 700 }}>{c.pass ? '✓' : '○'}</span>
            <div>
              <div style={{ fontSize: 13, fontWeight: 600, color: c.pass ? 'var(--text)' : 'var(--text-soft)' }}>{c.label}</div>
              <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{c.detail}</div>
            </div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 12, fontSize: 12.5, color: 'var(--text-soft)', lineHeight: 1.5, paddingTop: 10, borderTop: '1px solid var(--border)' }}>{verdict}</div>
    </div>
  );
}

function Holdings({ positions }) {
  return (
    <div className="panel">
      <div className="panel-head"><h2>💼 What you own right now</h2><span className="panel-sub">{positions?.length || 0} position(s)</span></div>
      {!positions || positions.length === 0 ? (
        <div className="empty" style={{ padding: '24px 12px' }}><div className="title">All cash</div><div className="hint">No open positions — the AI is waiting for a good setup.</div></div>
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          {positions.map((p) => {
            const up = (p.unrealized_pnl_pct ?? 0) >= 0;
            return (
              <div key={`${p.ticker}-${p.kind}`} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 12px', background: 'var(--panel-2)', borderRadius: 9, border: '1px solid var(--border)' }}>
                <div>
                  <div style={{ fontWeight: 700 }}>{p.ticker} <span style={{ fontSize: 11, color: 'var(--muted)', fontWeight: 500 }}>{p.kind === 'stock' ? `${shares(p.quantity)} shares` : p.kind}</span></div>
                  {p.kind === 'stock' && <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>avg {money(p.avg_cost)} {p.current_price ? `· now ${money(p.current_price)}` : ''}</div>}
                </div>
                {p.kind === 'stock' && p.unrealized_pnl_pct != null && (
                  <div style={{ textAlign: 'right' }}>
                    <div className={up ? 'pos' : 'neg'} style={{ fontWeight: 700 }}>{pct(p.unrealized_pnl_pct, 1, { showSign: true })}</div>
                    <div className={up ? 'pos' : 'neg'} style={{ fontSize: 11.5 }}>{money(p.unrealized_pnl, { showSign: true })}</div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function Cockpit() {
  const { config, refresh } = useOutletContext();
  const [brief, setBrief] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [focal, setFocal] = useState((config?.tickers && config.tickers[0]) || 'AAPL');

  const loadBrief = useCallback(async () => {
    try {
      const r = await fetch('/copilot/briefing');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setBrief(await r.json());
      setErr(null);
    } catch (e) { setErr(e.message); }
  }, []);

  useEffect(() => {
    loadBrief();
    const id = setInterval(loadBrief, 6000);
    return () => clearInterval(id);
  }, [loadBrief]);

  const toggleAutonomy = async (on) => {
    setBusy(true);
    try {
      await fetch('/copilot/autonomy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }) });
      await loadBrief();
      refresh && refresh();
    } finally { setBusy(false); }
  };

  const startTrial = async () => {
    if (!window.confirm('Reset your practice account to $5,000 and restart the 30-day trial? This clears current paper positions.')) return;
    setBusy(true);
    try {
      await fetch('/copilot/start-trial', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ starting_cash: 5000 }) });
      await loadBrief();
      refresh && refresh();
    } finally { setBusy(false); }
  };

  const toggleBrain = async () => {
    if (!brief?.ai_available) return;
    setBusy(true);
    try {
      await fetch('/copilot/brain', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !brief.brain_enabled, web_research: !!brief.brain_web_research }),
      });
      await loadBrief();
      refresh && refresh();
    } finally { setBusy(false); }
  };

  const toggleMeta = async () => {
    if (!brief?.ai_available) return;
    setBusy(true);
    try {
      await fetch('/copilot/meta', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !brief.meta_enabled }),
      });
      await loadBrief();
      refresh && refresh();
    } finally { setBusy(false); }
  };

  const totalTone = useMemo(() => (brief && brief.total_return_pct >= 0 ? 'pos' : 'neg'), [brief]);
  const todayTone = useMemo(() => (brief && brief.pnl_today >= 0 ? 'pos' : 'neg'), [brief]);

  return (
    <div className="grid">
      {/* ---- Hero: the one switch + the numbers that matter ---- */}
      <div className="panel col-12" style={{ background: 'var(--bg-elev)' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 28, alignItems: 'center' }}>
          <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
            <AutonomySwitch on={!!brief?.autonomous} busy={busy} onToggle={toggleAutonomy} />
            <div style={{ maxWidth: 220 }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>{brief?.autonomous ? 'AI is trading for you' : 'Let the AI trade for you'}</div>
              <div style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.4 }}>
                {brief?.autonomous ? `Scanning ${brief?.tickers?.length || 0} stocks every ${brief?.interval_sec}s.` : 'Flip the switch — it trades paper money only.'}
              </div>
              <button className="btn small ghost" style={{ marginTop: 6, padding: '2px 8px', fontSize: 11 }} onClick={startTrial} disabled={busy}>↻ Restart $5,000 trial</button>
              <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                <button
                  className={`btn small ${brief?.brain_enabled ? 'primary' : 'ghost'}`}
                  style={{ padding: '2px 9px', fontSize: 11 }}
                  onClick={toggleBrain}
                  disabled={busy || !brief?.ai_available}
                  title={brief?.ai_available
                    ? 'Full autonomy: Claude reasons freely (any strategy + live research) and trades paper money. Risk limits still apply.'
                    : 'Add ANTHROPIC_API_KEY to your .env to enable the AI Brain.'}
                >🧠 AI Brain {brief?.brain_enabled ? 'ON' : 'OFF'}</button>
                {!brief?.ai_available && <span style={{ fontSize: 10.5, color: 'var(--muted)' }}>needs API key</span>}
                {brief?.brain_enabled && brief?.ai_available && (
                  brief?.running
                    ? <span style={{ fontSize: 10.5, color: 'var(--accent)' }}>🟢 reasoning live{brief?.brain_web_research ? ' + web' : ''}</span>
                    : <span style={{ fontSize: 10.5, color: 'var(--muted)' }}>armed — activates when the bot is on</span>
                )}
                <button
                  className={`btn small ${brief?.meta_enabled ? 'primary' : 'ghost'}`}
                  style={{ padding: '2px 9px', fontSize: 11 }}
                  onClick={toggleMeta}
                  disabled={busy || !brief?.ai_available}
                  title={brief?.ai_available
                    ? 'Meta-AI strategist: Claude audits every analytical decision (regime, grade, portfolio exposure) and approves/vetoes with a position-size modifier. Risk limits still apply.'
                    : 'Add ANTHROPIC_API_KEY to enable the Meta-AI strategist.'}
                >🧭 Meta-AI {brief?.meta_enabled ? 'ON' : 'OFF'}</button>
              </div>
            </div>
          </div>
          <div style={{ flex: 1, display: 'flex', flexWrap: 'wrap', gap: 28, justifyContent: 'flex-end' }}>
            <HeroStat label="Account value" value={money(brief?.equity ?? 0)} sub={`from ${money(brief?.starting_cash ?? 0)}`} tone={totalTone} />
            <HeroStat label="Total return" value={pct(brief?.total_return_pct ?? 0, 1, { showSign: true })} sub={brief?.beat_spy ? 'beating SPY 📈' : (brief?.benchmark_spy_pct != null ? `SPY ${pct(brief.benchmark_spy_pct, 1, { showSign: true })}` : '—')} tone={totalTone} />
            <HeroStat label="Today" value={money(brief?.pnl_today ?? 0, { showSign: true })} tone={todayTone} />
            <HeroStat label="Trial day" value={`${brief?.trial?.days_in ?? 0}/${brief?.trial?.total_days ?? 30}`} sub={`${brief?.trial?.days_left ?? 30} left`} />
          </div>
        </div>
      </div>

      {/* ---- Full-width live chart (the star of the page) ---- */}
      <div className="panel col-12">
        <div className="panel-head">
          <h2>📊 Live chart — what the AI sees on <strong>{focal}</strong></h2>
          <div style={{ width: 260 }}>
            <TickerSearch onAdd={(s) => setFocal(s)} placeholder={`${focal} — search any stock or crypto`} />
          </div>
        </div>
        <AnnotatedStrategyChart strategy="adaptive" ticker={focal} height={560} />
      </div>

      {/* ---- Today's macro narrative (dominant theme + beneficiaries + risk) ---- */}
      <div className="col-12"><AuditHealthBanner /></div>
      <div className="col-12"><ResearchDigest /></div>
      <div className="col-12"><LessonsLearned /></div>
      <div className="col-12"><MetricsCard /></div>
      <div className="col-12"><CohortHeatWidget /></div>
      <div className="col-12"><NarrativeStrip /></div>

      {/* ---- Options-edge widgets (Heatseeker GEX + Flowseeker) ---- */}
      <div className="col-4"><GexMini symbol="SPY" /></div>
      <div className="col-8"><FlowStrip /></div>

      {/* ---- Portfolio risk: macro / concentration / theme overlap ---- */}
      <div className="col-12"><PortfolioRiskWidget /></div>

      {/* ---- Holdings + readiness ---- */}
      <div className="col-6"><Holdings positions={brief?.positions} /></div>
      <div className="col-6"><Readiness readiness={brief?.readiness} /></div>

      {/* ---- Strategy Scout (staged recommendation) ---- */}
      <div className="col-12"><StrategyScout ticker={focal} /></div>

      {/* ---- AI ratings ---- */}
      <div className="col-12"><AIPicks tickers={brief?.tickers} /></div>

      {/* ---- Plain-English activity ---- */}
      <div className="col-12"><PlainActivityFeed /></div>
    </div>
  );
}
