import React, { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from 'recharts';
import { money, num, shortTime } from '../lib/format.js';

async function fetchHistory(ticker) {
  // We use the snapshot endpoint as a proxy: it returns a single price.
  // To draw a chart we poll repeatedly and accumulate locally.
  try {
    const r = await fetch(`/watchlist`);
    if (!r.ok) return null;
    const list = await r.json();
    const item = list.find((i) => i.ticker === ticker);
    return item?.quote || null;
  } catch (e) {
    return null;
  }
}

export default function TickerChart({ ticker }) {
  const [series, setSeries] = useState([]);
  const [last, setLast] = useState(null);

  useEffect(() => {
    let active = true;
    setSeries([]);
    setLast(null);
    const tick = async () => {
      const quote = await fetchHistory(ticker);
      if (!active || !quote) return;
      setLast(quote);
      setSeries((prev) => {
        const next = [
          ...prev,
          { t: new Date().toISOString(), price: num(quote.price) },
        ];
        return next.slice(-120);
      });
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [ticker]);

  if (!ticker) {
    return (
      <div className="panel col-12">
        <div className="panel-head">
          <h2>Live chart</h2>
          <span className="panel-sub">click a watchlist ticker</span>
        </div>
        <div className="empty">Select a ticker from the watchlist to see its live price.</div>
      </div>
    );
  }

  const change = num(last?.change_pct);
  const positive = change >= 0;
  const color = positive ? 'var(--accent)' : 'var(--danger)';

  return (
    <div className="panel col-12">
      <div className="panel-head">
        <h2>{ticker} · live chart</h2>
        <div className="row">
          <span style={{ fontSize: 18, fontWeight: 600 }}>{money(last?.price)}</span>
          <span className={`pill ${positive ? 'on' : 'danger'}`}>
            {positive ? '+' : ''}
            {change.toFixed(2)}%
          </span>
          {last?.source && <span className="pill info">{last.source}</span>}
        </div>
      </div>
      <div className="chart-wrap" style={{ height: 240 }}>
        {series.length < 2 ? (
          <div className="empty">Collecting ticks… (5-sec poll)</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={series} margin={{ top: 4, right: 8, left: 4, bottom: 0 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis
                dataKey="t"
                tickFormatter={shortTime}
                tick={{ fontSize: 11, fill: 'var(--muted)' }}
                stroke="var(--border)"
                minTickGap={48}
              />
              <YAxis
                domain={['auto', 'auto']}
                tick={{ fontSize: 11, fill: 'var(--muted)' }}
                tickFormatter={(v) => money(v)}
                width={80}
                stroke="var(--border)"
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--panel)',
                  border: '1px solid var(--border)',
                  borderRadius: 8,
                  fontSize: 12,
                }}
                labelFormatter={shortTime}
                formatter={(v) => [money(v), 'Price']}
              />
              <Line
                type="monotone"
                dataKey="price"
                stroke={color}
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
