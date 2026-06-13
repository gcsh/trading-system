/**
 * Today — the operator's home page.
 *
 * Single answer to "what's the bot doing right now?" + "can I trust it?".
 * Composes existing CommandCenter sections plus a Money strip (equity
 * curve + performance metrics) at the top, replacing the old Portfolio /
 * Desk / Command Center scatter.
 */
import React from 'react';
import { useOutletContext } from 'react-router-dom';
import CommandCenter from './CommandCenter.jsx';
import EquityCurve from '../components/EquityCurve.jsx';
import PerformancePanel from '../components/PerformancePanel.jsx';
import RegimeBanner from '../components/RegimeBanner.jsx';

export default function Today() {
  const { equity, performance } = useOutletContext();
  return (
    <>
      <RegimeBanner />
      <div className="grid" style={{ marginBottom: 18 }}>
        <div className="col-8" style={{ minWidth: 0 }}>
          <EquityCurve data={equity} />
        </div>
        <div className="col-4" style={{ minWidth: 0 }}>
          <PerformancePanel performance={performance} />
        </div>
      </div>
      <CommandCenter />
    </>
  );
}
