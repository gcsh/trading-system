import React from 'react';
import { useOutletContext } from 'react-router-dom';
import TopMetrics from '../components/TopMetrics.jsx';
import EquityCurve from '../components/EquityCurve.jsx';
import PerformancePanel from '../components/PerformancePanel.jsx';
import OpenPositions from '../components/OpenPositions.jsx';
import StrategyBreakdown from '../components/StrategyBreakdown.jsx';
import TradesTable from '../components/TradesTable.jsx';
import MarketPulse from '../components/MarketPulse.jsx';
import BotHealth from '../components/BotHealth.jsx';

export default function Overview() {
  const { performance, status, equity, config, updateConfig, refresh } = useOutletContext();

  const startBot = async () => {
    await fetch('/bot/start', { method: 'POST' });
    refresh && refresh();
  };

  return (
    <>
      <BotHealth
        config={config}
        status={status}
        updateConfig={updateConfig}
        onStart={startBot}
        onForceTrade={() => refresh && refresh()}
      />
      <TopMetrics performance={performance} status={status} />
      <div className="grid">
        <MarketPulse compact />
        <EquityCurve data={equity} />
        <PerformancePanel performance={performance} />
        <OpenPositions />
        <StrategyBreakdown />
        <TradesTable />
      </div>
    </>
  );
}
