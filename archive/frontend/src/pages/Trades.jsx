import React from 'react';
import { useOutletContext } from 'react-router-dom';
import ExecutionQualityCard from '../components/ExecutionQualityCard.jsx';
import TopMetrics from '../components/TopMetrics.jsx';
import TradesTable from '../components/TradesTable.jsx';
import TradeLog from '../components/TradeLog.jsx';

export default function Trades() {
  const { performance, status } = useOutletContext();
  return (
    <>
      <TopMetrics performance={performance} status={status} />
      <div className="grid">
        <div className="col-12"><ExecutionQualityCard /></div>
        <TradesTable />
        <TradeLog />
      </div>
    </>
  );
}
