/**
 * Trades — full audit + per-trade drill-down.
 *
 * TradesTable already supports ?id=X deep-linking via TradeDetail.
 * This page adds the inline-expand pattern: when ?id=X is set, the
 * trade detail (memo + consensus + chairman + autopsy if loss) renders
 * below the table. /mission-control and /autopsy redirect here.
 */
import React from 'react';
import { useOutletContext, useSearchParams } from 'react-router-dom';
import ExecutionQualityCard from '../components/ExecutionQualityCard.jsx';
import TopMetrics from '../components/TopMetrics.jsx';
import TradesTable from '../components/TradesTable.jsx';
import TradeLog from '../components/TradeLog.jsx';
import MissionControl from './MissionControl.jsx';

export default function TradesV2() {
  const { performance, status } = useOutletContext();
  const [sp] = useSearchParams();
  const tradeId = sp.get('id');
  return (
    <>
      <TopMetrics performance={performance} status={status} />
      <div className="grid">
        <div className="col-12"><ExecutionQualityCard /></div>
        <TradesTable />
        <TradeLog />
      </div>
      {tradeId && (
        <div style={{
          marginTop: 18, padding: '14px 0',
          borderTop: '2px solid var(--accent)',
        }}>
          <div className="row" style={{
            justifyContent: 'space-between', marginBottom: 8,
            padding: '0 4px',
          }}>
            <div style={{
              fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em',
              color: 'var(--muted)', fontWeight: 600,
            }}>Trade detail · #{tradeId}</div>
            <a className="btn small" href="/trades">close ✕</a>
          </div>
          <MissionControl />
        </div>
      )}
    </>
  );
}
