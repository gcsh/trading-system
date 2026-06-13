import React, { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ReferenceLine,
} from 'recharts';
import { money, num, pct, shortDate, shortTime } from '../lib/format.js';
import AnnotatedStrategyChart from './AnnotatedStrategyChart.jsx';
import EvidencePanel from './EvidencePanel.jsx';
import LossAutopsyPanel from './LossAutopsyPanel.jsx';
import { EmptyState } from '../design/Components.jsx';

// Strategies that the EOD pipeline emits but that don't live in the
// strategy registry — rendering them in the AnnotatedStrategyChart would
// always surface "unknown strategy" from the /backtest endpoint.
const NON_REGISTERED_STRATEGIES = new Set(['cf_gate']);

function Stat({ label, value, tone }) {
  return (
    <div style={{ background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px' }}>
      <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</div>
      <div className={tone || ''} style={{ fontWeight: 600, fontFeatureSettings: '"tnum"' }}>{value}</div>
    </div>
  );
}

export default function TradeDetail({ trade: payload, onClose }) {
  const [candles, setCandles] = useState([]);
  const [loading, setLoading] = useState(false);

  // The fetcher now passes the new /detail payload {trade, decision, features,
  // execution, audit}. Older callers passed the raw trade row — accept both.
  const trade = payload && payload.trade ? payload.trade : payload;
  const decision = payload && payload.decision ? payload.decision : null;
  const features = payload && payload.features ? payload.features : null;
  const execution = payload && payload.execution ? payload.execution : null;
  const audit = payload && payload.audit ? payload.audit : null;

  useEffect(() => {
    if (!trade) return;
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const r = await fetch(`/market/candles/${encodeURIComponent(trade.ticker)}?period=5d&interval=15m`);
        if (r.ok && active) setCandles(await r.json());
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [trade]);

  if (!trade) return null;

  const detail = trade.detail || {};
  const snap = detail.snapshot || features || {};
  const isOption = trade.instrument === 'option' || trade.instrument === 'spread';
  const entry = num(trade.price);
  const stop = trade.stop_loss_price;
  const target = trade.take_profit_price;
  const pnl = trade.pnl;

  const series = candles.map((c) => ({ t: c.t, close: c.close }));

  return (
    <div
      onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(13,20,36,0.55)', zIndex: 120, display: 'grid', placeItems: 'center', padding: 24 }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 14,
          width: 'min(1100px, 96vw)', maxHeight: '92vh', overflow: 'auto', padding: 20,
          boxShadow: 'var(--shadow-md)',
        }}
      >
        <div className="panel-head">
          <div>
            <h2 style={{ margin: 0, fontSize: 18 }}>
              {trade.ticker}{' '}
              <span className={`pill ${trade.action.startsWith('BUY') ? 'on' : 'danger'}`} style={{ marginLeft: 6 }}>
                {trade.action.replace(/_/g, ' ')}
              </span>
              {' '}
              <span className="pill purple">{(trade.strategy || '').replace(/_/g, ' ')}</span>
            </h2>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4 }}>
              {shortDate(trade.timestamp)} {shortTime(trade.timestamp)} · {trade.status}
              {trade.paper ? ' · paper' : ' · LIVE'}
            </div>
          </div>
          <button className="btn small" onClick={onClose}>✕ Close</button>
        </div>

        {/* Key facts */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8, marginBottom: 14 }}>
          {isOption ? (
            <>
              <Stat label="Instrument" value={trade.instrument} />
              <Stat label="Type" value={(trade.option_type || '—').toUpperCase()} />
              <Stat label="Strike" value={trade.strike ? money(trade.strike) : '—'} />
              <Stat label="Expiry" value={trade.expiration || '—'} />
              <Stat label="Contracts" value={trade.contracts ?? trade.quantity} />
              <Stat label="Entry px" value={money(entry)} />
            </>
          ) : (
            <>
              <Stat label="Instrument" value="stock" />
              <Stat label="Quantity" value={num(trade.quantity).toFixed(4)} />
              <Stat label="Entry px" value={money(entry)} />
              <Stat label="Notional" value={money(num(trade.quantity) * entry)} />
              <Stat label="Stop" value={stop ? money(stop) : '—'} tone="neg" />
              <Stat label="Target" value={target ? money(target) : '—'} tone="pos" />
            </>
          )}
          <Stat label="Confidence" value={`${(num(trade.confidence) * 100).toFixed(0)}%`} />
          <Stat
            label="Realized P&L"
            value={pnl == null ? 'open' : money(pnl, { showSign: true })}
            tone={pnl == null ? '' : pnl >= 0 ? 'pos' : 'neg'}
          />
        </div>

        {/* Why this trade */}
        <div className="panel" style={{ marginBottom: 14, background: 'var(--panel-2)' }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>
            Why the bot took this trade
          </div>
          <div style={{ fontSize: 14, lineHeight: 1.55, whiteSpace: 'pre-wrap' }}>{detail.signal_reason || trade.reason || '—'}</div>
          {detail.legs && (
            <div style={{ marginTop: 8, fontSize: 12, color: 'var(--muted)' }}>
              Legs: {Array.isArray(detail.legs) ? detail.legs.join(', ') : String(detail.legs)}
            </div>
          )}
        </div>

        {/* MITS Phase 1 — corpus evidence panel for the trade's
            (ticker, strategy) cohort. The strategy slug doubles as a
            pattern hint; the panel falls back to ticker-only top-3 when
            it can't resolve a matching cell. */}
        <div style={{ marginBottom: 14 }}>
          <EvidencePanel
            ticker={trade.ticker}
            pattern={trade.strategy || undefined}
            horizon="1d"
            topN={3}
          />
          {/* When the strategy doesn't map to a pattern, render the
              ticker-only top-3 view as a complement so the operator always
              sees what the corpus knows about this ticker. */}
          {trade.strategy && (
            <div style={{ marginTop: 6 }}>
              <EvidencePanel ticker={trade.ticker} topN={3} horizon="1d" />
            </div>
          )}
        </div>

        {/* Loss Autopsy — only for closed losing trades */}
        <LossAutopsyPanel tradeId={trade.id} pnl={trade.pnl} />

        {/* Audit pill — visible only when an invariant fired on this row */}
        {audit && !audit.ok && (
          <div className="panel" style={{ marginBottom: 14,
              background: 'var(--danger-soft)', borderColor: 'var(--danger)' }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--danger)',
                textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 }}>
              ⚠ Audit violations on this trade row
            </div>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.5 }}>
              {audit.violations.map((v, i) => (
                <li key={i}><strong>{v.name}</strong> — {v.message}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Decision context — regime, grade, win prob at signal time */}
        {decision && (
          <div className="panel" style={{ marginBottom: 14, background: 'var(--panel-2)' }}>
            <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
              Decision context at signal time
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8 }}>
              <Stat label="Regime" value={decision.regime_trend || '—'} />
              <Stat label="Volatility" value={decision.regime_volatility || '—'} />
              <Stat label="Gamma" value={decision.regime_gamma || '—'} />
              <Stat label="Grade" value={decision.grade || '—'} />
              <Stat label="Win prob"
                value={decision.win_probability != null
                  ? `${(decision.win_probability * 100).toFixed(0)}%` : '—'} />
              {decision.outcome_pnl != null && (
                <Stat label="Outcome P&L"
                  value={money(decision.outcome_pnl, { showSign: true })}
                  tone={decision.outcome_pnl >= 0 ? 'pos' : 'neg'} />
              )}
            </div>
          </div>
        )}

        {/* Execution telemetry — fill quality vs the signal-time snapshot */}
        {execution && (
          <div className="panel" style={{ marginBottom: 14, background: 'var(--panel-2)' }}>
            <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
              Execution quality
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8 }}>
              <Stat label="Expected" value={money(execution.expected_price)} />
              <Stat label="Fill" value={money(execution.fill_price)} />
              <Stat label="Slippage"
                value={execution.slippage_bps != null
                  ? `${execution.slippage_bps.toFixed(1)} bps` : '—'}
                tone={execution.is_adverse ? 'neg' : ''} />
              <Stat label="Side" value={execution.side || '—'} />
            </div>
          </div>
        )}

        {/* Chart with entry / stop / target overlaid */}
        <div className="panel" style={{ marginBottom: 14 }}>
          <div className="panel-head">
            <h2>{trade.ticker} · 5D / 15m</h2>
            <span className="panel-sub">entry, stop &amp; target overlaid</span>
          </div>
          <div style={{ height: 280 }}>
            {series.length < 2 ? (
              <div className="empty">
                {loading
                  ? 'Loading chart…'
                  : `No chart data available for ${trade.ticker} on the 5d/15m window queried.`}
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={series} margin={{ top: 8, right: 16, left: 8, bottom: 0 }}>
                  <defs>
                    <linearGradient id="tdFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--info)" stopOpacity={0.25} />
                      <stop offset="100%" stopColor="var(--info)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="t" tickFormatter={shortDate} tick={{ fontSize: 11, fill: 'var(--muted)' }} stroke="var(--border)" minTickGap={48} />
                  <YAxis domain={['auto', 'auto']} tick={{ fontSize: 11, fill: 'var(--muted)' }} tickFormatter={(v) => money(v)} width={80} orientation="right" stroke="var(--border)" />
                  <Tooltip
                    contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={(t) => `${shortDate(t)} ${shortTime(t)}`}
                    formatter={(v) => [money(v), 'Close']}
                  />
                  <Area type="monotone" dataKey="close" stroke="var(--info)" strokeWidth={2} fill="url(#tdFill)" isAnimationActive={false} />
                  <ReferenceLine y={entry} stroke="var(--muted-2)" strokeDasharray="4 3" label={{ value: `entry ${money(entry)}`, fontSize: 10, fill: 'var(--muted)', position: 'insideTopLeft' }} />
                  {stop && <ReferenceLine y={stop} stroke="var(--danger)" strokeDasharray="4 3" label={{ value: `stop ${money(stop)}`, fontSize: 10, fill: 'var(--danger)', position: 'insideBottomLeft' }} />}
                  {target && <ReferenceLine y={target} stroke="var(--accent)" strokeDasharray="4 3" label={{ value: `target ${money(target)}`, fontSize: 10, fill: 'var(--accent)', position: 'insideTopLeft' }} />}
                </ComposedChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        {/* The strategy drawn on the real chart with its trade annotations.
            Skip rendering for synthetic strategies (anything starting with
            'exit' or '_') and for slugs that aren't in the strategy
            registry (e.g. cf_gate) — those always 404 against /backtest. */}
        {(() => {
          const slug = trade.strategy ? String(trade.strategy) : '';
          if (!slug) return null;
          if (slug.startsWith('exit')) return null;
          const isRegisteredStrategy = !slug.startsWith('_')
            && !NON_REGISTERED_STRATEGIES.has(slug);
          if (!isRegisteredStrategy) {
            return (
              <div className="panel" style={{ marginBottom: 14 }}>
                <EmptyState
                  icon="∅"
                  message={`Strategy not in registry — this trade ran on '${slug}', which isn't a registered backtestable strategy.`}
                />
              </div>
            );
          }
          return (
            <div className="panel" style={{ marginBottom: 14 }}>
              <AnnotatedStrategyChart strategy={slug.replace(/^adaptive→/, '')} ticker={trade.ticker} height={420} />
            </div>
          );
        })()}

        {/* Indicator snapshot at decision time */}
        <div className="panel" style={{ background: 'var(--panel-2)' }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
            Indicator snapshot when the signal fired
          </div>
          {Object.keys(snap).length === 0 ? (
            <div style={{ color: 'var(--muted)', fontSize: 13 }}>
              {trade.fill_snapshot_json
                ? 'No indicator snapshot stored for this trade — fill snapshot is available but indicator features were not persisted.'
                : 'This trade predates the fill-snapshot tracking layer (post-Phase 17.B trades show full snapshots).'}
            </div>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 8 }}>
              {snap.rsi != null && <Stat label="RSI" value={num(snap.rsi).toFixed(1)} />}
              {snap.macd != null && <Stat label="MACD" value={num(snap.macd).toFixed(3)} tone={num(snap.macd) > num(snap.macd_signal) ? 'pos' : 'neg'} />}
              {snap.macd_signal != null && <Stat label="MACD signal" value={num(snap.macd_signal).toFixed(3)} />}
              {snap.ma50 != null && <Stat label="MA50" value={money(snap.ma50)} />}
              {snap.ma200 != null && <Stat label="MA200" value={money(snap.ma200)} />}
              {snap.adx != null && <Stat label="ADX" value={num(snap.adx).toFixed(1)} />}
              {snap.vix != null && <Stat label="VIX" value={num(snap.vix).toFixed(1)} />}
              {snap.iv_rank != null && <Stat label="IV rank" value={num(snap.iv_rank).toFixed(0)} />}
              {snap.news_score != null && <Stat label="News" value={num(snap.news_score).toFixed(2)} />}
              {snap.spy_trend && <Stat label="SPY trend" value={snap.spy_trend} />}
              {snap.market_trend && <Stat label="Market" value={snap.market_trend} />}
              {snap.vwap != null && <Stat label="VWAP" value={money(snap.vwap)} />}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
