import React, { useEffect, useRef, useState } from 'react';
import { shortTime } from '../lib/format.js';

const SEVERITY_CLASS = {
  success: 'on',
  info: 'info',
  warning: 'warn',
  danger: 'danger',
};

// Request browser notification permission once.
function ensurePermission() {
  if (typeof Notification === 'undefined') return Promise.resolve('unsupported');
  if (Notification.permission === 'granted') return Promise.resolve('granted');
  if (Notification.permission === 'denied') return Promise.resolve('denied');
  return Notification.requestPermission();
}

export default function AlertsCenter() {
  const [alerts, setAlerts] = useState([]);
  const [permission, setPermission] = useState(
    typeof Notification !== 'undefined' ? Notification.permission : 'unsupported',
  );
  const [enableNotifs, setEnableNotifs] = useState(true);
  const seenIds = useRef(new Set());

  const load = async () => {
    try {
      const r = await fetch('/alerts/list');
      if (!r.ok) return;
      const data = await r.json();
      setAlerts(data);
      if (enableNotifs && permission === 'granted') {
        for (const a of data.slice().reverse()) {
          const key = `${a.timestamp}:${a.title}`;
          if (!seenIds.current.has(key)) {
            seenIds.current.add(key);
            if (seenIds.current.size > 1) {
              // Skip seeding existing alerts on first load.
              try {
                new Notification(a.title, { body: a.body, tag: key });
              } catch (e) {
                /* ignore */
              }
            }
          }
        }
      } else {
        // Still record seen ids so we don't burst when permission flips on later.
        data.forEach((a) => seenIds.current.add(`${a.timestamp}:${a.title}`));
      }
    } catch (e) {
      /* ignore */
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [permission, enableNotifs]);

  const askPermission = async () => {
    const result = await ensurePermission();
    setPermission(result);
  };

  return (
    <div className="panel col-6">
      <div className="panel-head">
        <h2>Alerts</h2>
        <div className="row">
          {permission !== 'granted' && permission !== 'unsupported' && (
            <button className="btn small" onClick={askPermission}>
              Enable browser notifications
            </button>
          )}
          <span className={`pill ${enableNotifs ? 'on' : 'off'}`}>
            <span className="dot" />
            {permission === 'granted' && enableNotifs ? 'desktop notifications' : 'in-app only'}
          </span>
          <button
            className="btn small ghost"
            onClick={() => setEnableNotifs((v) => !v)}
            title="Mute/unmute desktop notifications"
          >
            {enableNotifs ? 'mute' : 'unmute'}
          </button>
        </div>
      </div>
      {alerts.length === 0 ? (
        <div className="empty">No alerts yet. Signals, rejections, and order fills will appear here.</div>
      ) : (
        <div className="scroll" style={{ maxHeight: 260 }}>
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Severity</th>
                <th>Title</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((a, idx) => {
                const cls = SEVERITY_CLASS[a.severity] || 'off';
                return (
                  <tr key={`${a.timestamp}-${idx}`}>
                    <td style={{ color: 'var(--muted)' }}>{shortTime(a.timestamp)}</td>
                    <td><span className={`pill ${cls}`}>{a.severity}</span></td>
                    <td>{a.title}</td>
                    <td style={{ color: 'var(--muted)', fontSize: 12 }}>{a.body}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
