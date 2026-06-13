/**
 * Stage-20-UI · Earnings Call Intelligence.
 *
 * Per-ticker view of parsed earnings call intel:
 *   • guidance change (improved / maintained / reduced / withdrawn)
 *   • margin trajectory (expanding / stable / contracting)
 *   • management tone (confident / cautious / mixed / neutral)
 *   • key quotes + forward-looking statements
 *
 * Backend: /earnings-intel/{ticker} (latest), /earnings-intel/{ticker}/history.
 */
import React, { useCallback, useEffect, useState } from 'react';

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const GUIDANCE_PILL = {
  improved: { className: 'pill on', text: '↑ improved' },
  raised: { className: 'pill on', text: '↑ raised' },
  maintained: { className: 'pill info', text: '→ maintained' },
  first_time: { className: 'pill info', text: 'first-time' },
  reduced: { className: 'pill danger', text: '↓ reduced' },
  lowered: { className: 'pill danger', text: '↓ lowered' },
  withdrawn: { className: 'pill danger', text: '⛔ withdrawn' },
  none: { className: 'pill off', text: 'no guidance' },
};

const MARGIN_PILL = {
  expanding: { className: 'pill on', text: 'expanding' },
  stable: { className: 'pill info', text: 'stable' },
  contracting: { className: 'pill danger', text: 'contracting' },
  'n/a': { className: 'pill off', text: 'n/a' },
};

const TONE_PILL = {
  confident: { className: 'pill on', text: '😎 confident' },
  cautious: { className: 'pill warn', text: '⚠️ cautious' },
  mixed: { className: 'pill purple', text: 'mixed' },
  neutral: { className: 'pill info', text: 'neutral' },
};

