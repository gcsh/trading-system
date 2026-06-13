/* MITS Phase 19 Cluster B — Theory Studio v2 (/v2/theory).
 *
 * Multi-theory overlay sandbox for one ticker. Reuses:
 *   • OHLCChart        — bars from /analysis/{ticker}?window=…
 *   • TheoryOverlay    — legend + buildOverlays() (normalizes /theories/multi shape)
 *   • TheorySelector   — chip multi-select sourced from /theories
 *
 * Layout:
 *   HEADER   ticker + window dropdown
 *   ROW 1    KPI strip: theories on, signals returned, lines drawn, last computed
 *   ROW 2    TheorySelector chip row
 *   ROW 3    OHLCChart (bars + overlays) — main canvas
 *   ROW 4    Signal table from annotations[*].signals[]
 *
 * Endpoints (verified):
 *   GET /theories                                — { theories[], windows[] }
 *   GET /analysis/{ticker}?window=…              — { bars[], regime_vector, … }
 *   GET /theories/multi/{ticker}?theories=&window= — { annotations: {theory: {lines,zones,markers,signals}} }
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Card, Stat, Pill, Section, Table, EmptyState, AlertBanner, KPIWidget,
} from '../../design/Components.jsx';
import OHLCChart from '../components/OHLCChart.jsx';
import TheoryOverlay, { buildOverlays } from '../components/TheoryOverlay.jsx';
import TheorySelector from '../components/TheorySelector.jsx';

const DEFAULT_TICKERS = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'SPY', 'QQQ', 'AMZN', 'META', 'GOOGL', 'AMD'];
const DEFAULT_WINDOW = '3m';
const WINDOW_TO_ANALYSIS = {
  '1m': 'today',
  '3m': '5d',          // analyser windows are tighter; closest sensible map
  '6m': 'all',
  '1y': 'all',
  '2y': 'all',
  '5y': 'all',
  'max': 'all',
};
const DEFAULT_THEORIES = ['price_action', 'bollinger', 'ma_ribbon'];

function fmtAgo(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export default function TheoryStudio() {
  const [ticker, setTicker] = useState('AAPL');
  const [window, setWindow] = useState(DEFAULT_WINDOW);
  const [availTheories, setAvailTheories] = useState([]);
  const [availWindows, setAvailWindows] = useState(['1m','3m','6m','1y','2y','5y','max']);
  const [selected, setSelected] = useState(DEFAULT_THEORIES);
  const [analysis, setAnalysis] = useState(null);
  const [analysisErr, setAnalysisErr] = useState(null);
  const [theoryPayload, setTheoryPayload] = useState(null);
  const [theoryErr, setTheoryErr] = useState(null);
  const [theoryLoading, setTheoryLoading] = useState(false);
  const [lastComputedAt, setLastComputedAt] = useState(null);

  /* ── fetch /theories list (once) ─────────────────────────────────── */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch('/theories');
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setAvailTheories(Array.isArray(j?.theories) ? j.theories : []);
          if (Array.isArray(j?.windows) && j.windows.length) {
            setAvailWindows(j.windows);
          }
        }
      } catch (_) { /* keep defaults */ }
    })();
    return () => { cancelled = true; };
  }, []);

  /* ── fetch /analysis/{ticker} for bars ───────────────────────────── */
  useEffect(() => {
    if (!ticker) return;
    let cancelled = false;
    const aWin = WINDOW_TO_ANALYSIS[window] || 'all';
    (async () => {
      try {
        const r = await fetch(
          `/analysis/${encodeURIComponent(ticker)}?window=${encodeURIComponent(aWin)}`,
        );
        if (!r.ok) throw new Error(`${r.status}`);
        const j = await r.json();
        if (!cancelled) {
          setAnalysis(j);
          setAnalysisErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setAnalysisErr(e.message || 'analysis fetch failed');
          setAnalysis(null);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [ticker, window]);

  /* ── fetch /theories/multi/{ticker} when selection changes ───────── */
  useEffect(() => {
    if (!ticker) return;
    if (!selected.length) {
      setTheoryPayload(null);
      return;
    }
    let cancelled = false;
    setTheoryLoading(true);
    (async () => {
      try {
        const url = `/theories/multi/${encodeURIComponent(ticker)}`
                  + `?window=${encodeURIComponent(window)}`
                  + `&theories=${selected.map(encodeURIComponent).join(',')}`;
        const r = await fetch(url);
        if (!r.ok) {
          const txt = await r.text();
          throw new Error(`${r.status}: ${txt.slice(0, 80)}`);
        }
        const j = await r.json();
        if (!cancelled) {
          setTheoryPayload(j);
          setTheoryErr(null);
          setLastComputedAt(new Date().toISOString());
        }
      } catch (e) {
        if (!cancelled) {
          setTheoryErr(e.message || 'theory fetch failed');
          setTheoryPayload(null);
        }
      } finally {
        if (!cancelled) setTheoryLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [ticker, window, selected]);

  /* ── derive overlay dict + signal rows ───────────────────────────── */
  const bars = useMemo(() => {
    return Array.isArray(analysis?.bars) ? analysis.bars : [];
  }, [analysis]);

  const overlays = useMemo(() => {
    return buildOverlays(theoryPayload || {}, {});
  }, [theoryPayload]);

  const activeTheories = useMemo(() => {
    if (theoryPayload?.annotations) {
      return Object.keys(theoryPayload.annotations).filter(
        (k) => theoryPayload.annotations[k],
      );
    }
    return [];
  }, [theoryPayload]);

  const signalRows = useMemo(() => {
    if (!theoryPayload?.annotations) return [];
    const out = [];
    for (const [theory, ann] of Object.entries(theoryPayload.annotations)) {
      if (!ann || !Array.isArray(ann.signals)) continue;
      for (const s of ann.signals) {
        out.push({
          __key: `${theory}-${s.ts || ''}-${s.signal_type || ''}-${out.length}`,
          ts: s.ts || s.timestamp || '',
          theory,
          signal_type: s.signal_type || s.type || '—',
          action: s.action || s.direction || '—',
          confidence: s.confidence != null ? s.confidence : null,
          notes: s.notes || s.label || s.pattern_name || '',
        });
      }
    }
    // Most-recent first.
    out.sort((a, b) => {
      const ta = Date.parse(a.ts) || 0;
      const tb = Date.parse(b.ts) || 0;
      return tb - ta;
    });
    return out;
  }, [theoryPayload]);

  /* ── KPI numbers ─────────────────────────────────────────────────── */
  const linesCount = useMemo(() => {
    return (overlays.trendLines?.length || 0)
         + (overlays.priceLines?.length || 0);
  }, [overlays]);

  const sigCols = useMemo(() => [
    { key: 'ts',         label: 'When', mono: true },
    { key: 'theory',     label: 'Theory', mono: true },
    { key: 'signal_type',label: 'Signal', mono: true },
    { key: 'action',     label: 'Action', mono: true },
    { key: 'conf',       label: 'Conf', mono: true, align: 'right' },
    { key: 'notes',      label: 'Notes' },
  ], []);

  const sigRowsDisplay = useMemo(() => {
    return signalRows.slice(0, 60).map((r) => ({
      __key: r.__key,
      ts:    r.ts ? r.ts.replace('T', ' ').slice(0, 19) : '—',
      theory: r.theory,
      signal_type: r.signal_type,
      action: (
        <Pill tone={
          r.action === 'buy' || r.action === 'long' ? 'success'
          : r.action === 'sell' || r.action === 'short' ? 'error'
          : 'neutral'
        }>
          {String(r.action).toUpperCase()}
        </Pill>
      ),
      conf: r.confidence != null ? `${(r.confidence * 100).toFixed(0)}%` : '—',
      notes: r.notes || '—',
    }));
  }, [signalRows]);

  /* ── render ──────────────────────────────────────────────────────── */
  return (
    <div className="v2-ths">
      {/* ─── HEADER ─── */}
      <div className="v2-ths-header">
        <div className="v2-ths-header__main">
          <h1 className="v2-ths-header__title">Theory Studio</h1>
          <span className="dim">
            multi-overlay technical analysis sandbox · real OHLC + 23 theories
          </span>
        </div>
        <div className="v2-ths-header__controls">
          <label className="v2-ths-ctrl">
            <span className="v2-ths-ctrl__l">ticker</span>
            <select className="v2-ths-ctrl__input mono"
                    value={ticker}
                    onChange={(e) => setTicker(e.target.value.toUpperCase())}>
              {DEFAULT_TICKERS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <label className="v2-ths-ctrl">
            <span className="v2-ths-ctrl__l">window</span>
            <select className="v2-ths-ctrl__input mono"
                    value={window}
                    onChange={(e) => setWindow(e.target.value)}>
              {availWindows.map((w) => (
                <option key={w} value={w}>{w}</option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {(analysisErr || theoryErr) && (
        <AlertBanner severity="warning">
          {analysisErr && <>/analysis/{ticker} failed: {analysisErr}. </>}
          {theoryErr   && <>/theories/multi failed: {theoryErr}.</>}
        </AlertBanner>
      )}

      {/* ─── ROW 1: KPI ─── */}
      <Section title="Snapshot"
               subtitle={`${ticker} · ${window} · ${selected.length} theories selected`}>
        <div className="v2-ths-kpi-row">
          <KPIWidget icon="◉"
                     label="Theories on"
                     value={activeTheories.length}
                     trend="flat"
                     trendText={`${selected.length} requested`}
                     hint="number of theories with overlays returned" />
          <KPIWidget icon="◉"
                     label="Signals"
                     value={signalRows.length}
                     trend={signalRows.length > 0 ? 'up' : 'flat'}
                     trendText="across all selected theories"
                     hint="entries in annotations[*].signals[]" />
          <KPIWidget icon="◉"
                     label="Lines drawn"
                     value={linesCount}
                     trend="flat"
                     trendText={`${overlays.markers?.length || 0} markers`}
                     hint="trend lines + horizontal price lines" />
          <KPIWidget icon="◉"
                     label="Last computed"
                     value={fmtAgo(lastComputedAt)}
                     trend="flat"
                     trendText={theoryLoading ? 'computing…' : 'cached'}
                     hint="time since last /theories/multi call" />
        </div>
      </Section>

      {/* ─── ROW 2: Theory chip selector ─── */}
      <Section title="Theories" subtitle="pick up to 5 — chip toggles overlay">
        <Card>
          <TheorySelector
            theories={availTheories}
            selected={selected}
            onChange={setSelected}
            max={5}
          />
        </Card>
      </Section>

      {/* ─── ROW 3: Chart ─── */}
      <Section title="Chart"
               subtitle={`${bars.length} bars from /analysis (window=${WINDOW_TO_ANALYSIS[window]})`}>
        <Card>
          {bars.length === 0 ? (
            <EmptyState
              icon="∅"
              message={analysisErr
                ? `Analysis unavailable: ${analysisErr}`
                : 'No bars returned for this window.'}
            />
          ) : (
            <OHLCChart
              bars={bars}
              overlays={overlays}
              ticker={ticker}
              height={520}
            />
          )}
          <div style={{ marginTop: 10 }}>
            {activeTheories.length ? (
              <TheoryOverlay
                activeTheories={activeTheories}
                ticker={ticker}
                onTheoryToggle={(t) =>
                  setSelected(selected.filter((x) => x !== t))} />
            ) : (
              <TheoryOverlay
                ticker={ticker}
                empty
                emptyMessage={
                  theoryErr
                    ? `Theory overlays unavailable: ${theoryErr}`
                    : selected.length === 0
                      ? 'No theories selected — tap a chip above to overlay.'
                      : 'No annotations returned for this window — try a longer window.'
                }
              />
            )}
          </div>
        </Card>
      </Section>

      {/* ─── ROW 4: Signal table ─── */}
      <Section title="Signal log"
               subtitle={`${signalRows.length} signals${signalRows.length > 60 ? ' (showing latest 60)' : ''}`}>
        <Card>
          {sigRowsDisplay.length === 0 ? (
            <EmptyState icon="∅"
                        message={selected.length === 0
                          ? 'No theories selected — pick at least one to see signals.'
                          : 'No signals produced for this window. Theories may still render lines/markers.'} />
          ) : (
            <Table cols={sigCols} rows={sigRowsDisplay} striped sticky />
          )}
        </Card>
      </Section>

      <style>{`
        .v2-ths-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-end;
          gap: 16px;
          padding-bottom: 16px;
          border-bottom: 1px solid var(--border-subtle);
          margin-bottom: var(--space-4);
          flex-wrap: wrap;
        }
        .v2-ths-header__title {
          margin: 0; font-size: var(--font-size-2xl);
          font-weight: 800; letter-spacing: -0.02em;
          color: var(--text-primary);
        }
        .v2-ths-header__main { display: flex; flex-direction: column; gap: 4px; }
        .v2-ths-header__controls {
          display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap;
        }
        .v2-ths-ctrl {
          display: flex; flex-direction: column; gap: 4px;
        }
        .v2-ths-ctrl__l {
          color: var(--text-tertiary);
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-size: 10px;
        }
        .v2-ths-ctrl__input {
          background: var(--bg-tertiary);
          border: 1px solid var(--border-default);
          color: var(--text-primary);
          padding: 6px 10px;
          font-size: 12px;
          border-radius: var(--radius-sm);
          min-width: 120px;
        }
        .v2-ths-kpi-row {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: var(--space-4);
        }
        @media (max-width: 1100px) {
          .v2-ths-kpi-row { grid-template-columns: repeat(2, 1fr); }
        }
        .dim { color: var(--text-tertiary); }
      `}</style>
    </div>
  );
}
