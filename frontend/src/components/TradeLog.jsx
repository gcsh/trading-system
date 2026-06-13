import React, { useEffect, useRef, useState } from 'react';
import { shortTime, tzAbbrev } from '../lib/format';

export default function TradeLog() {
  const [events, setEvents] = useState([]);
  const logRef = useRef(null);

  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${window.location.host}/ws/log`;
    const ws = new WebSocket(url);
    ws.onmessage = (msg) => {
      try {
        const parsed = JSON.parse(msg.data);
        setEvents((prev) => [...prev.slice(-499), parsed]);
      } catch (e) {
        console.warn('bad ws payload', e);
      }
    };
    return () => ws.close();
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [events]);

  return (
    <div className="panel col-12">
      <h2>Activity log <span style={{ fontSize: 10, color: 'var(--muted)', fontWeight: 400 }}>
        · times {tzAbbrev() || 'local'}
      </span></h2>
      <div className="log" ref={logRef}>
        {events.length === 0 && <div style={{ color: 'var(--muted)' }}>Waiting for events…</div>}
        {events.map((e, idx) => {
          const cls = e.status === 'submitted' ? 'ok' : e.status === 'rejected' ? 'reject' : '';
          return (
            <div key={idx} className="line">
              <span className="ts" title={e.timestamp}>{shortTime(e.timestamp)}</span>
              <span>{e.ticker}</span>
              <span className={cls}>{e.action}</span>
              <span>{e.reason}{e.risk ? ` · risk: ${e.risk}` : ''}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
