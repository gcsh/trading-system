import React from 'react';
import { useOutletContext } from 'react-router-dom';
import AIInsights from '../components/AIInsights.jsx';
import AutoExecToggle from '../components/AutoExecToggle.jsx';
import AICostWidget from '../components/AICostWidget.jsx';
import CycleDiagnostics from '../components/CycleDiagnostics.jsx';
import FeatureImportance from '../components/FeatureImportance.jsx';
import PerRegimeImportance from '../components/PerRegimeImportance.jsx';
import { shortTime } from '../lib/format';

export default function AISignals() {
  const { config, status, updateConfig } = useOutletContext();
  const lastBlend = (status?.recent_signals || [])
    .slice()
    .reverse()
    .find((ev) => ev.ai_components);
  return (
    <div className="grid">
      <CycleDiagnostics />
      <AutoExecToggle
        value={config.auto_execute}
        onChange={(auto_execute) => updateConfig({ auto_execute })}
      />
      <AIInsights
        ai={config.ai}
        onChange={(ai) => updateConfig({ ai })}
        lastBlend={lastBlend}
      />
      <FeatureImportance topK={12} />
      <PerRegimeImportance topK={8} />
      <AICostWidget />
      <div className="panel col-12">
        <div className="panel-head">
          <h2>Recent AI-blended signals</h2>
          <span className="panel-sub">last {status?.recent_signals?.length || 0} cycles</span>
        </div>
        {(!status?.recent_signals || status.recent_signals.length === 0) ? (
          <div className="empty">
            <div className="title">No signals yet</div>
            <div className="hint">Start the bot to begin generating signals.</div>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Ticker</th>
                <th>Action</th>
                <th className="num">Confidence</th>
                <th>Reason</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {status.recent_signals.slice().reverse().slice(0, 30).map((ev, i) => {
                const isBuy = (ev.action || '').startsWith('BUY');
                const isSell = (ev.action || '').startsWith('SELL');
                return (
                  <tr key={i}>
                    <td style={{ color: 'var(--muted)' }} title={ev.timestamp || ''}>
                      {shortTime(ev.timestamp)}
                    </td>
                    <td><strong>{ev.ticker}</strong></td>
                    <td className={isBuy ? 'pos' : isSell ? 'neg' : ''}>
                      {(ev.action || '').replace(/_/g, ' ')}
                    </td>
                    <td className="num">{((ev.confidence || 0) * 100).toFixed(0)}%</td>
                    <td style={{ color: 'var(--muted)', fontSize: 12, maxWidth: 320 }}>{ev.reason}</td>
                    <td>
                      <span className={`pill ${
                        ev.status === 'submitted' ? 'on'
                          : ev.status === 'rejected' ? 'danger'
                          : ev.status === 'signal_only' ? 'info'
                          : 'off'
                      }`}>{ev.status}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
