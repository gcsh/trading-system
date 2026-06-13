import React from 'react';
import { useOutletContext } from 'react-router-dom';
import EquityCurve from '../components/EquityCurve.jsx';
import OpenPositions from '../components/OpenPositions.jsx';
import PaperAccount from '../components/PaperAccount.jsx';
import PerformancePanel from '../components/PerformancePanel.jsx';
import StrategyBreakdown from '../components/StrategyBreakdown.jsx';
import TopMetrics from '../components/TopMetrics.jsx';

export default function Portfolio() {
  const { performance, status, equity, config } = useOutletContext();
  return (
    <>
      <TopMetrics performance={performance} status={status} />
      <div className="grid">
        <EquityCurve data={equity} />
        <PerformancePanel performance={performance} />
        <OpenPositions />
        <PaperAccount broker={config.broker} />
        <StrategyBreakdown />
      </div>
    </>
  );
}
