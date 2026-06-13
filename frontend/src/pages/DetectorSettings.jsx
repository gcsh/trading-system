/**
 * MITS Phase 3 — operator-facing Detector control plane.
 *
 * Lists every registered detector grouped by family. Per-detector:
 *   - enable/disable toggle (PATCH /detectors/{name})
 *   - "Configure" button → modal with param sliders sourced from
 *     `default_params` + current overrides
 *
 * Family-level bulk actions: enable / disable all in the family.
 * Pine import: textarea → POST /detectors/import-pine.
 *
 * The wrapping page hosts this in SettingsHub.
 */
import React, { useEffect, useMemo, useState } from 'react';

const FAMILY_LABELS = {
  candlesticks: 'Candlesticks',
  price_action: 'Price Action',
  market_structure: 'Market Structure',
  liquidity: 'Liquidity',
  vwap: 'VWAP',
  volume_profile: 'Volume Profile',
  options_intel: 'Options Intel',
  uncategorized: 'Other',
};

const FAMILY_ORDER = [
  'candlesticks',
  'price_action',
  'market_structure',
  'liquidity',
  'vwap',
  'volume_profile',
  'options_intel',
  'uncategorized',
];

const FAMILY_COLORS = {
  candlesticks: '#5b9bd5',
  price_action: '#71c587',
  market_structure: '#a073d4',
  liquidity: '#e89a4c',
  vwap: '#5fc9ce',
  volume_profile: '#e6c95f',
  options_intel: '#e8606e',
  uncategorized: '#9aa5b2',
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${path} -> ${res.status} ${text}`);
  }
  return res.json();
}

function ParamModal({ detector, onClose, onSave }) {
  const initial = useMemo(() => ({
    ...(detector?.default_params || {}),
    ...(detector?.params || {}),
  }), [detector]);
  const [draft, setDraft] = useState(initial);
  useEffect(() => { setDraft(initial); }, [initial]);

  if (!detector) return null;
  const defaults = detector.default_params || {};
  const keys = Object.keys(defaults);

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(13,20,36,0.55)',
        zIndex: 200, display: 'grid', placeItems: 'center', padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--panel)',
          border: '1px solid var(--border)',
          borderRadius: 14, width: 'min(540px, 96vw)',
          padding: 18, boxShadow: 'var(--shadow-md)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <h3 style={{ margin: 0, flex: 1 }}>Configure · {detector.name}</h3>
          <button className="btn small" onClick={onClose}>Close</button>
        </div>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
          {detector.description || 'No description.'}
        </div>
        {keys.length === 0 ? (
          <div style={{ fontSize: 13, color: 'var(--muted)' }}>
            This detector has no operator-tunable parameters.
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 10 }}>
            {keys.map((k) => {
              const defVal = defaults[k];
              const curVal = draft[k];
              const isNum = typeof defVal === 'number';
              return (
                <label key={k} style={{ display: 'grid', gridTemplateColumns: '160px 1fr', gap: 10, alignItems: 'center', fontSize: 13 }}>
                  <span style={{ color: 'var(--text-soft)', fontWeight: 600 }}>{k}</span>
                  <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <input
                      type={isNum ? 'number' : 'text'}
                      step={isNum ? 'any' : undefined}
                      value={curVal == null ? '' : curVal}
                      onChange={(e) => {
                        const v = e.target.value;
                        setDraft((d) => ({ ...d, [k]: isNum ? (v === '' ? '' : Number(v)) : v }));
                      }}
                      style={{
                        flex: 1, padding: '6px 8px', border: '1px solid var(--border)',
                        background: 'var(--panel-2)', color: 'var(--text)', borderRadius: 6,
                        fontFamily: 'inherit',
                      }}
                    />
                    <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                      default {String(defVal)}
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
        )}
        <div style={{ marginTop: 14, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="btn small" onClick={() => setDraft({ ...defaults })}>Reset to defaults</button>
          <button className="btn small primary" onClick={() => onSave(draft)}>Save</button>
        </div>
      </div>
    </div>
  );
}


function PineImportPanel({ onImported }) {
  const [name, setName] = useState('');
  const [source, setSource] = useState('');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const run = async () => {
    if (!name.trim() || !source.trim()) return;
    setBusy(true); setErr(null);
    try {
      const r = await api('/detectors/import-pine', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), source }),
      });
      setResult(r);
      if (onImported) onImported();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel" style={{ marginTop: 16, padding: 14 }}>
      <h3 style={{ marginTop: 0 }}>Import Pine Script</h3>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
        Paste a TradingView Pine script. The translator extracts recognized
        rules (MACD crosses, RSI thresholds, price vs MA) and persists the
        script as a custom detector entry for audit.
      </div>
      <div style={{ display: 'grid', gap: 8, marginBottom: 8 }}>
        <input
          placeholder="Detector name (e.g. my_macd_rsi)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{
            padding: '8px 10px', border: '1px solid var(--border)',
            background: 'var(--panel-2)', color: 'var(--text)', borderRadius: 6,
          }}
        />
        <textarea
          placeholder="Paste Pine Script here..."
          value={source}
          onChange={(e) => setSource(e.target.value)}
          style={{ minHeight: 120 }}
        />
      </div>
      <div className="row" style={{ gap: 6 }}>
        <button className="btn small primary" disabled={busy || !name.trim() || !source.trim()} onClick={run}>
          {busy ? 'Importing...' : 'Import'}
        </button>
        {err && <span className="pill warning">{err}</span>}
      </div>
      {result && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            <span className="pill on">imported</span>
            {(result.recognized || []).map((r, i) => (
              <span key={i} className="pill info">{r}</span>
            ))}
          </div>
          {result.limitations && (
            <div style={{ marginTop: 6, color: 'var(--muted)' }}>
              {result.limitations}
            </div>
          )}
        </div>
      )}
    </div>
  );
}


function ScorecardStrip({ name }) {
  const [card, setCard] = useState(null);
  useEffect(() => {
    let alive = true;
    fetch(`/detectors/${encodeURIComponent(name)}/scorecard?window=30`)
      .then((r) => (r.ok ? r.json() : null))
      .then((b) => { if (alive) setCard(b); })
      .catch(() => {});
    return () => { alive = false; };
  }, [name]);
  if (!card) return null;
  const n = card.closed_trades || 0;
  if (n === 0) {
    return <span style={{ fontSize: 10, color: 'var(--muted)' }}>no trades (30d)</span>;
  }
  const wr = card.win_rate != null ? `${(card.win_rate * 100).toFixed(0)}%` : '—';
  const pnl = Number(card.realized_pnl_dollars || 0);
  const sign = pnl >= 0 ? '+' : '-';
  return (
    <span style={{ fontSize: 10, color: 'var(--muted)' }}>
      {n} · WR {wr} · <span style={{ color: pnl >= 0 ? 'var(--accent-2)' : 'var(--danger-2)' }}>
        {sign}${Math.abs(pnl).toFixed(0)}
      </span>
    </span>
  );
}


function SuggestionsBanner({ onAction }) {
  const [pending, setPending] = useState([]);
  const load = async () => {
    try {
      const r = await fetch('/detector-suggestions?status=pending');
      if (r.ok) setPending(await r.json());
    } catch (_) {}
  };
  useEffect(() => { load(); }, []);
  if (!pending || pending.length === 0) return null;
  const act = async (id, kind) => {
    await api(`/detector-suggestions/${id}/${kind}`, { method: 'POST' });
    await load();
    if (onAction) onAction();
  };
  return (
    <div className="panel" style={{
      padding: 12, marginBottom: 10,
      borderLeft: '4px solid var(--warning)',
      background: 'var(--panel-2)',
    }}>
      <div style={{ fontWeight: 700, marginBottom: 6 }}>
        {pending.length} detector{pending.length === 1 ? '' : 's'} suggested for review
        based on out-of-sample performance.
      </div>
      <div style={{ display: 'grid', gap: 6 }}>
        {pending.map((s) => (
          <div key={s.id} className="row" style={{ gap: 8, fontSize: 12, alignItems: 'center' }}>
            <span className={s.reason === 'recovered_posterior' ? 'pill on' : 'pill warning'} style={{ fontSize: 10 }}>
              {s.reason === 'recovered_posterior' ? 're-enable' : 'disable'}
            </span>
            <strong>{s.detector_name}</strong>
            <span style={{ color: 'var(--muted)' }}>
              posterior {s.out_of_sample_posterior != null ? s.out_of_sample_posterior.toFixed(2) : '—'} · N {s.sample_size}
            </span>
            <div style={{ flex: 1 }} />
            <button className="btn small primary" onClick={() => act(s.id, 'accept')}>Accept</button>
            <button className="btn small ghost" onClick={() => act(s.id, 'dismiss')}>Dismiss</button>
          </div>
        ))}
      </div>
    </div>
  );
}


export default function DetectorSettings() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null);
  const [expanded, setExpanded] = useState({});

  const load = async () => {
    setLoading(true);
    try {
      const data = await api('/detectors');
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      console.warn('detector load failed', e);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const grouped = useMemo(() => {
    const out = {};
    for (const r of rows) {
      const fam = r.family || 'uncategorized';
      if (!out[fam]) out[fam] = [];
      out[fam].push(r);
    }
    for (const fam of Object.keys(out)) {
      out[fam].sort((a, b) => a.name.localeCompare(b.name));
    }
    return out;
  }, [rows]);

  const orderedFamilies = useMemo(() => {
    const present = Object.keys(grouped);
    return [
      ...FAMILY_ORDER.filter((f) => present.includes(f)),
      ...present.filter((f) => !FAMILY_ORDER.includes(f)),
    ];
  }, [grouped]);

  const toggleFamily = (fam) => {
    setExpanded((e) => ({ ...e, [fam]: !e[fam] }));
  };

  const patchOne = async (name, body) => {
    await api(`/detectors/${encodeURIComponent(name)}`, {
      method: 'PATCH', body: JSON.stringify(body),
    });
    await load();
  };

  const bulkToggle = async (fam, enabled) => {
    const items = grouped[fam] || [];
    await Promise.all(items.map((d) => api(
      `/detectors/${encodeURIComponent(d.name)}`,
      { method: 'PATCH', body: JSON.stringify({ enabled }) },
    )));
    await load();
  };

  return (
    <div>
      <div className="panel-head" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Detector Settings</h2>
        <span className="panel-sub">
          {rows.length} detector{rows.length === 1 ? '' : 's'} ·
          {' '}{rows.filter((r) => r.enabled).length} enabled
        </span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
        Toggle individual detectors or whole families. Disabled detectors
        are excluded from the knowledge graph, the live engine, and the
        analysis pages. Their persisted observations stay on disk —
        re-enabling restores them automatically.
      </div>

      <SuggestionsBanner onAction={load} />

      {loading ? (
        <div style={{ padding: 24 }}>Loading detectors...</div>
      ) : (
        orderedFamilies.map((fam) => {
          const items = grouped[fam] || [];
          const enabled = items.filter((d) => d.enabled).length;
          const total = items.length;
          const isOpen = expanded[fam] !== false;  // default open
          return (
            <div key={fam} className="panel" style={{ marginBottom: 10, padding: 0 }}>
              <div
                onClick={() => toggleFamily(fam)}
                style={{
                  padding: '10px 14px', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', gap: 10,
                  borderLeft: `4px solid ${FAMILY_COLORS[fam] || '#9aa5b2'}`,
                }}
              >
                <span style={{ fontWeight: 700, fontSize: 14 }}>
                  {FAMILY_LABELS[fam] || fam}
                </span>
                <span className="pill" style={{ background: 'var(--panel-2)', color: 'var(--muted)' }}>
                  {enabled}/{total} enabled
                </span>
                <div style={{ flex: 1 }} />
                <button
                  className="btn small"
                  onClick={(e) => { e.stopPropagation(); bulkToggle(fam, true); }}
                >
                  Enable all
                </button>
                <button
                  className="btn small ghost"
                  onClick={(e) => { e.stopPropagation(); bulkToggle(fam, false); }}
                >
                  Disable all
                </button>
                <span style={{ color: 'var(--muted)', marginLeft: 6 }}>{isOpen ? '▾' : '▸'}</span>
              </div>
              {isOpen && (
                <div style={{ padding: '0 14px 12px' }}>
                  <div style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
                    gap: 8,
                  }}>
                    {items.map((d) => (
                      <div
                        key={d.name}
                        title={d.description || ''}
                        style={{
                          display: 'flex', alignItems: 'center', gap: 8,
                          padding: '8px 10px',
                          background: 'var(--panel-2)',
                          border: '1px solid var(--border)',
                          borderRadius: 8,
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={!!d.enabled}
                          onChange={(e) => patchOne(d.name, { enabled: e.target.checked })}
                        />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13, fontWeight: 600 }}>
                            {d.name}
                          </div>
                          <ScorecardStrip name={d.name} />
                        </div>
                        {d.source === 'pine_import' && (
                          <span className="pill purple" style={{ fontSize: 10 }}>pine</span>
                        )}
                        <button
                          className="btn small ghost"
                          onClick={() => setEditing(d)}
                          title="Configure parameters"
                        >
                          ⚙
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })
      )}

      <PineImportPanel onImported={load} />

      {editing && (
        <ParamModal
          detector={editing}
          onClose={() => setEditing(null)}
          onSave={async (params) => {
            // Normalize empty strings to drop (keep defaults).
            const cleaned = {};
            for (const [k, v] of Object.entries(params)) {
              if (v === '' || v == null) continue;
              cleaned[k] = v;
            }
            await patchOne(editing.name, { params: cleaned });
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}
