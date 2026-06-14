/**
 * LiveTapeChart — professional candlestick + volume + pivots view for any
 * ticker in the scan universe, with the bot's own buy/sell markers
 * overlaid as arrows on the relevant candles.
 *
 * Rewrite (2026-06-13): switched from custom-SVG line chart to
 * lightweight-charts (same library as TheoryChart). Visual target is
 * the operator's reference image: clean candles, volume histogram with
 * MA-20 in a sub-pane, current-price pill in green on the right axis,
 * pivot point annotations (R1/PP/S1) labeled on the right edge, native
 * crosshair, and a markets-closed pill so weekends don't look broken.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  createChart,
  CrosshairMode,
  LineStyle,
} from 'lightweight-charts';

const PERIODS = [
  { key: '1D', period: '1d',  interval: '5m'  },
  { key: '5D', period: '5d',  interval: '15m' },
  { key: '1M', period: '1mo', interval: '1d'  },
  { key: '3M', period: '3mo', interval: '1d'  },
  { key: '6M', period: '6mo', interval: '1d'  },
];

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function formatPrice(n) {
  if (n == null || !Number.isFinite(n)) return '—';
  return n.toFixed(2);
}

/** Floor-seconds Unix timestamp for lightweight-charts time axis. */
function toUnixSec(t) {
  const ms = typeof t === 'string' ? new Date(t).getTime() : Number(t);
  return Math.floor(ms / 1000);
}

/** Classic pivot points from the prior session's H/L/C. */
function computePivots(candles) {
  if (!candles || candles.length < 2) return null;
  // Heuristic: pivots are most meaningful when computed from a daily
  // session. For intraday data, group by trading date and use the
  // prior date's aggregated H/L/C. For daily candles, use the prior bar.
  const last = candles[candles.length - 1];
  const lastDate = new Date(last.time * 1000).toDateString();
  let h = -Infinity, l = Infinity, c = null;
  for (let i = candles.length - 2; i >= 0; i--) {
    const cd = candles[i];
    const date = new Date(cd.time * 1000).toDateString();
    if (date === lastDate) continue;
    h = Math.max(h, cd.high);
    l = Math.min(l, cd.low);
    if (c === null) c = cd.close; // closest-to-now close in the prior session
    // Stop once we cross to a session before the immediately prior one.
    const cutoff = new Date(last.time * 1000);
    cutoff.setDate(cutoff.getDate() - 2);
    if (cd.time * 1000 < cutoff.getTime()) break;
  }
  if (!Number.isFinite(h) || !Number.isFinite(l) || c == null) {
    const prev = candles[candles.length - 2];
    h = prev.high; l = prev.low; c = prev.close;
  }
  const pp = (h + l + c) / 3;
  return {
    r1: 2 * pp - l,
    pp,
    s1: 2 * pp - h,
  };
}

/** Rolling simple MA of an array of numbers. */
function rollingMA(values, window) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= window) sum -= values[i - window];
    if (i >= window - 1) out[i] = sum / window;
  }
  return out;
}