function IntelCard({ intel }) {
  if (!intel) {
    return (
      <div className="empty">
        <div className="title">No call intel</div>
        <div className="hint">Either the ticker has no recent 8-K Ex-99.1 release, or the cache is empty.</div>
      </div>
    );
  }
  const gp = GUIDANCE_PILL[intel.guidance_change] || GUIDANCE_PILL.none;
  const mp = MARGIN_PILL[intel.margin_trajectory] || MARGIN_PILL['n/a'];
  const tp = TONE_PILL[intel.management_tone] || TONE_PILL.neutral;

  const filedAt = intel.filed_at
    ? new Date(intel.filed_at).toLocaleDateString(undefined, { dateStyle: 'medium' })
    : '—';

  return (
    <div>
      <div className="row" style={{
        justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16,
      }}>
        <div>
          <div className="accent-intel" style={{
            fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
            fontWeight: 600, marginBottom: 6,
          }}>
            {intel.ticker} · filed {filedAt}
          </div>
          <div style={{ fontSize: 17, fontWeight: 600, lineHeight: 1.4 }}>
            {intel.summary || '—'}
          </div>
        </div>
        <span className={`pill ${intel.source === 'claude' ? 'purple' : 'info'}`}>
          {intel.source}
        </span>
      </div>

      <div className="kpi-row" style={{ marginBottom: 20 }}>
        <div className="kpi">
          <div className="kpi-label">Guidance change</div>
          <div style={{ marginTop: 6 }}>
            <span className={gp.className}>{gp.text}</span>
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Margin trajectory</div>
          <div style={{ marginTop: 6 }}>
            <span className={mp.className}>{mp.text}</span>
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Management tone</div>
          <div style={{ marginTop: 6 }}>
            <span className={tp.className}>{tp.text}</span>
          </div>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        <div>
          <div className="accent-intel section-title">Key quotes</div>
          {(intel.key_quotes || []).length === 0 ? (
            <div style={{ color: 'var(--muted)', fontSize: 13 }}>—</div>
          ) : (
            <ul style={{ paddingLeft: 18, lineHeight: 1.6, color: 'var(--text-soft)', fontSize: 13, margin: 0 }}>
              {intel.key_quotes.map((q, i) => (
                <li key={i} style={{ marginBottom: 8 }}>
                  <em>"{q}"</em>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <div className="accent-data section-title">Forward-looking</div>
          {(intel.forward_looking || []).length === 0 ? (
            <div style={{ color: 'var(--muted)', fontSize: 13 }}>—</div>
          ) : (
            <ul style={{ paddingLeft: 18, lineHeight: 1.6, color: 'var(--text-soft)', fontSize: 13, margin: 0 }}>
              {intel.forward_looking.map((q, i) => (
                <li key={i} style={{ marginBottom: 8 }}>{q}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function HistoryStrip({ history, onSelect, selected }) {
  if (!history.length) return null;
  return (
    <div className="row" style={{ gap: 8, overflowX: 'auto', paddingBottom: 4 }}>
      {history.map((h) => {
        const date = h.filed_at ? new Date(h.filed_at).toLocaleDateString(undefined, { month: 'short', year: '2-digit' }) : '?';
        const tp = TONE_PILL[h.management_tone] || TONE_PILL.neutral;
        const active = selected === h.accession_number;
        return (
          <button
            key={h.accession_number}
            onClick={() => onSelect(h)}
            className={`btn small ${active ? 'primary' : 'ghost'}`}
            style={{
              borderRadius: 8,
              minWidth: 110,
              flexDirection: 'column',
              alignItems: 'flex-start',
              padding: '8px 12px',
              border: active ? '' : '1px solid var(--border)',
            }}
          >
            <div style={{ fontSize: 11, opacity: 0.85, marginBottom: 4 }}>{date}</div>
            <span className={tp.className}>{tp.text}</span>
          </button>
        );
      })}
    </div>
  );
}

export default function EarningsIntel() {
  const [ticker, setTicker] = useState('NVDA');
  const [input, setInput] = useState('NVDA');
  const [latest, setLatest] = useState(null);
  const [history, setHistory] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async (t) => {
    setLoading(true);
    try {
      const [latestRes, histRes] = await Promise.allSettled([
        api(`/earnings-intel/${t}`),
        api(`/earnings-intel/${t}/history?limit=12`),
      ]);
      const l = latestRes.status === 'fulfilled' ? (latestRes.value.intel || null) : null;
      const h = histRes.status === 'fulfilled' ? (histRes.value.history || []) : [];
      setLatest(l);
      setHistory(h);
      setSelected(l);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(ticker); }, [ticker, load]);

  const submit = (e) => {
    e.preventDefault();
    const t = input.trim().toUpperCase();
    if (t) setTicker(t);
  };

  const refresh = async () => {
    setLoading(true);
    try {
      await api(`/earnings-intel/${ticker}/refresh`, { method: 'POST' });
      await load(ticker);
    } catch (e) {
      setError(e.message);
      setLoading(false);
    }
  };

  return (
    <div>
      {error && (
        <div className="panel panel--bear" style={{ marginBottom: 16 }}>
          <div className="accent-bear">{error}</div>
        </div>
      )}

      <div className="hero" style={{ marginBottom: 24 }}>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div className="accent-intel" style={{
              fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
              fontWeight: 600, marginBottom: 6,
            }}>Stage 19 · Earnings Call Intelligence</div>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              What did management actually say?
            </h2>
            <div style={{ color: 'var(--muted)', marginTop: 8, fontSize: 13, maxWidth: 680 }}>
              Parsed from SEC 8-K Ex-99.1 press releases. Heuristic-first; if Anthropic is
              configured, Claude extracts the structured tone + guidance trajectory + key
              quotes. The Devil's Advocate agent reads this to red-team trades the rest
              of the panel might miss.
            </div>
          </div>
          <form onSubmit={submit} className="row" style={{ gap: 8 }}>
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value.toUpperCase())}
              placeholder="ticker"
              style={{ width: 110, textTransform: 'uppercase' }}
            />
            <button type="submit" className="btn small primary">Load</button>
            <button type="button" className="btn small" onClick={refresh} disabled={loading}>
              {loading ? '…' : 'Refresh from EDGAR'}
            </button>
          </form>
        </div>
      </div>

      {history.length > 0 && (
        <div className="panel" style={{ marginBottom: 16 }}>
          <div className="section-title accent-muted">Past quarters</div>
          <HistoryStrip
            history={history}
            onSelect={setSelected}
            selected={selected?.accession_number}
          />
        </div>
      )}

      <div className="panel panel--intel">
        {loading
          ? <div className="empty"><div className="title">Loading…</div></div>
          : <IntelCard intel={selected || latest} />}
      </div>
    </div>
  );
}
