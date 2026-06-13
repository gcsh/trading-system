import React, { useEffect, useRef, useState } from 'react';

const GREETING = {
  role: 'assistant',
  content: "Hi! I'm your trading copilot. Ask me about your positions, a stock, what the bot is doing, or how any strategy works.",
};

export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([GREETING]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [aiReady, setAiReady] = useState(null);   // null = unknown
  const [keyInput, setKeyInput] = useState('');
  const [savingKey, setSavingKey] = useState(false);
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, open]);

  // Check whether a Claude key is configured when the panel opens.
  useEffect(() => {
    if (!open) return;
    fetch('/copilot/ai-status').then((r) => r.json()).then((d) => setAiReady(!!d.ai_available)).catch(() => {});
  }, [open]);

  const saveKey = async () => {
    const k = keyInput.trim();
    if (!k || savingKey) return;
    setSavingKey(true);
    try {
      const r = await fetch('/copilot/ai-key', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: k }),
      });
      const d = await r.json();
      setAiReady(!!d.ai_available);
      setKeyInput('');
      if (d.ai_available) setMessages((m) => [...m, { role: 'assistant', content: '✅ Connected to Claude — ask away!' }]);
    } catch (e) {
      setMessages((m) => [...m, { role: 'assistant', content: `Couldn't save the key — ${e.message}` }]);
    } finally { setSavingKey(false); }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    const history = messages.filter((m) => m.role === 'user' || m.role === 'assistant');
    const next = [...messages, { role: 'user', content: text }];
    setMessages(next);
    setInput('');
    setBusy(true);
    try {
      const r = await fetch('/copilot/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history }),
      });
      const d = await r.json();
      if (typeof d.available === 'boolean') setAiReady(d.available);
      setMessages((m) => [...m, { role: 'assistant', content: d.reply || '(no reply)' }]);
    } catch (e) {
      setMessages((m) => [...m, { role: 'assistant', content: `Sorry — ${e.message}` }]);
    } finally {
      setBusy(false);
    }
  };

  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  };

  return (
    <>
      <button
        onClick={() => setOpen((o) => !o)}
        title="Chat with your AI copilot"
        style={{
          position: 'fixed', right: 22, bottom: 22, zIndex: 60, width: 56, height: 56, borderRadius: '50%',
          border: '1px solid var(--accent)', cursor: 'pointer', fontSize: 24,
          background: 'linear-gradient(135deg, var(--accent), var(--accent-2))', color: '#fff',
          boxShadow: '0 6px 20px rgba(0,0,0,0.35)',
        }}
      >{open ? '×' : '💬'}</button>

      {open && (
        <div
          style={{
            position: 'fixed', right: 22, bottom: 88, zIndex: 60, width: 380, maxWidth: 'calc(100vw - 44px)',
            height: 520, maxHeight: 'calc(100vh - 130px)', display: 'flex', flexDirection: 'column',
            background: 'var(--panel)', border: '1px solid var(--border-strong)', borderRadius: 14,
            boxShadow: '0 12px 40px rgba(0,0,0,0.45)', overflow: 'hidden',
          }}
        >
          <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 18 }}>🤖</span>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14 }}>AI Copilot</div>
              <div style={{ fontSize: 11, color: 'var(--muted)' }}>Knows your account, positions & the bot's plan</div>
            </div>
          </div>

          <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {messages.map((m, i) => (
              <div key={i} style={{ alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start', maxWidth: '85%' }}>
                <div style={{
                  padding: '8px 11px', borderRadius: 12, fontSize: 13, lineHeight: 1.45, whiteSpace: 'pre-wrap',
                  background: m.role === 'user' ? 'var(--accent)' : 'var(--panel-2)',
                  color: m.role === 'user' ? '#fff' : 'var(--text)',
                  border: m.role === 'user' ? 'none' : '1px solid var(--border)',
                }}>{m.content}</div>
              </div>
            ))}
            {busy && <div style={{ alignSelf: 'flex-start', fontSize: 12, color: 'var(--muted)' }}>thinking…</div>}
          </div>

          {aiReady === false && (
            <div style={{ padding: '10px 12px', borderTop: '1px solid var(--border)', background: 'var(--panel-2)' }}>
              <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 6 }}>
                🔑 Connect your Anthropic API key to chat — stored locally on your machine, never shared.
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <input
                  type="password"
                  value={keyInput}
                  onChange={(e) => setKeyInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); saveKey(); } }}
                  placeholder="sk-ant-…"
                  style={{ flex: 1, borderRadius: 8, border: '1px solid var(--border)', background: 'var(--panel)', color: 'var(--text)', padding: '8px 10px', fontSize: 12 }}
                />
                <button className="btn small primary" onClick={saveKey} disabled={savingKey || !keyInput.trim()}>{savingKey ? '…' : 'Connect'}</button>
              </div>
              <a href="https://console.anthropic.com/" target="_blank" rel="noreferrer" style={{ fontSize: 10.5, color: 'var(--info)', display: 'inline-block', marginTop: 5 }}>Get an API key →</a>
            </div>
          )}

          <div style={{ padding: 10, borderTop: '1px solid var(--border)', display: 'flex', gap: 8 }}>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKey}
              placeholder="Ask anything… (Enter to send)"
              rows={1}
              style={{
                flex: 1, resize: 'none', borderRadius: 9, border: '1px solid var(--border)', background: 'var(--panel-2)',
                color: 'var(--text)', padding: '9px 11px', fontSize: 13, fontFamily: 'inherit', maxHeight: 90,
              }}
            />
            <button className="btn primary" onClick={send} disabled={busy || !input.trim()} style={{ alignSelf: 'stretch' }}>Send</button>
          </div>
        </div>
      )}
    </>
  );
}
