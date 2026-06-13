import React, { useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import StrategySelector from '../components/StrategySelector.jsx';
import StrategyBreakdown from '../components/StrategyBreakdown.jsx';
import AnnotatedStrategyChart from '../components/AnnotatedStrategyChart.jsx';
import StrategyCompare from '../components/StrategyCompare.jsx';
import StrategyTester from '../components/StrategyTester.jsx';
import CustomRules from '../components/CustomRules.jsx';
import PineImport from '../components/PineImport.jsx';
import TickerSearch from '../components/TickerSearch.jsx';
import { useStrategies } from '../hooks/useStrategies.js';

export default function Strategies() {
  const { config, status, updateConfig } = useOutletContext();
  const strategies = useStrategies();
  const [testStrategy, setTestStrategy] = useState(config.strategy || 'macd_momentum');
  const [scope, setScope] = useState('single'); // single | folder | all
  const [folder, setFolder] = useState('default');
  const [folders, setFolders] = useState(['default']);
  const [chartTicker, setChartTicker] = useState((config.tickers && config.tickers[0]) || 'AAPL');
  const plan = status?.day_plan;

  useEffect(() => {
    fetch('/watchlist/folders').then((r) => r.ok && r.json()).then((f) => f && setFolders(f)).catch(() => {});
  }, []);

  // Which tickers does the chosen scope cover?
  const [folderTickers, setFolderTickers] = useState([]);
  useEffect(() => {
    if (scope !== 'folder') return;
    fetch(`/watchlist/items?list_name=${encodeURIComponent(folder)}`)
      .then((r) => r.ok && r.json())
      .then((items) => items && setFolderTickers(items.map((i) => i.ticker)))
      .catch(() => {});
  }, [scope, folder]);

  const scopeTickers = scope === 'single'
    ? [chartTicker]
    : scope === 'folder'
      ? folderTickers
      : (config.tickers || []);

  return (
    <div className="grid">
      <div className="panel col-12">
        <div className="panel-head">
          <h2>Today's plan</h2>
          <span className="panel-sub">picked by adaptive selector</span>
        </div>
        {plan ? (
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
            <div>
              <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Primary</div>
              <div style={{ fontSize: 18, fontWeight: 600, textTransform: 'capitalize' }}>{plan.primary_strategy?.replace(/_/g, ' ')}</div>
            </div>
            <div>
              <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Regime</div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{plan.market_regime || '—'}</div>
            </div>
            <div>
              <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Reason</div>
              <div style={{ fontSize: 14, color: 'var(--text-soft)' }}>{plan.reason}</div>
            </div>
          </div>
        ) : (
          <div className="empty"><div className="title">No plan yet</div><div className="hint">Start the bot or Run cycle to generate today's plan.</div></div>
        )}
      </div>

      {/* Multi-strategy comparison on a real chart with proactive suggestions */}
      <div className="panel col-12" style={{ padding: 0, border: 'none', background: 'transparent', boxShadow: 'none' }}>
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head">
            <h2>Pick the chart ticker</h2>
            <div style={{ width: 260 }}>
              <TickerSearch onAdd={(sym) => setChartTicker(sym)} placeholder={`Chart ticker: ${chartTicker} — search to change…`} />
            </div>
          </div>
          <div className="row">
            {(config.tickers || []).concat(['SPY','AAPL','NVDA','TSLA','QQQ']).filter((v,i,a)=>a.indexOf(v)===i).slice(0,10).map((t) => (
              <button key={t} className={`btn small ${chartTicker === t ? 'primary' : ''}`} onClick={() => setChartTicker(t)}>{t}</button>
            ))}
          </div>
        </div>
      </div>
      <StrategyCompare ticker={chartTicker} />

      {/* Single-strategy deep dive — annotated candlestick chart + theory studio */}
      <div className="panel col-12">
        <div className="panel-head">
          <div>
            <h2 style={{ margin: 0 }}>🔬 Theory Studio — study any strategy &amp; theory on any stock</h2>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>Pick a strategy and toggle classic theories (support/resistance, Bollinger, VWAP, Fibonacci, Elliott waves, trend channel) right on the chart.</div>
          </div>
          <select value={testStrategy} onChange={(e) => setTestStrategy(e.target.value)} style={{ width: 240 }}>
            {strategies.map((s) => (
              <option key={s.slug} value={s.slug}>{s.label}</option>
            ))}
          </select>
        </div>
        <AnnotatedStrategyChart strategy={testStrategy} ticker={chartTicker} />
      </div>

      {/* Test scope: single / folder / all */}
      <div className="panel col-12">
        <div className="panel-head">
          <h2>Test scope</h2>
          <div className="row">
            {['single', 'folder', 'all'].map((s) => (
              <button key={s} className={`btn small ${scope === s ? 'primary' : ''}`} onClick={() => setScope(s)}>
                {s === 'single' ? 'Single stock' : s === 'folder' ? 'Watchlist folder' : 'All configured'}
              </button>
            ))}
          </div>
        </div>

        {/* Scope-specific picker */}
        {scope === 'single' && (
          <div style={{ marginBottom: 12 }}>
            <label>Pick the stock to test (search any US ticker)</label>
            <div className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
              <div style={{ flex: 1, minWidth: 220 }}>
                <TickerSearch onAdd={(sym) => setChartTicker(sym)} placeholder={`Currently: ${chartTicker} — search to change…`} />
              </div>
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <span style={{ fontSize: 11, color: 'var(--muted)' }}>quick:</span>
              {(config.tickers || []).concat(['SPY','AAPL','NVDA','TSLA']).filter((v,i,a)=>a.indexOf(v)===i).slice(0,10).map((t) => (
                <button key={t} className={`btn small ${chartTicker === t ? 'primary' : ''}`} onClick={() => setChartTicker(t)}>{t}</button>
              ))}
            </div>
          </div>
        )}
        {scope === 'folder' && (
          <div style={{ marginBottom: 12 }}>
            <label>Watchlist folder to test</label>
            <select value={folder} onChange={(e) => setFolder(e.target.value)} style={{ width: 220 }}>
              {folders.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
        )}

        <div style={{ fontSize: 13, color: 'var(--text-soft)', marginBottom: 8 }}>
          Testing <strong>{testStrategy.replace(/_/g, ' ')}</strong> on{' '}
          {scope === 'single'
            ? <strong>{chartTicker}</strong>
            : scopeTickers.length
              ? <strong>{scopeTickers.length} ticker(s): {scopeTickers.join(', ')}</strong>
              : <span style={{ color: 'var(--warn)' }}>no tickers in this scope — add some first</span>}
        </div>
        <StrategyTester
          strategy={testStrategy}
          tickers={scopeTickers}
          onApply={(s) => updateConfig({ strategy: s })}
        />
      </div>

      <StrategySelector
        value={config.strategy}
        onChange={(strategy) => updateConfig({ strategy })}
        onTest={(strategy) => setTestStrategy(strategy)}
      />
      <StrategyBreakdown />

      <PineImport onApplied={() => updateConfig({ strategy: 'custom' })} />
      <CustomRules value={config.custom_rules} onChange={(custom_rules) => updateConfig({ custom_rules })} />
    </div>
  );
}
