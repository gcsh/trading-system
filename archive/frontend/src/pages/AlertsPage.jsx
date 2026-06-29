import React from 'react';
import AlertsCenter from '../components/AlertsCenter.jsx';
import TradeLog from '../components/TradeLog.jsx';

export default function AlertsPage() {
  return (
    <div className="grid">
      <AlertsCenter />
      <TradeLog />
    </div>
  );
}