export default function LiveTapeChart() {
  const [universe, setUniverse] = useState([]);
  const [trades, setTrades] = useState([]);
  const [ticker, setTicker] = useState('SPY');
  const [period, setPeriod] = useState(PERIODS[1]);
  const [candles, setCandles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const wrapRef = useRef(null);
  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);
  const volMaSeriesRef = useRef(null);
  const pivotLinesRef = useRef([]);

  // Load scan universe + trade list once.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [u, ts] = await Promise.all([
          api('/authority/scan-universe'),
          api('/trades/list?limit=200'),
        ]);
        if (!active) return;
        const tickerList = u.tickers || [];
        setUniverse(tickerList);
        setTrades(ts || []);
        const recent = (ts || [])[0]?.ticker;
        if (recent && tickerList.includes(recent)) setTicker(recent);
        else if (tickerList.length) setTicker(tickerList[0]);
      } catch (e) {
        setError(e.message);
      }
    })();
    return () => { active = false; };
  }, []);

  // Reload candles when ticker or period changes.
  useEffect(() => {
    if (!ticker) { setLoading(false); return; }
    let active = true;
    setLoading(true);
    (async () => {
      try {
        const c = await api(
          `/market/candles/${ticker}?period=${period.period}&interval=${period.interval}`,
        );
        if (!active) return;
        const arr = c?.candles || c || [];
        const normalized = (Array.isArray(arr) ? arr : [])
          .map((c) => ({
            time: toUnixSec(c.time || c.t || c.timestamp),
            open: Number(c.open ?? c.o),
            high: Number(c.high ?? c.h),
            low: Number(c.low ?? c.l),
            close: Number(c.close ?? c.c),
            volume: Number(c.volume ?? c.v ?? 0),
          }))
          .filter((c) => Number.isFinite(c.close) && Number.isFinite(c.time))
          .sort((a, b) => a.time - b.time);
        setCandles(normalized);
        setError(null);
      } catch (e) {
        setError(e.message);
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [ticker, period]);

  // Live trade refresh — every 8s.
  useEffect(() => {
    const id = setInterval(async () => {
      try { setTrades(await api('/trades/list?limit=200')); }
      catch { /* swallow */ }
    }, 8000);
    return () => clearInterval(id);
  }, []);

  const tradesForTicker = useMemo(
    () => trades.filter((t) => t.ticker === ticker),
    [trades, ticker],
  );

  // Build the chart exactly once per container mount.
  useEffect(() => {
    if (!chartContainerRef.current) return;
    if (chartRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: 'transparent' },
        textColor: 'rgba(220, 226, 235, 0.85)',
        fontSize: 11,
        fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Inter, sans-serif',
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.04)', style: LineStyle.Dotted },
        horzLines: { color: 'rgba(255, 255, 255, 0.05)', style: LineStyle.Dotted },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: 'rgba(160, 110, 220, 0.7)',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#221a33',
        },
        horzLine: {
          color: 'rgba(160, 110, 220, 0.55)',
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: '#221a33',
        },
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.06)',
        scaleMargins: { top: 0.05, bottom: 0.28 },
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.06)',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
        barSpacing: 6,
      },
      width: chartContainerRef.current.clientWidth || 600,
      height: chartContainerRef.current.clientHeight || 380,
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      priceLineVisible: true,
      priceLineColor: '#22c55e',
      priceLineWidth: 1,
      priceLineStyle: LineStyle.Solid,
      lastValueVisible: true,
    });

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      lastValueVisible: true,
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
      borderColor: 'rgba(255, 255, 255, 0.04)',
    });

    const volMaSeries = chart.addLineSeries({
      color: 'rgba(245, 158, 11, 0.85)',
      lineWidth: 1,
      priceScaleId: 'vol',
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    volMaSeriesRef.current = volMaSeries;

    const ro = new ResizeObserver((entries) => {
      if (!chartRef.current) return;
      for (const e of entries) {
        chartRef.current.applyOptions({
          width: Math.floor(e.contentRect.width),
          height: Math.floor(e.contentRect.height),
        });
      }
    });
    ro.observe(chartContainerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      volMaSeriesRef.current = null;
      pivotLinesRef.current = [];
    };
  }, []);

  // Whenever candles / trades change, push fresh data into the series.
  useEffect(() => {
    if (!candleSeriesRef.current || candles.length === 0) return;

    candleSeriesRef.current.setData(candles);

    // Volume histogram — color matches the candle direction so the eye
    // can read pressure direction at a glance.
    const volData = candles.map((c) => ({
      time: c.time,
      value: c.volume,
      color: c.close >= c.open ? 'rgba(34, 197, 94, 0.55)' : 'rgba(239, 68, 68, 0.55)',
    }));
    volumeSeriesRef.current.setData(volData);

    // 20-period volume MA — orange line over the histogram.
    const volumes = candles.map((c) => c.volume);
    const ma = rollingMA(volumes, 20);
    volMaSeriesRef.current.setData(
      candles
        .map((c, i) => (ma[i] == null ? null : { time: c.time, value: ma[i] }))
        .filter(Boolean),
    );

    // Pivot point lines — drop the previous set, add the new.
    if (candleSeriesRef.current && pivotLinesRef.current.length) {
      for (const ln of pivotLinesRef.current) {
        try { candleSeriesRef.current.removePriceLine(ln); } catch { /* noop */ }
      }
      pivotLinesRef.current = [];
    }
    const pivots = computePivots(candles);
    if (pivots && candleSeriesRef.current) {
      const r1 = candleSeriesRef.current.createPriceLine({
        price: pivots.r1,
        color: 'rgba(245, 158, 11, 0.85)',
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: 'R1',
      });
      const pp = candleSeriesRef.current.createPriceLine({
        price: pivots.pp,
        color: 'rgba(255, 255, 255, 0.85)',
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: 'PP',
      });
      const s1 = candleSeriesRef.current.createPriceLine({
        price: pivots.s1,
        color: 'rgba(245, 158, 11, 0.85)',
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: 'S1',
      });
      pivotLinesRef.current = [r1, pp, s1];
    }

    // Bot trade markers — buy=green-arrow-below, sell=red-arrow-above,
    // placed on the candle whose time window contains the fill.
    const markers = tradesForTicker
      .map((t) => {
        const ts = toUnixSec(t.timestamp || t.t);
        if (!ts || ts < candles[0].time || ts > candles[candles.length - 1].time) return null;
        const action = (t.action || '').toUpperCase();
        const side = action.startsWith('BUY')
          ? 'buy'
          : action.startsWith('SELL')
            ? 'sell'
            : 'hold';
        if (side === 'hold') return null;
        return {
          time: ts,
          position: side === 'buy' ? 'belowBar' : 'aboveBar',
          color: side === 'buy' ? '#22c55e' : '#ef4444',
          shape: side === 'buy' ? 'arrowUp' : 'arrowDown',
          text: `${action} ${Number(t.qty || t.quantity || 0).toFixed(2)} @ ${formatPrice(Number(t.price))}`,
        };
      })
      .filter(Boolean)
      .sort((a, b) => a.time - b.time);

    candleSeriesRef.current.setMarkers(markers);

    // Fit the visible range after a data load — but only on first load
    // for this ticker/period; otherwise the user's zoom is preserved.
    if (candles.length) chartRef.current.timeScale().fitContent();
  }, [candles, tradesForTicker]);

  // Header price + change derived from the candle data (latest close vs
  // first close in the visible range).
  const headerStats = useMemo(() => {
    if (!candles.length) return null;
    const last = candles[candles.length - 1].close;
    const first = candles[0].close;
    const change = last - first;
    const pct = first ? (change / first) * 100 : 0;
    return { last, change, pct, positive: change >= 0 };
  }, [candles]);

  // Market-state pill — explains weekends/holidays/after-hours so the
  // chart's terminal candle doesn't look like a bug.
  const marketState = (() => {
    const now = new Date();
    const dow = now.getDay();
    if (dow === 0) return { closed: true, label: 'Markets closed · Sunday', reopen: 'Reopens Mon 9:30 ET' };
    if (dow === 6) return { closed: true, label: 'Markets closed · Saturday', reopen: 'Reopens Mon 9:30 ET' };
    const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const m = et.getHours() * 60 + et.getMinutes();
    if (m < 9 * 60 + 30) return { closed: true, label: 'Pre-market', reopen: 'Opens 9:30 ET' };
    if (m >= 16 * 60) return { closed: true, label: 'After hours', reopen: 'Reopens 9:30 ET next session' };
    return { closed: false, label: 'Markets open', reopen: '' };
  })();

  const lastCandleDate = candles.length
    ? new Date(candles[candles.length - 1].time * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    : null;

  return (
    <div className="panel panel--markets" ref={wrapRef}>
      <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div style={{ minWidth: 200 }}>
          <div style={{
            fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
            color: 'var(--muted)', fontWeight: 600,
          }}>Live tape · with the bot's trades</div>
          <div className="row" style={{ gap: 12, marginTop: 4, alignItems: 'baseline' }}>
            <h3 style={{ margin: 0, fontSize: 22, fontWeight: 700, letterSpacing: '-0.015em' }}>
              {ticker || '—'}
            </h3>
            {headerStats && (
              <>
                <span style={{ fontSize: 17, fontWeight: 600, fontFeatureSettings: '"tnum"' }}>
                  ${formatPrice(headerStats.last)}
                </span>
                <span className={headerStats.positive ? 'pill on' : 'pill danger'}>
                  {headerStats.positive ? '▲' : '▼'} {headerStats.positive ? '+' : ''}{headerStats.pct.toFixed(2)}%
                </span>
              </>
            )}
            {marketState.closed && (
              <span
                title={marketState.reopen}
                style={{
                  fontSize: 10, fontWeight: 700, letterSpacing: '0.08em',
                  textTransform: 'uppercase', padding: '3px 8px',
                  borderRadius: 6, color: 'rgba(245, 158, 11, 1)',
                  background: 'rgba(245, 158, 11, 0.12)',
                  border: '1px solid rgba(245, 158, 11, 0.30)',
                }}
              >
                {marketState.label}{lastCandleDate ? ` · last close ${lastCandleDate}` : ''}
              </span>
            )}
          </div>
        </div>

        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <select
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            style={{ width: 110, padding: '6px 28px 6px 10px', fontWeight: 600 }}
          >
            {universe.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
            {!universe.includes(ticker) && ticker && (
              <option value={ticker}>{ticker}</option>
            )}
          </select>
          <div className="row" style={{
            gap: 2, background: 'var(--panel-2)', borderRadius: 8, padding: 2,
            border: '1px solid var(--border)',
          }}>
            {PERIODS.map((p) => (
              <button
                key={p.key}
                onClick={() => setPeriod(p)}
                className="btn small ghost"
                style={{
                  padding: '4px 9px', fontSize: 11, fontWeight: 600,
                  background: period.key === p.key ? 'var(--accent)' : 'transparent',
                  color: period.key === p.key ? '#02160e' : 'var(--text-soft)',
                  border: 'none', boxShadow: 'none', borderRadius: 6,
                }}
              >
                {p.key}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="empty" style={{ padding: 16, color: 'var(--danger)' }}>
          chart error: {error}
        </div>
      )}

      {/* Chart container — lightweight-charts manages its own canvas inside */}
      <div
        ref={chartContainerRef}
        style={{
          position: 'relative',
          width: '100%',
          height: 380,
          marginTop: 8,
          opacity: loading ? 0.55 : 1,
          transition: 'opacity 200ms',
        }}
      />

      {tradesForTicker.length === 0 && !loading && (
        <div style={{
          marginTop: 6, fontSize: 11, color: 'var(--muted)',
          padding: '0 4px',
        }}>
          No bot trades on {ticker} in this window — markers will appear when the bot acts.
        </div>
      )}
    </div>
  );
}
