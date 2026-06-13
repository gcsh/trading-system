/**
 * CuratedRules — P2.2 institutional guardrail audit page.
 *
 * Shows the static rule catalog (with citations) plus an interactive
 * "what fires for ___" preview so operator can probe scenarios.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { useStrategies } from '../hooks/useStrategies.js';

const SEVERITY_COLOR = {
  alert: '#ff5d5d',
  warn: '#ffd84d',
  info: '#5dc6ff',
};

const REGIMES = ['unknown', 'trending', 'ranging', 'volatile', 'mean_reverting'];

function RuleCard({ rule, isFiring }) {
  const color = SEVERITY_COLOR[rule.severity] || 'var(--muted)';
  return (
    <div className="panel" style={{
      padding: 14, borderLeft: `4px solid ${color}`,
      opacity: isFiring === false ? 0.4 : 1,
      background: isFiring ? 'var(--panel-2)' : undefined,
    }}>
      <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
        <span style={{
          fontSize: 9, padding: '2px 8px', borderRadius: 10,
          background: color + '22', color: color,
          fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          {rule.severity}
        </span>
        <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text)' }}>
          {rule.rule_id}
        </div>
        {isFiring && (
          <span style={{
            marginLeft: 'auto', fontSize: 10, fontWeight: 700,
            color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.05em',
          }}>
            🔥 fires
          </span>
        )}
      </div>
      <div style={{ marginTop: 6, fontSize: 13, color: 'var(--text-soft)' }}>
        {rule.pattern}
      </div>
      <div style={{
        marginTop: 8, fontSize: 11, color: 'var(--muted)',
        fontStyle: 'italic', paddingLeft: 8,
        borderLeft: '2px solid var(--border)',
      }}>
        {rule.citation}
      </div>
      <div className="row" style={{ marginTop: 8, gap: 12, fontSize: 11 }}>
        <span>
          <span style={{ color: 'var(--muted)' }}>Action: </span>
          <strong>{rule.suggested_action}</strong>
        </span>
        <span>
          <span style={{ color: 'var(--muted)' }}>Size×: </span>
          <strong>{rule.size_multiplier}</strong>
        </span>
        {Object.keys(rule.condition_keys || {}).length > 0 && (
          <span style={{ color: 'var(--muted)' }}>
            {Object.entries(rule.condition_keys).map(([k, v]) => `${k}=${v}`).join(' · ')}
          </span>
        )}
      </div>
    </div>
  );
}


export default function CuratedRules() {
  const strategies = useStrategies();
  const [catalog, setCatalog] = useState(null);
  const [err, setErr] = useState(null);

  // Probe form state.
  const [strategy, setStrategy] = useState('cash_secured_put');
  const [regimeTrend, setRegimeTrend] = useState('unknown');
  const [earningsDays, setEarningsDays] = useState('');
  const [ivRank, setIvRank] = useState('');
  const [vix, setVix] = useState('');
  const [dayOfWeek, setDayOfWeek] = useState('');
  const [ycInverted, setYcInverted] = useState(false);

  const [matches, setMatches] = useState([]);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    fetch('/journal/curated')
      .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setCatalog)
      .catch((e) => setErr(String(e)));
  }, []);

  // Probe every time the form changes (debounced).
  useEffect(() => {
    setProbing(true);
    const params = new URLSearchParams({
      strategy, regime_trend: regimeTrend,
    });
    if (earningsDays !== '') params.set('earnings_days', earningsDays);
    if (ivRank !== '') params.set('iv_rank', ivRank);
    if (vix !== '') params.set('vix', vix);
    if (dayOfWeek) params.set('day_of_week', dayOfWeek);
    if (ycInverted) params.set('yield_curve_inverted', 'true');
    const t = setTimeout(() => {
      fetch(`/journal/curated/applicable?${params}`)
        .then((r) => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
        .then((d) => setMatches((d.matches || []).map((m) => m.condition_keys?.rule_id).filter(Boolean)))
        .catch(() => setMatches([]))
        .finally(() => setProbing(false));
    }, 200);
    return () => clearTimeout(t);
  }, [strategy, regimeTrend, earningsDays, ivRank, vix, dayOfWeek, ycInverted]);

  const firingSet = useMemo(() => new Set(matches), [matches]);

  if (err) return <div className="empty">curated endpoint error: {err}</div>;
  if (!catalog) return <div className="empty">Loading curated rules…</div>;

  return (
    <div>
      <div className="panel" style={{ padding: 14, marginBottom: 16 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)',
                            textTransform: 'uppercase', letterSpacing: '0.05em',
                            fontWeight: 600, marginBottom: 8 }}>
          Probe — which rules fire for this context?
        </div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
          <label>
            Strategy:&nbsp;
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {strategies.map(({ slug, label }) => (
                <option key={slug} value={slug}>{label}</option>
              ))}
            </select>
          </label>
          <label>
            Regime:&nbsp;
            <select value={regimeTrend} onChange={(e) => setRegimeTrend(e.target.value)}>
              {REGIMES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label>
            Earnings days:&nbsp;
            <input type="number" value={earningsDays} placeholder="e.g. 5"
                   onChange={(e) => setEarningsDays(e.target.value)}
                   style={{ width: 70 }} />
          </label>
          <label>
            IV rank:&nbsp;
            <input type="number" value={ivRank} placeholder="0–100"
                   onChange={(e) => setIvRank(e.target.value)}
                   style={{ width: 70 }} />
          </label>
          <label>
            VIX:&nbsp;
            <input type="number" value={vix} placeholder="e.g. 22"
                   onChange={(e) => setVix(e.target.value)}
                   style={{ width: 70 }} />
          </label>
          <label>
            Day:&nbsp;
            <select value={dayOfWeek} onChange={(e) => setDayOfWeek(e.target.value)}>
              <option value="">—</option>
              {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'].map((d) =>
                <option key={d} value={d}>{d}</option>
              )}
            </select>
          </label>
          <label style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={ycInverted}
                   onChange={(e) => setYcInverted(e.target.checked)} />
            <span style={{ marginLeft: 4 }}>2s10s inverted</span>
          </label>
          <span style={{ marginLeft: 'auto', color: 'var(--muted)' }}>
            {probing ? 'probing…' : `${matches.length} of ${catalog.count} fire`}
          </span>
        </div>
      </div>

      <div style={{ display: 'grid', gap: 12,
                       gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))' }}>
        {catalog.rules.map((r) => (
          <RuleCard key={r.rule_id} rule={r}
                    isFiring={firingSet.size > 0 ? firingSet.has(r.rule_id) : null} />
        ))}
      </div>

      <div style={{ marginTop: 12, fontSize: 10, color: 'var(--muted-2)' }}>
        Rules ride ON TOP of organic journal lessons; most-penalising wins on conflicts.
      </div>
    </div>
  );
}
