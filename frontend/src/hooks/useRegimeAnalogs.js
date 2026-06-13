import { useCallback, useEffect, useState } from 'react';

// MITS Phase 8.7 — pulls the most recent Opportunity Brain hypothesis
// and exposes its `historical_analogs` array. The bot writes this
// onto Trade.detail_json["opportunity_hypothesis"], so the regime banner
// can render "Today resembles: 2020-03-12 / 2018-12-24 / 2022-01-21".
// Falls back to /regime/intraday's snapshot when no recent opportunity
// trade has been logged yet.
export default function useRegimeAnalogs(ttlMs = 30000) {
  const [analogs, setAnalogs] = useState([]);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch('/regime/intraday');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const fromBrain = data?.opportunity?.historical_analogs;
      if (Array.isArray(fromBrain) && fromBrain.length) {
        setAnalogs(fromBrain);
      } else {
        setAnalogs([]);
      }
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    let id = null;
    const visible = () =>
      typeof document === 'undefined' || document.visibilityState !== 'hidden';
    const tick = () => { if (visible()) refresh(); };
    const start = () => { if (id == null) id = setInterval(tick, ttlMs); };
    const stop = () => { if (id != null) { clearInterval(id); id = null; } };
    const onVis = () => {
      if (visible()) { refresh(); start(); } else { stop(); }
    };

    refresh();
    if (visible()) start();
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis);
    }
    return () => {
      stop();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVis);
      }
    };
  }, [refresh, ttlMs]);

  return { analogs, refresh, error };
}
