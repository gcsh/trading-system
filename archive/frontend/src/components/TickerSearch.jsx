import React, { useEffect, useRef, useState } from 'react';

export default function TickerSearch({
  onAdd,
  placeholder = 'Search by symbol or company…',
  id,
  autoFocus = false,
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const containerRef = useRef(null);
  const inputRef = useRef(null);
  const debounceRef = useRef();

  // Phase 19.x — when StockAnalysis lands without a ticker in the URL,
  // we want the prominent picker focused so the operator can type a
  // symbol immediately. Plumbed via `autoFocus`; consumers that don't
  // want auto-focus just omit the prop.
  useEffect(() => {
    if (autoFocus && inputRef.current) {
      try { inputRef.current.focus({ preventScroll: true }); } catch (_) {
        inputRef.current.focus();
      }
    }
  }, [autoFocus]);

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        setBusy(true);
        const r = await fetch(`/market/search?q=${encodeURIComponent(query.trim())}`);
        if (r.ok) {
          setResults(await r.json());
          setOpen(true);
        }
      } finally {
        setBusy(false);
      }
    }, 220);
    return () => clearTimeout(debounceRef.current);
  }, [query]);

  // Close dropdown on outside click.
  useEffect(() => {
    const handle = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, []);

  const pick = (item) => {
    setOpen(false);
    setQuery('');
    setResults([]);
    onAdd && onAdd(item.symbol, item);
  };

  return (
    <div ref={containerRef} style={{ position: 'relative', width: '100%' }}>
      <input
        ref={inputRef}
        id={id}
        type="text"
        value={query}
        placeholder={placeholder}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && results.length > 0) pick(results[0]);
          if (e.key === 'Enter' && results.length === 0 && query.trim()) {
            pick({ symbol: query.trim().toUpperCase(), description: '' });
          }
          if (e.key === 'Escape') setOpen(false);
        }}
      />
      {open && (results.length > 0 || busy) && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            left: 0,
            right: 0,
            background: 'var(--panel)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            boxShadow: 'var(--shadow-md)',
            zIndex: 20,
            maxHeight: 320,
            overflowY: 'auto',
          }}
        >
          {busy && (
            <div style={{ padding: '8px 12px', color: 'var(--muted)', fontSize: 12 }}>Searching…</div>
          )}
          {!busy && results.map((r) => (
            <div
              key={r.symbol}
              onClick={() => pick(r)}
              style={{
                padding: '8px 12px',
                cursor: 'pointer',
                display: 'flex',
                justifyContent: 'space-between',
                gap: 12,
                borderBottom: '1px solid var(--border)',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--hover)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{r.symbol}</div>
                <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{r.description}</div>
              </div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>{r.type}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
